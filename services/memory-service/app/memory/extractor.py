"""LLM 结构化事实抽取（M-04 写路径第 ① 步）。

职责：对话轮次（带 ID 标注）→ ExtractedFact 列表。
json_mode 请求 + 容错解析（markdown 围栏剥离）+ 字段校验；
低置信度结果在管线层过滤，此处只做解析与类型归一。
"""

import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from app.llm.client import LLMClient
from app.memory.prompts import build_extract_messages
from app.memory.schemas import ExtractedFact
from app.models.enums import MemoryType

logger = logging.getLogger(__name__)

# key 命名示例（注入抽取 prompt，约束层级化命名）
_KEY_EXAMPLES = (
    '"skill.langgraph", "profile.job", "job.字节.进度", "preference.summary_style", "tool.vscode"'
)

# 抽取合法的记忆类型（episodic 由滚动摘要管线负责，不在此抽取）
_VALID_TYPES = {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}


class ExtractionError(Exception):
    """抽取输出无法解析为合法 JSON 结构（调用方降级为跳过本轮）。"""


class MemoryExtractor:
    """对话 → 候选事实抽取器（LLM structured output）。"""

    def __init__(self, llm: LLMClient) -> None:
        """注入 LLM 客户端（生产为抽取小模型，测试可替换 fake）。

        Args:
            llm: OpenAI 兼容客户端。
        """
        self._llm = llm

    async def extract(
        self,
        messages: list[tuple[uuid.UUID, str, str]],
        *,
        window: int = 20,
        known_keys: Sequence[str] = (),
    ) -> list[ExtractedFact]:
        """抽取一轮对话中的候选事实。

        Args:
            messages: (message_id, role, content) 三元组列表，时间正序。
            window: 参与抽取的最近消息条数上限（控制 prompt 长度）。
            known_keys: 用户既有 active 记忆的 key 列表（注入 prompt 引导
                复用，稳定 key 命名使精确判重与仲裁链路生效）。

        Returns:
            抽取事实列表（可能为空 —— 无值得记住的内容是正常结果）。

        Raises:
            LLMError: 上游调用失败（超时/5xx，调用方降级跳过本轮）。
            ExtractionError: 输出 JSON 结构非法（调用方降级跳过本轮）。
        """
        recent = messages[-window:]
        transcript = "\n".join(f"[{mid}] {role}: {content}" for mid, role, content in recent)
        extra_rules = ""
        if known_keys:
            keys_text = ", ".join(sorted(set(known_keys))[:64])
            extra_rules = (
                "用户记忆库中已有的 key（同一主题的事实必须复用已有 key，仅更新内容；"
                f"例如换手机号仍写 contact.phone）：{keys_text}。"
                "确属全新主题才创建新 key。"
            )
        llm_messages = build_extract_messages(
            transcript, key_examples=_KEY_EXAMPLES, extra_rules=extra_rules
        )
        content, _usage = await self._llm.complete(llm_messages, json_mode=True)
        return self._parse(content)

    @staticmethod
    def _parse(raw: str) -> list[ExtractedFact]:
        """解析并校验 LLM 输出为 ExtractedFact 列表。

        Args:
            raw: LLM 原始输出文本。

        Returns:
            合法事实列表（非法元素逐条跳过并记日志，不整体失败）。

        Raises:
            ExtractionError: 整体不是 JSON 对象或缺 facts 数组。
        """
        data = _load_json_object(raw)
        if not isinstance(data.get("facts"), list):
            raise ExtractionError("输出缺少 facts 数组")
        facts: list[ExtractedFact] = []
        for i, item in enumerate(data["facts"]):
            fact = _to_fact(item)
            if fact is None:
                logger.warning("extract_skip_invalid_fact index=%s", i)
                continue
            facts.append(fact)
        return facts


def _load_json_object(raw: str) -> dict[str, Any]:
    """剥离 markdown 围栏后解析 JSON 对象。

    Raises:
        ExtractionError: 无法解析为 JSON 对象。
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")  # 剥离 ```json / ``` 围栏
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"输出不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionError("输出不是 JSON 对象")
    return data


def _to_fact(item: Any) -> ExtractedFact | None:
    """单个 JSON 元素转 ExtractedFact；不合法返回 None。

    Args:
        item: facts 数组中的单个元素。

    Returns:
        合法事实；字段缺失/类型非法/枚举非法时 None。
    """
    if not isinstance(item, dict):
        return None
    key = item.get("key")
    content = item.get("content")
    key_valid = isinstance(key, str) and bool(key.strip())
    content_valid = isinstance(content, str) and bool(content.strip())
    if not key_valid or not content_valid:
        return None
    try:
        memory_type = MemoryType(str(item.get("memory_type", "")))
    except ValueError:
        return None
    if memory_type not in _VALID_TYPES:
        return None
    confidence = _clamp01(item.get("confidence"))
    importance = _clamp01(item.get("importance"))
    ttl_raw = item.get("ttl_days")
    ttl_days = ttl_raw if isinstance(ttl_raw, int) and ttl_raw > 0 else None
    source_ids: list[uuid.UUID] = []
    for sid in item.get("source_message_ids") or []:
        if isinstance(sid, str):
            try:
                source_ids.append(uuid.UUID(sid))
            except ValueError:
                continue
    return ExtractedFact(
        key=key.strip()[:128],
        content=content.strip(),
        memory_type=memory_type,
        confidence=confidence,
        importance=importance,
        ttl_days=ttl_days,
        source_message_ids=source_ids,
    )


def _clamp01(value: Any) -> float:
    """数值裁剪到 [0, 1]；非法输入返回 0.5（中性值）。"""
    if isinstance(value, int | float) and 0.0 <= float(value) <= 1.0:
        return float(value)
    return 0.5
