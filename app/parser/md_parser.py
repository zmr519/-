"""
md_parser.py — Markdown 文件解析器

按双换行​分割文本块，每个非空块生成一个 Chunk。
识别 Markdown 表格（| col1 | col2 | 语法），为每行生成带
structured_row 元数据的 Chunk，与 xlsx_parser 行为一致。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.io_utils import read_text_with_fallback
from app.schemas import Chunk, ParsedDocument, normalize_text, stable_id


# Markdown 表格行：以 | 开头或包含至少两个 | 分隔的内容
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|(.+\|)\s*$")
# 分隔行：仅含 |、-、:、空格
_MD_SEPARATOR_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


def _parse_md_table_row(line: str) -> list[str]:
    """解析一行 Markdown 表格，返回各单元格文本（去前后空白）。"""
    cells = line.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def _is_md_table_line(line: str) -> bool:
    return bool(_MD_TABLE_ROW_RE.match(line))


def _is_separator_line(line: str) -> bool:
    return bool(_MD_SEPARATOR_RE.match(line))


def _extract_table_chunks(
    lines: list[str],
    start_line: int,
    doc_id: str,
    file_path: str,
    table_index: int,
) -> tuple[list[Chunk], int]:
    """从 lines[start_line] 开始，提取一个完整的 Markdown 表格。

    返回 (chunks, next_line_index)。
    每个数据行生成一个带 structured_row 的 Chunk。
    """
    # 第一行是表头
    headers = _parse_md_table_row(lines[start_line])
    pos = start_line + 1

    # 跳过分隔行 (| --- | --- |)
    if pos < len(lines) and _is_separator_line(lines[pos]):
        pos += 1

    chunks: list[Chunk] = []
    row_index = 0
    while pos < len(lines) and _is_md_table_line(lines[pos]) and not _is_separator_line(lines[pos]):
        cells = _parse_md_table_row(lines[pos])
        structured_row: dict[str, str] = {}
        for col_idx, cell_value in enumerate(cells):
            header = headers[col_idx] if col_idx < len(headers) else f"列{col_idx + 1}"
            value = normalize_text(cell_value)
            if value:
                structured_row[header] = value

        if structured_row:
            row_text = " | ".join(f"{k}: {v}" for k, v in structured_row.items())
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:t:{table_index}:r:{row_index}",
                    doc_id=doc_id,
                    text=row_text,
                    metadata={
                        "file_path": file_path,
                        "file_type": "md",
                        "block_type": "sheet_row",
                        "table_index": table_index,
                        "row_index": row_index,
                        "headers": headers,
                        "structured_row": structured_row,
                    },
                )
            )
            row_index += 1
        pos += 1

    return chunks, pos


def parse_md(path: str) -> ParsedDocument:
    """解析 .md 文件：按双换行切分为 Chunk，生成 ParsedDocument。

    额外识别 Markdown 表格语法，为每行生成带 structured_row 的 Chunk。
    """
    target = Path(path)
    text = read_text_with_fallback(target)
    doc_id = stable_id(target.resolve(), target.stat().st_size, int(target.stat().st_mtime))
    file_path = str(target.resolve())

    lines = text.split("\n")
    chunks: list[Chunk] = []
    block_index = 0
    table_index = 0
    i = 0

    while i < len(lines):
        # 检测 Markdown 表格起始：当前行是表格行，且下一行是分隔行或也是表格行
        if _is_md_table_line(lines[i]) and not _is_separator_line(lines[i]):
            next_i = i + 1
            is_table = False
            if next_i < len(lines):
                if _is_separator_line(lines[next_i]):
                    is_table = True
                elif _is_md_table_line(lines[next_i]) and not _is_separator_line(lines[next_i]):
                    is_table = True

            if is_table:
                table_chunks, i = _extract_table_chunks(
                    lines, i, doc_id, file_path, table_index
                )
                chunks.extend(table_chunks)
                table_index += 1
                continue

        # 普通段落：收集连续非空、非表格行
        para_lines: list[str] = []
        while i < len(lines):
            line = lines[i]
            # 空行表示段落结束
            if not line.strip():
                i += 1
                break
            # 如果遇到表格起始，也结束当前段落
            if _is_md_table_line(line) and not _is_separator_line(line):
                next_i = i + 1
                if next_i < len(lines) and (_is_separator_line(lines[next_i]) or
                        (_is_md_table_line(lines[next_i]) and not _is_separator_line(lines[next_i]))):
                    break
            para_lines.append(line)
            i += 1

        if para_lines:
            value = normalize_text("\n".join(para_lines))
            if value:
                chunks.append(
                    Chunk(
                        chunk_id=f"{doc_id}:b:{block_index}",
                        doc_id=doc_id,
                        text=value,
                        metadata={
                            "file_path": file_path,
                            "file_type": "md",
                            "block_type": "markdown_block",
                            "block_index": block_index,
                        },
                    )
                )
                block_index += 1

    return ParsedDocument(
        doc_id=doc_id,
        name=target.name,
        path=str(target),
        file_type="md",
        text=text,
        chunks=chunks,
        metadata={"last_modified": target.stat().st_mtime},
    )
