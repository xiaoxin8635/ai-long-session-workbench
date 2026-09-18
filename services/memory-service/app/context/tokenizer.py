"""token 计数（v1 启发式估算；自 app/services/token_counter.py 迁入，M-05 归口上下文域）。

策略（显式声明的估算，非精确分词器）：
  - CJK 字符（含扩展 A、假名、谚文）按 1 token/字计
  - 其余文本按 4 字符 ≈ 1 token（英文平均词长口径）
  - 与 cl100k 类分词器相比误差约 ±20%，满足会话统计与预算展示用途

不引入 tiktoken：其在容器内首次使用需出网下载 BPE 编码文件（离线/受限网络
不可靠），且全服务（消息统计/滚动摘要/工作窗口/上下文预算）统一本口径，
误差一致性比绝对精度更重要。后续如需精确计数，仅替换本实现。
"""

import re

# CJK 统一表意文字 + 扩展 A + 假名 + 谚文（中日韩混排常见范围）
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")


def count_tokens(text: str) -> int:
    """返回文本的 token 估算值（≥1，空串为 0）。

    Args:
        text: 待计数文本。

    Returns:
        token 估算值；空串返回 0。
    """
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    other_len = len(text) - cjk_count
    return cjk_count + (other_len + 3) // 4  # ceil(other/4)
