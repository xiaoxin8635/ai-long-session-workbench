"""文档解析（M-07 摄入链路第一步：原始字节 → 纯文本）。

支持 pdf（pypdf）/ docx（python-docx）/ md 与 txt（原样读取）；
其余类型拒绝。解析在上传请求内同步执行（CPU 密集但个人文档量级 <1s）。
"""

import logging

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# 允许上传的扩展名 → file_type 标识
SUPPORTED_TYPES = frozenset({"pdf", "docx", "md", "txt"})

# 单文件大小上限（Settings.rag_max_upload_mb，路由层校验；此处仅防御空文件）
_MIN_BYTES = 1


class ParseError(AppError):
    """文档解析失败（损坏/加密/空内容），上传请求 422 返回。"""

    def __init__(self, message: str) -> None:
        """初始化解析错误（统一 422，客户端文件问题）。"""
        super().__init__("knowledge_parse_error", 422, message)


def parse_document(filename: str, data: bytes) -> tuple[str, str]:
    """按扩展名解析原始字节为纯文本。

    Args:
        filename: 原始文件名（取扩展名分派解析器）。
        data: 文件原始字节。

    Returns:
        (file_type, 纯文本) 元组；段落结构由空行保留（供 chunker 分段）。

    Raises:
        ParseError: 类型不支持、文件为空或解析失败。
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_TYPES:
        raise ParseError(f"不支持的文件类型 .{ext}（支持 pdf/docx/md/txt）")
    if len(data) < _MIN_BYTES:
        raise ParseError("文件内容为空")

    try:
        if ext == "pdf":
            return ext, _parse_pdf(data)
        if ext == "docx":
            return ext, _parse_docx(data)
        return ext, _parse_plain(data)
    except ParseError:
        raise
    except Exception as exc:  # pypdf/python-docx 对损坏文件抛各种底层异常
        logger.warning("knowledge_parse_failed file=%s error=%s", filename, exc)
        raise ParseError(f"文档解析失败：{exc}") from exc


def _parse_pdf(data: bytes) -> str:
    """PDF 逐页抽文本，页间空行分隔（保留段落边界供切片）。"""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ParseError("PDF 已加密，无法解析")
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(page.strip() for page in pages if page.strip())
    if not text.strip():
        raise ParseError("PDF 未抽取到文本（可能为扫描件）")
    return text


def _parse_docx(data: bytes) -> str:
    """docx 按段落抽取，段间空行分隔。"""
    import io

    from docx import Document

    document = Document(io.BytesIO(data))
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    if not paragraphs:
        raise ParseError("docx 未抽取到文本")
    return "\n\n".join(paragraphs)


def _parse_plain(data: bytes) -> str:
    """md/txt 解码为 UTF-8 文本（BOM 兼容；失败提示重存编码）。"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("文本编码非 UTF-8，请另存为 UTF-8 后重试") from exc
