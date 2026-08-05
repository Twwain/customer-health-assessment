"""文档解析（SOW §3.3.2）。

支持文本型 PDF / Markdown / TXT / XLSX / CSV / DOCX 的文本抽取。
重型依赖（PyMuPDF / python-docx）按需 lazy import，缺失时抛出 ``ParseError`` 并提示安装方式；
md / txt / csv 走标准库，零依赖，保证开发与测试环境可用性。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass


class ParseError(Exception):
    """文档解析失败。"""


@dataclass
class ParsedDocument:
    text: str
    source: str  # 使用的解析器名称，便于排查


def _parse_plain(raw: bytes) -> str:
    # md / txt：直接读文本
    return raw.decode("utf-8", errors="replace")


def _parse_csv(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def _parse_pdf(raw: bytes | None, path: str | None) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - 依赖可选
        raise ParseError("解析 PDF 需要 PyMuPDF：pip install pymupdf") from exc
    doc = fitz.open(stream=raw, filetype="pdf") if raw is not None else fitz.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _parse_xlsx(raw: bytes | None, path: str | None) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ParseError("解析 XLSX 需要 openpyxl（已包含在 requirements）") from exc
    src: io.BytesIO | str = io.BytesIO(raw) if raw is not None else path  # type: ignore[assignment]
    wb = load_workbook(filename=src, data_only=True, read_only=True)
    chunks: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            line = "\t".join(cells).strip()
            if line:
                chunks.append(line)
    return "\n".join(chunks)


def _parse_docx(raw: bytes | None, path: str | None) -> str:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover
        raise ParseError("解析 DOCX 需要 python-docx：pip install python-docx") from exc
    src: io.BytesIO | str = io.BytesIO(raw) if raw is not None else path  # type: ignore[assignment]
    document = Document(src)
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())


def parse_document(
    filename: str,
    raw: bytes | None = None,
    path: str | None = None,
) -> ParsedDocument:
    """按扩展名选择解析器。优先使用 ``raw`` 字节，否则读取 ``path``。"""
    if raw is None and path is None:
        raise ParseError("parse_document 需要 raw 字节或文件路径")
    src_path = path or filename
    ext = os.path.splitext(src_path)[1].lower()
    try:
        if ext in (".md", ".markdown", ".txt"):
            return ParsedDocument(_parse_plain(raw or b""), "plain")
        if ext == ".csv":
            return ParsedDocument(_parse_csv(raw or b""), "csv")
        if ext == ".pdf":
            return ParsedDocument(_parse_pdf(raw, path), "pymupdf")
        if ext in (".xlsx", ".xlsm"):
            return ParsedDocument(_parse_xlsx(raw, path), "openpyxl")
        if ext == ".docx":
            return ParsedDocument(_parse_docx(raw, path), "python-docx")
    except ParseError:
        raise
    except Exception as exc:  # pragma: no cover - 解析内部异常统一包装
        raise ParseError(f"解析 {src_path} 失败：{exc}") from exc
    raise ParseError(f"不支持的文件类型：{ext or '未知'}")
