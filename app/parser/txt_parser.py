"""
txt_parser.py — 纯文本文件解析器

按行切分文本文件，每一非空行生成一个 Chunk。
额外识别文本中的表格结构（制表符分隔、Markdown 竖线表格），
为每行生成带 structured_row 元数据的 Chunk，与 xlsx_parser 行为一致。
使用 read_text_with_fallback 处理多编码问题。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.io_utils import read_text_with_fallback
from app.schemas import Chunk, ParsedDocument, normalize_text, stable_id


# Markdown 表格行
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|(.+\|)\s*$")
_MD_SEPARATOR_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


def _is_md_table_line(line: str) -> bool:
    return bool(_MD_TABLE_ROW_RE.match(line))


def _is_separator_line(line: str) -> bool:
    return bool(_MD_SEPARATOR_RE.match(line))


def _parse_md_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def _is_tsv_line(line: str) -> bool:
    """检测制表符分隔的行（至少含 1 个 tab，且拆分后至少 2 个非空单元格）。"""
    if "\t" not in line:
        return False
    parts = [p.strip() for p in line.split("\t") if p.strip()]
    return len(parts) >= 2


def _detect_tsv_block(lines: list[str], start: int) -> int:
    """从 start 开始，返回连续 TSV 行的数量；若不足 2 行则返回 0。"""
    count = 0
    col_count = None
    for i in range(start, len(lines)):
        if not _is_tsv_line(lines[i]):
            break
        parts = [p.strip() for p in lines[i].split("\t") if p.strip()]
        if col_count is None:
            col_count = len(parts)
        # 列数差异过大则视为表格结束
        if abs(len(parts) - col_count) > 1:
            break
        count += 1
    return count if count >= 2 else 0


def _extract_md_table_chunks(
    lines: list[str],
    start: int,
    doc_id: str,
    file_path: str,
    table_index: int,
) -> tuple[list[Chunk], int]:
    """提取 Markdown 风格的 | 表格，返回 (chunks, next_line_index)。"""
    headers = _parse_md_row(lines[start])
    pos = start + 1
    if pos < len(lines) and _is_separator_line(lines[pos]):
        pos += 1

    chunks: list[Chunk] = []
    row_index = 0
    while pos < len(lines) and _is_md_table_line(lines[pos]) and not _is_separator_line(lines[pos]):
        cells = _parse_md_row(lines[pos])
        structured_row: dict[str, str] = {}
        for ci, cv in enumerate(cells):
            hdr = headers[ci] if ci < len(headers) else f"列{ci + 1}"
            v = normalize_text(cv)
            if v:
                structured_row[hdr] = v
        if structured_row:
            row_text = " | ".join(f"{k}: {v}" for k, v in structured_row.items())
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:t:{table_index}:r:{row_index}",
                    doc_id=doc_id,
                    text=row_text,
                    metadata={
                        "file_path": file_path,
                        "file_type": "txt",
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


def _extract_tsv_table_chunks(
    lines: list[str],
    start: int,
    count: int,
    doc_id: str,
    file_path: str,
    table_index: int,
) -> list[Chunk]:
    """提取制表符分隔的表格块，第一行为表头，后续为数据行。"""
    header_cells = [c.strip() for c in lines[start].split("\t") if c.strip()]
    headers = [c if c else f"列{i + 1}" for i, c in enumerate(header_cells)]

    chunks: list[Chunk] = []
    for ri in range(1, count):
        row_line = lines[start + ri]
        cells = [c.strip() for c in row_line.split("\t")]
        structured_row: dict[str, str] = {}
        for ci, cv in enumerate(cells):
            hdr = headers[ci] if ci < len(headers) else f"列{ci + 1}"
            v = normalize_text(cv)
            if v:
                structured_row[hdr] = v
        if structured_row:
            row_text = " | ".join(f"{k}: {v}" for k, v in structured_row.items())
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:t:{table_index}:r:{ri - 1}",
                    doc_id=doc_id,
                    text=row_text,
                    metadata={
                        "file_path": file_path,
                        "file_type": "txt",
                        "block_type": "sheet_row",
                        "table_index": table_index,
                        "row_index": ri - 1,
                        "headers": headers,
                        "structured_row": structured_row,
                    },
                )
            )
    return chunks


def parse_txt(path: str) -> ParsedDocument:
    """解析 .txt 文件：按行切分为 Chunk，生成 ParsedDocument。

    额外识别 Markdown 表格（| 语法）和制表符分隔表格，
    为每行生成带 structured_row 的 Chunk。
    """
    target = Path(path)
    text = read_text_with_fallback(target)
    doc_id = stable_id(target.resolve(), target.stat().st_size, int(target.stat().st_mtime))
    file_path = str(target.resolve())

    lines = text.split("\n")
    chunks: list[Chunk] = []
    line_index = 0
    table_index = 0
    i = 0

    while i < len(lines):
        line = lines[i]

        # 检测 Markdown 表格
        if _is_md_table_line(line) and not _is_separator_line(line):
            next_i = i + 1
            is_table = False
            if next_i < len(lines):
                if _is_separator_line(lines[next_i]):
                    is_table = True
                elif _is_md_table_line(lines[next_i]) and not _is_separator_line(lines[next_i]):
                    is_table = True
            if is_table:
                table_chunks, i = _extract_md_table_chunks(
                    lines, i, doc_id, file_path, table_index
                )
                chunks.extend(table_chunks)
                table_index += 1
                continue

        # 检测制表符分隔表格
        if _is_tsv_line(line):
            tsv_count = _detect_tsv_block(lines, i)
            if tsv_count >= 2:
                table_chunks = _extract_tsv_table_chunks(
                    lines, i, tsv_count, doc_id, file_path, table_index
                )
                chunks.extend(table_chunks)
                table_index += 1
                i += tsv_count
                continue

        # 普通行
        value = normalize_text(line)
        if value:
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:l:{line_index}",
                    doc_id=doc_id,
                    text=value,
                    metadata={
                        "file_path": file_path,
                        "file_type": "txt",
                        "block_type": "line",
                        "line_index": line_index,
                    },
                )
            )
        line_index += 1
        i += 1

    return ParsedDocument(
        doc_id=doc_id,
        name=target.name,
        path=str(target),
        file_type="txt",
        text=text,
        chunks=chunks,
        metadata={"last_modified": target.stat().st_mtime},
    )
