"""
simple_chunker.py — 文档块切分器

提供两种切分模式：
  1. chunk_document_blocks(): 固定大小滑动窗口切分（向后兼容）
  2. chunk_two_level():       语义结构双层切分
       - 按文档自然结构（同一表格 / 同一 heading section）分组生成 Parent Chunk
       - 每个原始 block 作为 Chi block 进一步滑动窗口切分
       - Child Chunk 仅用于向量检索；Parent Chunk 作为 LLM 上下文
"""

from __future__ import annotations

import re

from app.schemas import Chunk, stable_id


def _split_sentences(text: str) -> list[str]:
    """按中文句子边界拆分文本，保证每句完整。

    优先级：换行符 > 句末标点（。！？…）> 分号（；;）> 逗号（，,）
    """
    lines = text.splitlines()
    sentences: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = re.split(r'(?<=[。！？…；;])', line)
        for part in parts:
            part = part.strip()
            if part:
                sentences.append(part)
    # 整个文本没有句末标点时，按逗号兜底拆分
    if len(sentences) <= 1:
        sentences = [s.strip() for s in re.split(r'(?<=[，,])', text) if s.strip()]
    if not sentences:
        sentences = [text.strip()]
    return sentences


def chunk_document_blocks(
    blocks: list[Chunk],
    chunk_size: int = 600,
    overlap: int = 80,
) -> list[Chunk]:
    """固定大小滑动窗口切分（原有逻辑，向后兼容）。"""
    if not blocks:
        return []

    final_chunks: list[Chunk] = []
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        if len(text) <= chunk_size:
            final_chunks.append(block)
            continue

        start = 0
        part_index = 0
        while start < len(text):
            end = min(len(text), start + chunk_size)
            piece = text[start:end].strip()
            if piece:
                metadata = dict(block.metadata)
                metadata["parent_chunk_id"] = block.chunk_id
                metadata["subchunk_index"] = part_index
                final_chunks.append(
                    Chunk(
                        chunk_id=f"{block.chunk_id}:sub:{part_index}",
                        doc_id=block.doc_id,
                        text=piece,
                        metadata=metadata,
                    )
                )
                part_index += 1
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)

    return final_chunks


# ---------------------------------------------------------------------------
# 语义结构双层切分（Parent-Child Chunking）
# ---------------------------------------------------------------------------

def _group_by_structure(blocks: list[Chunk]) -> list[list[Chunk]]:
    """
    按文档自然结构将 Chunk 列表分组：
    - table_row（含 table_index）→ 同文档同表格所有行归一组（整张表为一个 Parent）
    - heading / paragraph（含 breadcrumb）→ 同文档同 breadcrumb 连续段落归一组
    - 其他类型（xlsx row 等无结构元数据的块）→ 每块独立成组
    """
    groups: list[list[Chunk]] = []
    current_group: list[Chunk] = []
    current_key: tuple | None = None

    for block in blocks:
        meta = block.metadata
        block_type = meta.get("block_type", "")
        doc_id = block.doc_id

        if block_type in ("table_row", "sheet_row") and "table_index" in meta:
            key: tuple = (doc_id, "table", meta["table_index"])
        elif block_type in ("heading", "paragraph") and "breadcrumb" in meta:
            key = (doc_id, "section", meta.get("breadcrumb", ""))
        else:
            # 无法归类：每块独立成组
            key = (doc_id, "sole", block.chunk_id)

        if key != current_key:
            if current_group:
                groups.append(current_group)
            current_group = [block]
            current_key = key
        else:
            current_group.append(block)

    if current_group:
        groups.append(current_group)

    return groups


def _build_parent_text(group: list[Chunk]) -> str:
    """从一组 Child Chunk 构建 Parent Chunk 的文本内容。"""
    representative = group[0]
    block_type = representative.metadata.get("block_type", "")

    if block_type in ("table_row", "sheet_row"):
        # 优先使用 docx_parser 已生成的整张表格 Markdown（列结构完整）
        md = representative.metadata.get("markdown_table", "")
        if md:
            return md
        # 回退：逐行 raw_text 拼接
        rows = []
        for b in group:
            raw = b.metadata.get("raw_text") or b.text
            if raw.strip():
                rows.append(raw.strip())
        return "\n".join(rows)

    # 段落 / heading：拼接同 section 内所有段落 raw_text
    parts = []
    for b in group:
        raw = b.metadata.get("raw_text") or b.text
        if raw.strip():
            parts.append(raw.strip())
    return "\n\n".join(parts)


def _make_children(
    block: Chunk,
    parent_chunk_id: str,
    child_size: int,
    child_overlap: int,
) -> list[Chunk]:
    """将单个 block 切分为若干 Child Chunk，句子感知：整句贪婪合并，装满再分。

    - 先按句子边界（换行/句号/分号）拆分
    - 贪婪地将句子合并，直到再加一句会超出 child_size 才输出当前块
    - child_overlap 控制相邻块间保留末尾若干字符的重叠上下文
    - 保证任何一句话不被从中间截断
    """
    text = block.text.strip()
    if not text:
        return []

    meta_base = dict(block.metadata)
    meta_base["parent_chunk_id"] = parent_chunk_id

    if len(text) <= child_size:
        return [Chunk(chunk_id=block.chunk_id, doc_id=block.doc_id, text=text, metadata=meta_base)]

    sentences = _split_sentences(text)

    children: list[Chunk] = []
    part_idx = 0
    current_sentences: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal part_idx, current_sentences, current_len
        piece = "".join(current_sentences).strip()
        if piece:
            meta = dict(meta_base)
            meta["subchunk_index"] = part_idx
            children.append(Chunk(
                chunk_id=f"{block.chunk_id}:sub:{part_idx}",
                doc_id=block.doc_id,
                text=piece,
                metadata=meta,
            ))
            part_idx += 1
        current_sentences = []
        current_len = 0

    for sent in sentences:
        sent_len = len(sent)
        # 单句超长：强制独立成块
        if sent_len > child_size:
            if current_sentences:
                _flush()
            meta = dict(meta_base)
            meta["subchunk_index"] = part_idx
            children.append(Chunk(
                chunk_id=f"{block.chunk_id}:sub:{part_idx}",
                doc_id=block.doc_id,
                text=sent.strip(),
                metadata=meta,
            ))
            part_idx += 1
            continue

        if current_len + sent_len > child_size and current_sentences:
            _flush()
            # overlap：从上一块末尾截取若干字符作为当前块的前置上下文
            if child_overlap > 0 and children:
                tail = children[-1].text[-child_overlap:]
                # 回退到最近句子边界，避免截断
                boundary = re.split(r'(?<=[。！？…；;\n，,])', tail)
                tail_sents = [t for t in boundary if t.strip()]
                if tail_sents:
                    current_sentences = tail_sents
                    current_len = sum(len(t) for t in tail_sents)

        current_sentences.append(sent)
        current_len += sent_len

    if current_sentences:
        _flush()

    return children


def chunk_two_level(
    blocks: list[Chunk],
    child_size: int = 300,
    child_overlap: int = 50,
) -> tuple[list[Chunk], dict[str, Chunk]]:
    """
    语义结构双层切分（Parent-Child Chunking）。

    按文档自然结构（同一表格 / 同一 heading section）生成 Parent Chunk，
    原始 block（或过长 block 的滑动窗口片段）作为 Child Chunk。

    Args:
        blocks:        解析器产生的原始 Chunk 列表
        child_size:    Child Chunk 最大字符数（用于向量检索）
        child_overlap: 滑动窗口切分时的字符重叠量

    Returns:
        (child_chunks, parent_lookup)
        - child_chunks:  向量索引用的小 Chunk 列表
        - parent_lookup: parent_chunk_id → Parent Chunk 查找字典
    """
    if not blocks:
        return [], {}

    groups = _group_by_structure(blocks)
    child_chunks: list[Chunk] = []
    parent_lookup: dict[str, Chunk] = {}

    for group in groups:
        if not group:
            continue

        representative = group[0]
        parent_text = _build_parent_text(group)
        if not parent_text.strip():
            continue

        # 多块组用稳定 hash；单块组沿用原 chunk_id 加后缀
        if len(group) > 1:
            parent_id = stable_id(representative.doc_id, representative.chunk_id, "parent")
        else:
            parent_id = f"{representative.chunk_id}:parent"

        parent_meta = dict(representative.metadata)
        parent_meta["is_parent"] = True
        parent_meta["child_count"] = len(group)
        parent_lookup[parent_id] = Chunk(
            chunk_id=parent_id,
            doc_id=representative.doc_id,
            text=parent_text,
            metadata=parent_meta,
        )

        for block in group:
            child_chunks.extend(_make_children(block, parent_id, child_size, child_overlap))

    return child_chunks, parent_lookup
