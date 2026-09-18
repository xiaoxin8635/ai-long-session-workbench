"""滚动摘要 prompt 模板（docs/06 §M-06）。

两级 prompt：
  - 增量合并：旧摘要 + 新滑出对话段 → 新摘要（常规路径）
  - 二级压缩：摘要自身超预算时对摘要再压缩（防摘要无限膨胀）
"""

# 增量合并系统提示词：{old_summary} 为已有摘要（首次为"（暂无）"），
# 用户消息为待并入的滑出对话段（逐条 role: content 原文）
INCREMENTAL_SYSTEM = """\
你是对话摘要助手。请把"已有摘要"与"新增对话段"合并为一份**新的完整摘要**。

要求：
- 保留双方的全部关键信息：用户提到的事实、偏好、决定、约定、任务与结论
- 冲突时以新增对话段中的最新说法为准
- 用简洁的中文条目式书写，不要复述寒暄与过程性内容
- 直接输出摘要正文，不要任何前后缀说明

已有摘要：
{old_summary}"""

# 二级压缩系统提示词：摘要本身过长时使用，用户消息为待压缩的摘要原文
COMPRESS_SYSTEM = """\
以下是一份会话摘要，因超出长度预算需要压缩。请输出压缩后的版本：
保留全部关键事实与结论，去掉次要细节，长度约为原文的一半以内。
直接输出压缩后的摘要正文，不要任何说明。

待压缩摘要如下："""


def build_incremental_messages(
    old_summary: str | None, evicted: list[dict[str, str]]
) -> list[dict[str, str]]:
    """构造增量合并的 LLM 输入消息。

    Args:
        old_summary: 已有滚动摘要；None 表示首次生成。
        evicted: 从 Working Memory 窗口滑出的消息（时间正序，role/content）。

    Returns:
        OpenAI 格式消息列表（system + 单条 user）。
    """
    system = INCREMENTAL_SYSTEM.format(old_summary=old_summary or "（暂无）")
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in evicted)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"新增对话段：\n{transcript}"},
    ]


def build_compress_messages(summary: str) -> list[dict[str, str]]:
    """构造二级压缩的 LLM 输入消息（摘要自身超预算时）。

    Args:
        summary: 待压缩的摘要原文。

    Returns:
        OpenAI 格式消息列表（system + 单条 user）。
    """
    return [
        {"role": "system", "content": COMPRESS_SYSTEM},
        {"role": "user", "content": summary},
    ]
