"""token 计数（v1 启发式估算，docs/06 §M-03 数据流「token 计数」）。

策略（显式声明的估算，非精确分词器）：
  - CJK 字符（含扩展 A、假名、谚文）按 1 token/字计
  - 其余文本按 4 字符 ≈ 1 token（英文平均词长口径）
  - 与 cl100k 类分词器相比误差约 ±20%，满足会话统计与预算展示用途

M-08 接入真实 LLM 客户端后，将替换为模型对应 tokenizer 的精确计数
（接口不变，仅升级内部实现）。
"""
import re

# CJK 统一表意文字 + 扩展 A + 假名 + 谚文（中日韩混排常见范围）
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")


def count_tokens(text: str) -> int:
    """返回文本的 token 估算值（≥1，空串为 0）。"""
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    other_len = len(text) - cjk_count
    return cjk_count + (other_len + 3) // 4  # ceil(other/4)
