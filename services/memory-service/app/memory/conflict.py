"""记忆冲突仲裁（M-04 写路径第 ④ 步）。

LLM 判定旧记忆与新事实的关系：
  - MERGE：语义一致 → 合并正文、提升 confidence
  - SUPERSEDE：矛盾且新事实可信 → 旧条目 superseded，新条目挂版本链
  - COEXIST：矛盾但无法判定 → 双条 conflicted，等待用户在记忆面板裁决
  - INDEPENDENT：不矛盾的独立事实 → 双条各自 ACTIVE 并存、不打冲突标
    （同主题并行事实的正确出口，防止 conflicted 误标畸高）

LLM 失败时的保守降级：COEXIST（不丢任何一方信息）。
"""

import json
import logging

from app.llm.client import LLMClient, LLMError
from app.memory.prompts import build_arbitrate_messages
from app.memory.schemas import ConflictAction, ConflictOutcome, ExtractedFact
from app.models.memory import Memory

logger = logging.getLogger(__name__)


class ConflictResolver:
    """新旧记忆冲突的 LLM 仲裁器。"""

    def __init__(self, llm: LLMClient) -> None:
        """注入 LLM 客户端（生产为抽取小模型，测试可替换 fake）。

        Args:
            llm: OpenAI 兼容客户端。
        """
        self._llm = llm

    async def resolve(self, old: Memory, new: ExtractedFact) -> ConflictOutcome:
        """仲裁一条候选事实与既有记忆的关系。

        Args:
            old: 库中的既有记忆条目。
            new: 候选事实。

        Returns:
            仲裁结论；LLM 输出非法或调用失败时降级为 COEXIST（保守不丢信息）。
        """
        messages = build_arbitrate_messages(
            key=old.key,
            old_content=old.content,
            old_confidence=old.confidence,
            new_content=new.content,
            new_confidence=new.confidence,
        )
        try:
            raw, _usage = await self._llm.complete(messages, json_mode=True)
            outcome = self._parse(raw, new)
        except (LLMError, ValueError) as exc:
            logger.warning("arbitrate_fallback_coexist key=%s error=%s", old.key, exc)
            return ConflictOutcome(action=ConflictAction.COEXIST)
        return outcome

    @staticmethod
    def _parse(raw: str, new: ExtractedFact) -> ConflictOutcome:
        """解析仲裁输出；结构非法时抛 ValueError（由调用方降级）。

        Args:
            raw: LLM 原始输出。
            new: 候选事实（MERGE 字段缺省时回退到候选值）。

        Returns:
            仲裁结论。

        Raises:
            ValueError: 输出不是 JSON 对象或 action 非法。
        """
        text = raw.strip().strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("仲裁输出不是 JSON 对象")
        action = ConflictAction(str(data.get("action", "")))
        if action == ConflictAction.MERGE:
            content = data.get("merged_content")
            confidence = data.get("merged_confidence")
            merged = content if isinstance(content, str) and content.strip() else new.content
            return ConflictOutcome(
                action=action,
                merged_content=merged,
                merged_confidence=(
                    float(confidence)
                    if isinstance(confidence, int | float) and 0.0 <= float(confidence) <= 1.0
                    else max(new.confidence, 0.9)
                ),
            )
        return ConflictOutcome(action=action)
