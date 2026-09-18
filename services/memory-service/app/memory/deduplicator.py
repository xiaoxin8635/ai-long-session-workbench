"""记忆去重（M-04 写路径第 ③ 步）。

两条路径（先精确后相似，key 命中即返回）：
  1. workspace + key 精确匹配 active 记忆
  2. 向量余弦相似度 ≥ 阈值（默认 0.92）的 active 记忆（复用 HNSW 索引）

命中即交由 ConflictResolver 仲裁（一致→MERGE / 矛盾→SUPERSEDE 或 COEXIST）。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.memory.schemas import Duplicate
from app.repositories import memory_repo


class MemoryDeduplicator:
    """候选事实与既有记忆的重复检测器。"""

    async def find_duplicate(
        self, db: AsyncSession, *, ws_id: uuid.UUID, key: str, embedding: list[float] | None
    ) -> Duplicate | None:
        """查找与候选重复的既有记忆。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            key: 候选事实的 key。
            embedding: 候选向量（embedding 服务不可用时 None，仅走 key 路径）。

        Returns:
            命中的重复（带相似度）；无重复 None。
        """
        exact = await memory_repo.find_active_by_key(db, ws_id=ws_id, key=key)
        if exact is not None:
            return Duplicate(memory=exact, similarity=1.0)
        if embedding is None:
            return None  # 无向量时降级为仅 key 判重
        settings = get_settings()
        neighbors = await memory_repo.find_similar(
            db,
            ws_id=ws_id,
            embedding=embedding,
            threshold=settings.dedup_similarity_threshold,
            limit=1,
        )
        if neighbors:
            return Duplicate(memory=neighbors[0][0], similarity=neighbors[0][1])
        return None
