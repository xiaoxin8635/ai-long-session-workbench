"""ContextBuilder：token 预算内的上下文统一装配（M-05，docs/01 §5.3 / docs/06 §7）。

两层职责：
  - assemble()：纯函数装配——各区块内按策略裁剪 → 全局按 evict_order 防线
    → 渲染为 OpenAI messages（一条合成 system + working 原文消息）
  - build()：高层取数编排——滚动摘要 + M-04 记忆检索 + M-10 任务简报 +
    M-07 RAG 检索 + Working Memory 窗口汇成候选集后调用 assemble；
    各取数源独立降级，任一失败不阻断装配。

装配顺序固定（docs/01 §5.3 要点 2）：
  system → procedural(偏好) → semantic(事实+任务简报) → episodic(摘要)
  → working(近期) → rag(知识) → tool_results(工具结果)
"""

import logging
import uuid
from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.budget import ContextBudget, load_budget
from app.context.schemas import (
    AssembledContext,
    ContextCandidates,
    ContextItem,
    SectionKey,
    SectionUsage,
)
from app.context.tokenizer import count_tokens
from app.core.config import get_settings
from app.llm.embeddings import EmbeddingError, get_embedding_client
from app.llm.rerank import get_rerank_client
from app.memory.retriever import MemoryRetriever
from app.memory.working_memory import WorkingMemory
from app.models.enums import MemoryType, TaskStatus
from app.observability import tracing
from app.rag.retriever import KnowledgeRetriever
from app.rag.schemas import CitedChunk
from app.repositories import memory_repo, message_repo, session_repo, task_repo

logger = logging.getLogger(__name__)

# 基础系统提示（角色 + 记忆使用说明；system 区块固定注入不裁剪）
DEFAULT_SYSTEM_PROMPT = (
    "你是 EchoDesk，一个具备长期记忆的 AI 工作台助手。\n"
    "系统会在下方以结构化标签注入跨会话记忆（<memory> 为用户长期事实与偏好，"
    "<summary> 为历史会话摘要，<knowledge> 为知识库片段）。\n"
    "使用规则：把这些记忆当作关于用户的可靠背景，自然地用于回答；"
    "当记忆与用户最新表述冲突时，以用户当前消息为准；不要提及记忆系统的存在。"
)

# 各记忆区块渲染用的 (标签名, 包裹标签) 映射
_SECTION_TAGS: dict[SectionKey, tuple[str, str]] = {
    SectionKey.PROCEDURAL: ("memory", "procedural_memories"),
    SectionKey.SEMANTIC: ("memory", "semantic_memories"),
    SectionKey.EPISODIC: ("summary", "session_summaries"),
    SectionKey.RAG: ("knowledge", "knowledge_chunks"),
    SectionKey.TOOL_RESULTS: ("tool_result", "tool_results"),
}

# 区块包裹标签内的行为引导语（渲染在首条之前；评测优化轮补丁——长对话
# 末轮 probe 时偏好条目注入了但模型未遵守，显式指令提醒后遵循率显著提升）
_WRAPPER_HINTS: dict[SectionKey, str] = {
    SectionKey.PROCEDURAL: (
        "以下为用户的持久偏好与约定（作息时段、可用时长、沟通风格、优先级等），"
        "制定计划、安排日程与给建议时必须逐条遵守。"
    ),
}

# 检索结果 memory_type → 注入区块的映射
_RETRIEVAL_BUCKETS: dict[MemoryType, SectionKey] = {
    MemoryType.PROCEDURAL: SectionKey.PROCEDURAL,
    MemoryType.SEMANTIC: SectionKey.SEMANTIC,
    MemoryType.EPISODIC: SectionKey.EPISODIC,
}

# 按 score 裁剪的记忆类区块
_SCORED_SECTIONS = (
    SectionKey.PROCEDURAL,
    SectionKey.SEMANTIC,
    SectionKey.EPISODIC,
    SectionKey.RAG,
    SectionKey.TOOL_RESULTS,
)


class ContextBuilder:
    """上下文装配器：候选裁剪 + 结构化渲染 + 高层取数编排。"""

    def __init__(
        self,
        working_memory: WorkingMemory,
        retriever: MemoryRetriever | None = None,
        knowledge: KnowledgeRetriever | None = None,
    ) -> None:
        """注入 Working Memory 与两个检索器。

        Args:
            working_memory: 会话消息窗口（working 区块数据源）。
            retriever: 长期记忆检索器；None 表示检索不可用（降级为摘要+窗口）。
            knowledge: RAG 知识检索器（M-07）；None 表示知识库未接入
                （RAG 区块恒空，测试隔离用）。
        """
        self._wm = working_memory
        self._retriever = retriever
        self._knowledge = knowledge

    # ---- 纯函数装配（单测主战场，不依赖任何外部状态） ----

    @staticmethod
    def assemble(candidates: ContextCandidates, budget: ContextBudget) -> AssembledContext:
        """按区块预算装配候选集为最终 messages。

        Args:
            candidates: 全部候选片段（各区块独立）。
            budget: 绝对 token 预算。

        Returns:
            装配结果（messages + 各区块用量明细 + 总量）。

        Notes:
            区块内淘汰——记忆类按 score 降序装满即止；working 从最旧滑窗
            （始终保留末条即当前问题）。全局防线——总量超 available 时按
            evict_order 从前往后整区块放弃（working 只剩末条），system 永不裁剪。
        """
        kept: dict[SectionKey, list[Any]] = {
            SectionKey.SYSTEM: [candidates.system_prompt],
            SectionKey.WORKING: list(candidates.working),
            SectionKey.PROCEDURAL: [],
            SectionKey.SEMANTIC: [],
            SectionKey.EPISODIC: [],
            SectionKey.RAG: [],
            SectionKey.TOOL_RESULTS: [],
        }
        for key in _SCORED_SECTIONS:
            items: tuple[ContextItem, ...] = getattr(candidates, key.value)
            kept[key] = ContextBuilder._pack_scored(items, budget.sections[key])
        kept[SectionKey.WORKING] = ContextBuilder._pack_working(
            kept[SectionKey.WORKING], budget.sections[SectionKey.WORKING]
        )

        usage = ContextBuilder._usage(candidates, kept, budget)
        ContextBuilder._enforce_global(kept, usage, budget)
        sections = tuple(usage[key] for key in SectionKey)
        return AssembledContext(
            messages=ContextBuilder._render(kept),
            sections=sections,
            total_tokens=sum(u.tokens for u in sections),
        )

    @staticmethod
    def _pack_scored(items: tuple[ContextItem, ...], budget_tokens: int) -> list[ContextItem]:
        """记忆类区块裁剪：score 降序装满即止（返回按分数降序）。

        Args:
            items: 区块候选。
            budget_tokens: 区块预算（<0 视为不限制）。

        Returns:
            装入的条目列表（超预算部分丢弃）。
        """
        if budget_tokens < 0:
            return list(items)
        kept: list[ContextItem] = []
        used = 0
        for item in sorted(items, key=lambda i: i.score, reverse=True):
            tokens = count_tokens(item.content)
            if used + tokens > budget_tokens:
                continue  # 跳过放不下的，继续尝试更小的
            kept.append(item)
            used += tokens
        return kept

    @staticmethod
    def _pack_working(entries: list[dict[str, Any]], budget_tokens: int) -> list[dict[str, Any]]:
        """working 区块裁剪：从最旧开始滑窗，始终保留末条（当前问题）。

        Args:
            entries: 窗口消息（时间正序）。
            budget_tokens: working 预算。

        Returns:
            预算内的尾部消息（时间正序；至少一条）。
        """
        if not entries:
            return []
        kept = list(entries)
        while len(kept) > 1:
            used = sum(count_tokens(str(e.get("content", ""))) for e in kept)
            if used <= budget_tokens:
                break
            kept.pop(0)  # 丢最旧
        return kept

    @staticmethod
    def _enforce_global(
        kept: dict[SectionKey, list[Any]],
        usage: dict[SectionKey, SectionUsage],
        budget: ContextBudget,
    ) -> None:
        """全局防线：总量超 available 时按 evict_order 整区块放弃。

        原地修改 kept 与 usage；working 区块放弃时保留末条（当前问题不可丢，
        已只剩末条时跳过，防止无进展死循环）。
        """
        while sum(u.tokens for u in usage.values()) > budget.available_tokens:
            victim = next(
                (
                    key
                    for key in budget.evict_order
                    if kept.get(key) and not (key is SectionKey.WORKING and len(kept[key]) <= 1)
                ),
                None,
            )
            if victim is None:
                return  # 无可放弃区块（system 永不裁剪），保留现状
            kept[victim] = kept[victim][-1:] if victim is SectionKey.WORKING else []
            usage[victim] = ContextBuilder._section_usage(
                victim, kept[victim], budget.sections[victim]
            )

    # ---- 用量统计与渲染 ----

    @staticmethod
    def _section_usage(key: SectionKey, kept: list[Any], budget_tokens: int) -> SectionUsage:
        """构造单区块用量（kept 为装入条目；token 按渲染口径统计）。"""
        if key is SectionKey.WORKING:
            tokens = sum(count_tokens(str(e.get("content", ""))) for e in kept)
        else:
            tokens = sum(count_tokens(str(e)) for e in kept)
        return SectionUsage(
            key=key, budget_tokens=budget_tokens, included=len(kept), dropped=0, tokens=tokens
        )

    @staticmethod
    def _usage(
        candidates: ContextCandidates,
        kept: dict[SectionKey, list[Any]],
        budget: ContextBudget,
    ) -> dict[SectionKey, SectionUsage]:
        """全部区块用量（dropped = 候选数 − 装入数）。"""
        usage: dict[SectionKey, SectionUsage] = {}
        for key in SectionKey:
            if key is SectionKey.SYSTEM:
                candidate_count = 1  # system_prompt 恒为一条
            elif key is SectionKey.WORKING:
                candidate_count = len(candidates.working)
            else:
                candidate_count = len(getattr(candidates, key.value))
            u = ContextBuilder._section_usage(key, kept[key], budget.sections[key])
            usage[key] = SectionUsage(
                key=key,
                budget_tokens=u.budget_tokens,
                included=u.included,
                dropped=max(candidate_count - u.included, 0),
                tokens=u.tokens,
            )
        return usage

    @staticmethod
    def _render(kept: dict[SectionKey, list[Any]]) -> list[dict[str, str]]:
        """渲染为 OpenAI messages：合成 system + working 原文。

        system 内容 = 系统提示 + 各区块结构化标签（可溯源）；
        working 区块按原 role 逐条输出（末条即当前问题）。
        """
        parts: list[str] = [str(kept[SectionKey.SYSTEM][0])]
        for key in _SCORED_SECTIONS:  # procedural → … → tool_results 固定顺序
            parts.extend(ContextBuilder._render_tagged(key, kept[key]))
        messages = [{"role": "system", "content": "\n\n".join(parts)}]
        for entry in kept[SectionKey.WORKING]:
            messages.append(
                {
                    "role": str(entry.get("role", "user")),
                    "content": str(entry.get("content", "")),
                }
            )
        return messages

    @staticmethod
    def _render_tagged(key: SectionKey, items: list[Any]) -> list[str]:
        """把一个记忆类区块渲染为包裹标签 + 逐条结构化标签（空区块返回空）。

        配置了引导语的区块（见 _WRAPPER_HINTS）在包裹标签内首行注入行为指令。
        """
        if not items:
            return []
        tag, wrapper = _SECTION_TAGS[key]
        if tag == "memory":
            lines = [
                f'<memory type="{key.value}" source="{item.source}">{item.content}</memory>'
                for item in items
                if isinstance(item, ContextItem)
            ]
        else:
            lines = [
                f'<{tag} source="{item.source}">{item.content}</{tag}>'
                for item in items
                if isinstance(item, ContextItem)
            ]
        head = [f"<{wrapper}>"]
        hint = _WRAPPER_HINTS.get(key)
        if hint:
            head.append(hint)
        return [*head, *lines, f"</{wrapper}>"]

    # ---- 高层取数编排（ChatService 主链路调用） ----

    async def build(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        query: str,
        profile: str | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> AssembledContext:
        """取数 + 装配：摘要/记忆检索/窗口 → 候选集 → assemble。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            user_id: 归属用户（检索隔离）。
            session_id: 当前会话（窗口与摘要来源）。
            query: 当前用户问题（记忆检索的查询）。
            profile: 预算 profile；None 用配置默认。
            system_prompt: 系统提示覆盖（默认 DEFAULT_SYSTEM_PROMPT）。

        Returns:
            装配结果；检索/窗口失败自动降级，绝不抛出阻断对话。
        """
        budget = load_budget(profile)
        episodic: list[ContextItem] = []
        buckets: dict[SectionKey, list[ContextItem]] = {
            SectionKey.PROCEDURAL: [],
            SectionKey.SEMANTIC: [],
            SectionKey.EPISODIC: [],
            SectionKey.RAG: [],
            SectionKey.TOOL_RESULTS: [],
        }

        # ① 本会话滚动摘要（新鲜必达，score 最高）
        session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=session_id)
        if session is not None and session.rolling_summary:
            episodic.append(
                ContextItem(
                    content=session.rolling_summary, source=f"session:{session_id}", score=1.0
                )
            )

        # ② 长期记忆检索（不可用降级为空，主对话不受影响）
        if self._retriever is not None and query:
            try:
                with tracing.span("memory.retrieve") as obs:
                    hits = await self._retriever.retrieve(
                        db, ws_id=ws_id, user_id=user_id, query=query
                    )
                    obs.update(
                        metadata={
                            "query": tracing.snippet(query, 100),
                            "hits": len(hits),
                        }
                    )
            except EmbeddingError as exc:
                logger.warning("context_retrieval_degraded error=%s", exc)
                hits = []
            for scored in hits:
                section = _RETRIEVAL_BUCKETS.get(scored.memory.memory_type)
                if section is None:
                    continue
                # 本会话摘要已直取注入，跳过检索命中的同 key 条目防重复
                if scored.memory.key == memory_repo.summary_memory_key(session_id):
                    continue
                buckets[section].append(
                    ContextItem(
                        content=scored.memory.content,
                        source=scored.memory.key,
                        score=scored.score,
                    )
                )
        buckets[SectionKey.EPISODIC].extend(episodic)

        # ②.2 任务简报（M-10 Task Continuity）：仅在会话早期注入（消息总数
        # ≤ 阈值，覆盖"新会话开场接着干"场景）；之后让位于真正的记忆检索，
        # 避免固定简报长期挤占 semantic 桶的 top 名额（M-13 评测 Recall 根因）。
        # score 0.85 低于 episodic 摘要 1.0，预算紧张时优先被裁剪。
        try:
            total_messages = await message_repo.count_by_session(db, session_id=session_id)
            if total_messages <= get_settings().task_brief_max_session_messages:
                for task in await task_repo.unfinished_brief(
                    db, ws_id=ws_id, limit=get_settings().task_brief_limit
                ):
                    buckets[SectionKey.SEMANTIC].append(
                        ContextItem(
                            content=(
                                f"未完成任务「{task.title}」"
                                f"（状态 {TaskStatus(task.status).value}，优先级 {task.priority}）"
                            ),
                            source=f"task:{task.id}",
                            score=0.85,
                        )
                    )
        except Exception as exc:  # 任务简报失败不阻断装配（与检索降级同口径）
            logger.warning("context_task_brief_degraded error=%s", exc)

        # ②.5 RAG 知识检索（M-07；不可用降级为空，主对话不受影响）
        rag_hits: list[CitedChunk] = []
        if self._knowledge is not None and query:
            try:
                with tracing.span("rag.search") as obs:
                    rag_hits = await self._knowledge.search(db, ws_id=ws_id, query=query)
                    obs.update(
                        metadata={
                            "query": tracing.snippet(query, 100),
                            "hits": len(rag_hits),
                        }
                    )
            except Exception as exc:
                logger.warning("context_rag_degraded error=%s", exc)
                rag_hits = []
        for cited in rag_hits:
            buckets[SectionKey.RAG].append(
                ContextItem(
                    content=cited.content,
                    source=rag_source(cited),
                    score=cited.score,
                )
            )

        # ③ Working Memory 窗口（按 working 预算截断）
        working = await self._wm.window(session_id, budget.sections[SectionKey.WORKING])

        candidates = ContextCandidates(
            system_prompt=system_prompt,
            procedural=tuple(buckets[SectionKey.PROCEDURAL]),
            semantic=tuple(buckets[SectionKey.SEMANTIC]),
            episodic=tuple(buckets[SectionKey.EPISODIC]),
            working=tuple(working),
            rag=tuple(buckets[SectionKey.RAG]),
            tool_results=tuple(buckets[SectionKey.TOOL_RESULTS]),
        )
        assembled = ContextBuilder.assemble(candidates, budget)
        # citations 只保留真正装入 RAG 区块的命中（预算裁剪后不引用未注入内容；
        # _pack_scored 为纯函数，同输入重放即可还原装配时的取舍）
        kept_sources = {
            item.source
            for item in ContextBuilder._pack_scored(candidates.rag, budget.sections[SectionKey.RAG])
        }
        citations = tuple(cited for cited in rag_hits if rag_source(cited) in kept_sources)
        return replace(assembled, citations=citations)


def rag_source(cited: CitedChunk) -> str:
    """构造 RAG 命中的溯源标记（注入 <knowledge source=...> 与 citations 对齐）。"""
    return f"knowledge:{cited.filename}#chunk{cited.chunk_index}"


def default_builder(working_memory: WorkingMemory) -> ContextBuilder:
    """构造默认装配器（embedding 未配置时记忆检索禁用，降级为摘要+窗口）。

    Args:
        working_memory: Working Memory 窗口实例。

    Returns:
        可直接用于对话链路 / preview 端点的 ContextBuilder。
    """
    try:
        embedding_client = get_embedding_client()
    except Exception:  # EmbeddingError：未配置属正常部署形态，仅记提示
        logger.info("context_embedding_not_configured")
        embedding_client = None
    retriever = MemoryRetriever(embedding_client) if embedding_client is not None else None
    try:
        rerank_client = get_rerank_client()
    except Exception:  # RerankError：未配置降级 RRF 直排
        rerank_client = None
    # BM25 不依赖外部服务，知识检索恒启用（向量/rerank 各自按可用性降级）
    knowledge = KnowledgeRetriever(embedding=embedding_client, rerank=rerank_client)
    return ContextBuilder(working_memory, retriever, knowledge)
