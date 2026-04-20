"""
docx_parser.py — Word 文档解析器

解析 .docx 文件，生成供 RAG 使用的 Chunk 列表。

核心逻辑：
  1. 统一遍历 document.element.body 子节点（<w:p> / <w:tbl>），
     保持段落与表格的原始文档顺序。
  2. 按 Heading 层级维护 heading_stack（压栈/弹栈），
     生成面包屑（breadcrumb）字符串注入每个 Chunk。
  3. 表格行遍历时用 id(cell._tc) 去重，避免合并单元格导致内容重复。
  4. 表格首行通过启发式判断是否为表头：
     非空单元格中纯数字占比 >= 60% 则视为数据行，列名回退为 "列1"、"列2"… 形式。
  5. 每个 Chunk 的 metadata 含 breadcrumb 字段；
     text 字段前注入 "[breadcrumb]\\n" 前缀（breadcrumb 为空时不注入）；
     metadata 中保留 raw_text 存储不含前缀的原始文本。
  6. 表格行 Chunk 的 metadata 含 markdown_table 字段，
     存储该行所属完整表格的 Markdown 格式，供 LLM prompt 使用。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
import re

from docx import Document

from app.schemas import Chunk, ParsedDocument, normalize_text, stable_id

# .docx 中可选的 XML 部件：.rels 可能声明了这些，但文件可能缺失
# 对应的最小合法 XML 占位符（命名空间与 OOXML 规范一致）
_OPTIONAL_PART_STUBS: dict[str, bytes] = {
    "word/footnotes.xml": (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
    ),
    "word/endnotes.xml": (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:endnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
    ),
    "word/comments.xml": (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
    ),
    "word/settings.xml": (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
    ),
    "word/webSettings.xml": (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:webSettings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
    ),
    "word/fontTable.xml": (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
    ),
}


def _load_document_patched(path: str) -> Document:
    """加载 .docx，若 ZIP 包缺少可选部件则自动补全后再加载。"""
    try:
        return Document(path)
    except KeyError:
        pass

    # 读取原始 ZIP，在内存中补全缺失的可选部件
    with open(path, "rb") as fh:
        raw = fh.read()

    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        existing = set(zin.namelist())
        for name in existing:
            zout.writestr(name, zin.read(name))
        for part, stub in _OPTIONAL_PART_STUBS.items():
            if part not in existing:
                zout.writestr(part, stub)

    buf.seek(0)
    return Document(buf)


def _build_doc_id(path: Path) -> str:
    """根据文件路径、大小、修改时间生成稳定的文档 ID。"""
    stat = path.stat()
    return stable_id(path.resolve(), stat.st_size, int(stat.st_mtime))


def _normalize_date_text(value: str) -> str:
    text = normalize_text(value)
    direct = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if direct:
        year, month, day = direct.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    chinese = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if chinese:
        year, month, day = chinese.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return ""


def _get_heading_level(style_name: str) -> int | None:
    """从样式名中解析 heading 层级，如 'Heading 2' → 2。非 heading 返回 None。"""
    match = re.match(r"heading\s*(\d+)", style_name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _is_numeric_str(text: str) -> bool:
    """判断文本去空白后是否为纯数字（含小数、负号、百分号）。"""
    cleaned = text.strip().rstrip("%‰")
    if not cleaned:
        return False
    try:
        float(cleaned.replace(",", "").replace("，", ""))
        return True
    except ValueError:
        return False


def _detect_headers(table: Table) -> tuple[list[str], int]:
    """检测表格表头（支持多级合并表头）。

    返回 (flat_headers, header_row_count)。
    - flat_headers 长度 = 物理列数（row.cells 含合并单元格重复）
    - 多级表头用 " > " 拼接层级，如 "性别分布 > 男"
    - header_row_count = 0 表示无表头（首行即为数据）
    """
    if not table.rows:
        return [], 0

    # 物理列数 = row.cells 的长度（合并单元格会重复，恰好保持列索引对齐）
    phys_col_count = len(table.rows[0].cells)

    # 逐行扫描，判断是否为表头行
    header_rows_texts: list[list[str]] = []
    for row in table.rows[:4]:  # 最多检查前 4 行
        # 注意：这里用 row.cells（不去重），保持物理列数
        texts = [normalize_text(cell.text) for cell in row.cells]
        non_empty = [t for t in texts if t]
        if not non_empty:
            break  # 全空行，表头结束
        numeric_count = sum(1 for t in non_empty if _is_numeric_str(t))
        if numeric_count / len(non_empty) >= 0.6:
            break  # 多数为数字，数据行开始
        header_rows_texts.append(texts)

    if not header_rows_texts:
        # 无表头
        return [f"列{i + 1}" for i in range(phys_col_count)], 0

    header_row_count = len(header_rows_texts)

    # 单行表头：直接返回（兼容原有简单表格）
    if header_row_count == 1:
        flat = [
            header_rows_texts[0][ci] if header_rows_texts[0][ci] else f"列{ci + 1}"
            for ci in range(phys_col_count)
        ]
        return flat, 1

    # 多行表头：逐列合并层级，跳过与上一层相同的文本（合并单元格重复）
    flat_headers: list[str] = []
    for ci in range(phys_col_count):
        parts: list[str] = []
        for ri in range(header_row_count):
            text = header_rows_texts[ri][ci] if ci < len(header_rows_texts[ri]) else ""
            # 跳过空文本和与前一层级相同的文本（合并单元格纵向重复）
            if text and (not parts or text != parts[-1]):
                parts.append(text)
        flat_headers.append(" > ".join(parts) if parts else f"列{ci + 1}")

    return flat_headers, header_row_count


def _dedup_row_cells(row) -> list:
    """对一行的 cells 按 id(cell._tc) 去重，保持顺序。"""
    seen: set[int] = set()
    result = []
    for cell in row.cells:
        tc_id = id(cell._tc)
        if tc_id not in seen:
            seen.add(tc_id)
            result.append(cell)
    return result


def _dedup_row_values(row) -> list[str]:
    """对一行的 cells 按 id(cell._tc) 去重，返回文本列表。

    与 _dedup_row_cells 不同，这里返回 str 列表，
    同时记录每个去重后单元格跨越的物理列数（用于补齐列索引）。
    """
    seen: set[int] = set()
    result: list[str] = []
    for cell in row.cells:
        tc_id = id(cell._tc)
        if tc_id not in seen:
            seen.add(tc_id)
            result.append(normalize_text(cell.text))
    return result


def _physical_row_values(row, phys_col_count: int) -> list[str]:
    """按物理列数读取一行的文本，保持与 headers 等长的列索引对齐。

    row.cells 对合并单元格会重复返回同一个 _tc，长度 = 物理列数。
    """
    texts = [normalize_text(cell.text) for cell in row.cells]
    # 补齐（防御性）
    while len(texts) < phys_col_count:
        texts.append("")
    return texts[:phys_col_count]


def _build_markdown_table(headers: list[str], data_rows: list[list[str]]) -> str:
    """将表头和数据行构建为 Markdown 表格字符串。

    headers 可能包含多级表头（用 " > " 分隔），会完整保留在 Markdown 中。
    """
    col_count = len(headers)
    header_row = "| " + " | ".join(h or " " for h in headers) + " |"
    sep_row = "| " + " | ".join("---" for _ in range(col_count)) + " |"
    lines = [header_row, sep_row]
    for row_vals in data_rows:
        # 补齐列数
        padded = list(row_vals) + [""] * (col_count - len(row_vals))
        lines.append("| " + " | ".join(padded[:col_count]) + " |")
    return "\n".join(lines)


def _make_breadcrumb(heading_stack: list[str]) -> str:
    """将 heading_stack 拼接为面包屑字符串。"""
    return " > ".join(heading_stack) if heading_stack else ""


def _inject_breadcrumb(raw_text: str, breadcrumb: str) -> str:
    """在文本前注入面包屑前缀。breadcrumb 为空时返回原文。"""
    if breadcrumb:
        return f"[{breadcrumb}]\n{raw_text}"
    return raw_text


def _iter_body_elements(document: Document):
    """
    按文档顺序遍历 body 的子节点，yield (element_type, obj)。
    element_type 为 'paragraph' 或 'table'。
    使用 python-docx 已构造好的对象（带完整 parent/part 链），
    通过 XML 元素 id 做查找匹配，避免手动构造导致 .part 缺失。
    """
    body = document.element.body

    # 预建查找表：XML element id → python-docx 对象
    para_lookup: dict[int, object] = {id(p._p): p for p in document.paragraphs}
    table_lookup: dict[int, object] = {id(t._tbl): t for t in document.tables}

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "p":
            para = para_lookup.get(id(child))
            if para is not None:
                yield "paragraph", para
        elif tag == "tbl":
            tbl_obj = table_lookup.get(id(child))
            if tbl_obj is not None:
                yield "table", tbl_obj


def parse_docx(path: str) -> ParsedDocument:
    """解析 .docx 文件：按文档顺序提取段落 + 表格行，生成 ParsedDocument。"""
    target = Path(path)
    doc_id = _build_doc_id(target)
    document = _load_document_patched(path)

    chunks: list[Chunk] = []
    full_text: list[str] = []
    heading_stack: list[str] = []  # [(title, ...)] 按层级维护
    _heading_levels: list[int] = []  # 与 heading_stack 等长，记录每层 level
    entity_context = ""
    date_context = ""
    entity_pattern = re.compile(
        r"^([\u4e00-\u9fffA-Za-z·]{2,}(?:省|市|自治区|回族自治区|维吾尔自治区|壮族自治区|特别行政区))"
    )
    date_pattern = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")

    paragraph_counter = 0
    table_counter = 0

    for elem_type, elem_obj in _iter_body_elements(document):
        if elem_type == "paragraph":
            paragraph = elem_obj
            text = normalize_text(paragraph.text)
            if not text:
                paragraph_counter += 1
                continue

            style_name = normalize_text(getattr(paragraph.style, "name", ""))
            heading_level = _get_heading_level(style_name)

            # --- Bug 1 修复：按层级维护 heading_stack ---
            if heading_level is not None:
                # 弹出所有 >= 当前 level 的条目
                while _heading_levels and _heading_levels[-1] >= heading_level:
                    _heading_levels.pop()
                    heading_stack.pop()
                heading_stack.append(text)
                _heading_levels.append(heading_level)

            entity_match = entity_pattern.match(text)
            if entity_match:
                entity_context = entity_match.group(1)
            date_match = date_pattern.search(text)
            if date_match:
                date_context = _normalize_date_text(date_match.group(0))

            breadcrumb = _make_breadcrumb(heading_stack)
            display_text = _inject_breadcrumb(text, breadcrumb)

            full_text.append(text)
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:p:{paragraph_counter}",
                    doc_id=doc_id,
                    text=display_text,
                    metadata={
                        "file_path": str(target.resolve()),
                        "file_type": "docx",
                        "block_type": "heading" if heading_level is not None else "paragraph",
                        "paragraph_index": paragraph_counter,
                        "section_title": heading_stack[-1] if heading_stack else "",
                        "entity_context": entity_context,
                        "date_context": date_context,
                        "breadcrumb": breadcrumb,
                        "raw_text": text,
                    },
                )
            )
            paragraph_counter += 1

        elif elem_type == "table":
            table = elem_obj
            if not table.rows:
                table_counter += 1
                continue

            headers, header_row_count = _detect_headers(table)
            first_data_row_idx = header_row_count if header_row_count > 0 else 0
            # 物理列数（headers 长度 = row.cells 长度，含合并单元格重复）
            phys_col_count = len(headers)

            # --- 预处理：收集所有数据行（按物理列数，保持列索引对齐） ---
            data_rows_texts: list[list[str]] = []
            for row in table.rows[first_data_row_idx:]:
                row_values = _physical_row_values(row, phys_col_count)
                data_rows_texts.append(row_values)

            # --- 生成 markdown_table（包含多级表头层级信息） ---
            markdown_table = _build_markdown_table(headers, data_rows_texts)
            breadcrumb = _make_breadcrumb(heading_stack)

            for local_row_idx, row_values in enumerate(data_rows_texts):
                if not any(row_values):
                    continue

                # 构建 structured_row，按物理列索引与 headers 一一对应
                structured_row = {
                    headers[ci]: row_values[ci]
                    for ci in range(phys_col_count)
                    if row_values[ci]
                }
                raw_row_text = " | ".join(
                    f"{key}: {value}"
                    for key, value in structured_row.items()
                )
                display_text = _inject_breadcrumb(raw_row_text, breadcrumb)

                # row_index = 实际行号（header_row_count 之后开始）
                row_index = first_data_row_idx + local_row_idx

                full_text.append(raw_row_text)
                chunks.append(
                    Chunk(
                        chunk_id=f"{doc_id}:t:{table_counter}:r:{row_index}",
                        doc_id=doc_id,
                        text=display_text,
                        metadata={
                            "file_path": str(target.resolve()),
                            "file_type": "docx",
                            "block_type": "table_row",
                            "table_index": table_counter,
                            "row_index": row_index,
                            "headers": headers,
                            "structured_row": structured_row,
                            "section_title": heading_stack[-1] if heading_stack else "",
                            "breadcrumb": breadcrumb,
                            "raw_text": raw_row_text,
                            "markdown_table": markdown_table,
                        },
                    )
                )

            table_counter += 1

    return ParsedDocument(
        doc_id=doc_id,
        name=target.name,
        path=str(target),
        file_type="docx",
        text="\n".join(full_text),
        chunks=chunks,
        metadata={
            "last_modified": target.stat().st_mtime,
        },
    )
