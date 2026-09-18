"""Working Memory：Redis 近期消息窗口（docs/06 §M-04，四层记忆第一层）。

设计要点：
  - 每会话一个 Redis list（wm:session:{id}），元素为 JSON 序列化的消息
  - append 只保留最近 _MAX_MESSAGES 条（LTRIM），窗口天然有界
  - 整链 7 天 TTL（会话级自愈；DB 中的消息是持久真相源，Redis 仅是热窗口）
  - window() 从最新往旧累计 token，超出预算截断（Context Builder 的取数入口）
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.context.tokenizer import count_tokens

logger = logging.getLogger(__name__)

_KEY_PREFIX = "wm:session:"
_MAX_MESSAGES = 200  # 窗口硬上限（条数）
_TTL_SECONDS = 7 * 24 * 3600  # 会话窗口保留 7 天


class WorkingMemory:
    """会话级近期消息窗口（Redis list 实现）。"""

    def __init__(self, redis: Redis) -> None:
        """注入 Redis 客户端（复用进程级连接）。"""
        self._redis = redis

    @staticmethod
    def _key(session_id: uuid.UUID) -> str:
        """会话对应的 Redis key。"""
        return f"{_KEY_PREFIX}{session_id}"

    async def append(self, session_id: uuid.UUID, *, role: str, content: str) -> None:
        """追加一条消息到窗口尾部（含 token 计数与时间戳）。"""
        entry: dict[str, Any] = {
            "role": role,
            "content": content,
            "token_count": count_tokens(content),
            "created_at": datetime.now(UTC).isoformat(),
        }
        key = self._key(session_id)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.rpush(key, json.dumps(entry, ensure_ascii=False))
            pipe.ltrim(key, -_MAX_MESSAGES, -1)  # 只留最近 N 条
            pipe.expire(key, _TTL_SECONDS)
            await pipe.execute()

    async def window(self, session_id: uuid.UUID, max_tokens: int) -> list[dict[str, Any]]:
        """取 token 预算内的消息窗口（时间正序返回）。

        从最新往旧累计，超预算即截断；始终至少保留最新一条（预算极小时
        也保证上下文非空，由调用方决定是否再放大预算）。

        Args:
            session_id: 目标会话。
            max_tokens: 窗口 token 预算（Context Builder 按预算档位传入）。
        """
        raw = await self._redis.lrange(self._key(session_id), 0, -1)
        entries: list[dict[str, Any]] = []
        for item in reversed(raw):  # 最新在前
            try:
                entry = json.loads(item)
            except json.JSONDecodeError:
                logger.warning("working_memory_skip_corrupted session=%s", session_id)
                continue
            entries.append(entry)
            if sum(e["token_count"] for e in entries) > max_tokens and len(entries) > 1:
                entries.pop()  # 超预算：丢弃这条边界消息（保留至少最新一条）
                break
        entries.reverse()  # 恢复时间正序
        return entries

    async def all_entries(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """取窗口内全部消息（时间正序，不做 token 截断）。

        RollingSummarizer 用它与 window() 的差集识别"滑出段"。

        Args:
            session_id: 目标会话。
        """
        raw = await self._redis.lrange(self._key(session_id), 0, -1)
        entries: list[dict[str, Any]] = []
        for item in raw:  # list 本身即时间正序（rpush 追加）
            try:
                entries.append(json.loads(item))
            except json.JSONDecodeError:
                logger.warning("working_memory_skip_corrupted session=%s", session_id)
        return entries

    async def trim_to_keep(self, session_id: uuid.UUID, keep: int) -> None:
        """只保留最近 keep 条消息（滑出段已并入摘要后裁剪，防重复摘要）。

        Args:
            session_id: 目标会话。
            keep: 保留的尾部条数（window() 返回的窗口长度）。
        """
        if keep <= 0:
            return
        key = self._key(session_id)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.ltrim(key, -keep, -1)
            pipe.expire(key, _TTL_SECONDS)
            await pipe.execute()
