"""RAG 中文分词（M-07，索引侧与查询侧统一口径）。

jieba cut_for_search（搜索引擎模式，粒度更细利于召回）；
过滤单空白与标点类 token；全半角统一由 jieba 内部处理。
"""

import re

import jieba

# 分词后过滤：纯空白、纯标点符号（中英文）
_NOISE_RE = re.compile(r"^[\W_]+$", re.UNICODE)

# 进程内只初始化一次（jieba 线程安全，重复 init 无害但浪费）
_initialized = False


def _ensure_init() -> None:
    """惰性初始化分词器词典（首次调用加载默认词典，约百毫秒）。"""
    global _initialized
    if not _initialized:
        jieba.initialize()
        _initialized = True


def tokenize_for_index(text: str) -> list[str]:
    """把文本切为索引/查询共用的 token 列表。

    Args:
        text: chunk 正文或用户查询。

    Returns:
        过滤空白与标点后的 token 列表（保持出现顺序）。
    """
    _ensure_init()
    return [
        token
        for token in jieba.cut_for_search(text)
        if token.strip() and not _NOISE_RE.match(token)
    ]
