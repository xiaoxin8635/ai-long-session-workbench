"""文档切片（M-07 摄入链路，docs/06 §9：段落感知语义切片）。

策略：
  - 按空行分段；markdown 标题行（# 开头）强制开启新块（章节边界优先于 token 目标）
  - 段落贪心合并至目标 token 数（软上限，段落完整性优先）
  - 超长单段（无空行的整页文本）退化为句子级贪心切分
  - 相邻块以"块尾段落复用"实现 overlap（默认 10%，保持段落完整；
    标题开新块时不带 overlap——跨章节重叠无意义）
"""

import re

from app.context.tokenizer import count_tokens

# 句子边界：中英文句末标点（超长段落的二级切分单位）
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；.!?;])\s*")


def split_chunks(text: str, *, target_tokens: int = 512, overlap_ratio: float = 0.1) -> list[str]:
    """把纯文本切成目标大小的重叠切片。

    Args:
        text: 解析后的纯文本（段落以空行分隔）。
        target_tokens: 单块目标 token 数（软上限）。
        overlap_ratio: 相邻块重叠比例（块尾段落复用；0 不重叠）。

    Returns:
        切片列表（顺序即 chunk_index 顺序）；空白输入返回空列表。
    """
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return []

    blocks: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for para in paragraphs:
        para_tokens = count_tokens(para)
        is_heading = para.lstrip().startswith("#")

        # 超长单段先做句子级预切，各片段按普通段落参与贪心
        pieces = (
            _split_long_paragraph(para, target_tokens) if para_tokens > target_tokens else [para]
        )

        for piece in pieces:
            piece_tokens = count_tokens(piece)
            if current and (is_heading or current_tokens + piece_tokens > target_tokens):
                blocks.append(current)
                current = [] if is_heading else _overlap_tail(current, target_tokens, overlap_ratio)
                current_tokens = sum(count_tokens(p) for p in current)
            current.append(piece)
            current_tokens += piece_tokens
    if current:
        blocks.append(current)
    return ["\n\n".join(block).strip() for block in blocks if block]


def _paragraphs(text: str) -> list[str]:
    """按空行分段并去空白（连续空行视为一个分隔）。"""
    return [seg.strip() for seg in re.split(r"\n\s*\n", text) if seg.strip()]


def _overlap_tail(block: list[str], target_tokens: int, overlap_ratio: float) -> list[str]:
    """取块尾段落序列作为下一块的开头（累计 token 达重叠目标即止）。"""
    if overlap_ratio <= 0:
        return []
    overlap_tokens = max(int(target_tokens * overlap_ratio), 1)
    tail: list[str] = []
    tail_tokens = 0
    for para in reversed(block):
        tail.insert(0, para)
        tail_tokens += count_tokens(para)
        if tail_tokens >= overlap_tokens:
            break
    return tail


def _split_long_paragraph(para: str, target_tokens: int) -> list[str]:
    """超长段落按句子贪心预切（句子完整性优先，仍超长的单句按字符窗硬切）。

    Args:
        para: 超长段落原文。
        target_tokens: 句子组的目标 token 数（与块目标一致）。

    Returns:
        句子组片段列表（拼接后可还原段落语义）。
    """
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(para) if s.strip()]
    pieces: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0
    for sentence in sentences:
        sentence_tokens = count_tokens(sentence)
        if sentence_tokens > target_tokens:
            # 极端无标点长句：按目标字符数滑窗硬切（CJK 约 1 token/字）
            if buffer:
                pieces.append("".join(buffer))
                buffer, buffer_tokens = [], 0
            pieces.extend(_hard_window(sentence, target_tokens))
            continue
        if buffer and buffer_tokens + sentence_tokens > target_tokens:
            pieces.append("".join(buffer))
            buffer, buffer_tokens = [], 0
        buffer.append(sentence)
        buffer_tokens += sentence_tokens
    if buffer:
        pieces.append("".join(buffer))
    return pieces


def _hard_window(sentence: str, target_tokens: int) -> list[str]:
    """无标点超长句的字符级滑窗硬切（每窗约 target_tokens 个 CJK 字符）。"""
    step = max(target_tokens, 64)  # 防止 target 过小导致碎切
    return [sentence[i : i + step] for i in range(0, len(sentence), step)]
