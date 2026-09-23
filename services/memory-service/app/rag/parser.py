"""文档解析（M-07 摄入链路第一步：原始字节 → 纯文本）。

支持 pdf（pypdf）/ docx（python-docx）/ csv（stdlib）/ xlsx（openpyxl）/
md 与 txt（原样读取）；其余类型拒绝。解析在上传请求内同步执行
（CPU 密集但个人文档量级 <1s）。

表格类（csv/xlsx）统一渲染为「表头 + 逐行 `列名: 值`」的纯文本，兼顾
BM25 分词命中与 LLM 可读性（避免裸 CSV 逗号流丢失列语义）。
"""

import logging

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# 允许上传的扩展名 → file_type 标识
SUPPORTED_TYPES = frozenset({"pdf", "docx", "md", "txt", "csv", "xlsx"})

# 单文件切片数上限（表格类可能行数巨大，防御性截断避免撑爆摄取管线）
_MAX_TABLE_ROWS = 5000

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
        raise ParseError(f"不支持的文件类型 .{ext}（支持 pdf/docx/md/txt/csv/xlsx）")
    if len(data) < _MIN_BYTES:
        raise ParseError("文件内容为空")

    try:
        if ext == "pdf":
            return ext, _parse_pdf(data)
        if ext == "docx":
            return ext, _parse_docx(data)
        if ext == "csv":
            return ext, _parse_csv(data)
        if ext == "xlsx":
            return ext, _parse_xlsx(data)
        return ext, _parse_plain(data)
    except ParseError:
        raise
    except Exception as exc:  # pypdf/python-docx/openpyxl 对损坏文件抛各种底层异常
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


def _render_table(header: list[str], rows: list[list[str]], *, sheet: str | None = None) -> str:
    """把表格渲染为「表头 + 逐行 `列名: 值`」纯文本（csv/xlsx 共用）。

    Args:
        header: 表头列名。
        rows: 数据行（已字符串化）。
        sheet: 工作表名（xlsx 多表时作为小节标题；csv 为 None）。

    Returns:
        纯文本；每行形如 `列名1: 值1 | 列名2: 值2`，缺列名的位置用「列N」。
    """
    width = max([len(header), *(len(r) for r in rows)] or [0])
    cols = [
        (header[i].strip() if i < len(header) and header[i].strip() else f"列{i + 1}")
        for i in range(width)
    ]
    lines: list[str] = []
    if sheet:
        lines.append(f"# 工作表：{sheet}")
    if cols:
        lines.append(" | ".join(cols))
    for row in rows:
        cells = [str(c).strip() if c is not None else "" for c in row]
        pairs = [f"{cols[i]}: {v}" for i, v in enumerate(cells) if i < len(cols) and v]
        if pairs:
            lines.append(" | ".join(pairs))
    return "\n".join(lines)


def _parse_csv(data: bytes) -> str:
    """CSV 解析：多编码尝试（utf-8-sig → gbk → latin-1）后渲染为表格文本。"""
    import csv
    import io

    text: str | None = None
    for encoding in ("utf-8-sig", "gbk", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ParseError("CSV 编码无法识别，请另存为 UTF-8 后重试")

    reader = csv.reader(io.StringIO(text))
    all_rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not all_rows:
        raise ParseError("CSV 未解析到有效数据行")
    header, rows = all_rows[0], all_rows[1:_MAX_TABLE_ROWS]
    return _render_table([str(c) for c in header], [[str(c) for c in r] for r in rows])


def _parse_xlsx(data: bytes) -> str:
    """xlsx 解析：openpyxl 只读模式逐表渲染，多表以空行分隔。"""
    import io

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sections: list[str] = []
    try:
        for worksheet in workbook.worksheets:
            grid = [
                ["" if cell is None else str(cell) for cell in row]
                for row in worksheet.iter_rows(values_only=True)
            ]
            grid = [row for row in grid if any(c.strip() for c in row)]
            if not grid:
                continue
            header, rows = grid[0], grid[1:_MAX_TABLE_ROWS]
            sections.append(_render_table(header, rows, sheet=worksheet.title))
    finally:
        workbook.close()
    if not sections:
        raise ParseError("xlsx 未解析到有效数据行")
    return "\n\n".join(sections)
