"""
word_filler.py — Word 模板填写器

填写方式：
  1. 段落中的 {{xxx}} 占位符替换
  2. 段落中的 【xxx】 中文占位符替换（统计公报模板）
  3. 表格中的占位符替换和标签-值对填写
  4. 标签式表格填写（根据行标签 + 列头定位单元格）
  5. 表格数据填写（自动添加行）
"""

from __future__ import annotations

import logging
import re

from docx import Document

logger = logging.getLogger(__name__)

from app.filler.utils import (
    CN_PLACEHOLDER_PATTERN,
    UNDERLINE_BLANK_PATTERN,
    BRACKET_BLANK_PATTERN,
    CN_PAREN_BLANK_PATTERN,
    fill_blanks_in_text,
    has_blank_marker,
    inspect_template,
)
from app.schemas import TemplateLayout, normalize_field_name, normalize_text

import math

# ----------- 模糊匹配工具 -----------

_CN_PH_RE = re.compile(r"\u3010([^\u3011]+)\u3011")   # 匹配 【xxx】
_MUSTACHE_RE = re.compile(r"\{\{([^}]+)\}\}")          # 匹配 {{xxx}}

_CTX_WINDOW = 60  # 上下文窗口：占位符前后各取 60 个字符


def _context_window(text: str, m: re.Match) -> str:
    """提取占位符周围 _CTX_WINDOW 个字符，去掉其他【xxx】，返回纯上下文字符串。"""
    left = text[max(0, m.start() - _CTX_WINDOW):m.start()]
    right = text[m.end():min(len(text), m.end() + _CTX_WINDOW)]
    # 去掉窗口内其他占位符，避免干扰关键词提取
    left = _CN_PH_RE.sub("", left)
    right = _CN_PH_RE.sub("", right)
    return left + right


def _fuzzy_find_value(
    placeholder_text: str,
    values: dict[str, str],
    context: str = "",
    used_keys: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """按优先级在 values 中查找占位符对应的值。

    返回 (matched_key, value)，未找到时返回 (None, None)。
    空值字段也参与 key 匹配（防止误匹配到其他有值字段），但返回 None 表示"有匹配但值为空，保留占位符"。

    策略（按优先级）：
      1 (主)：占位符内文本精确匹配（含空值占位，防止误匹配）。
      2 (主)：normalize 后精确匹配。
      3 (备)：上下文关键词匹配 —— 从占位符前后 _CTX_WINDOW 字符提取短词，
              与每个候选 key 归一化形式比对，重叠词数最多者胜出。多个占位符
              文本相同（如多处"占比%"）时，靠上下文区分。
      4 (兜底)：normalize 后包含匹配，要求重叠比例 ≥ 0.6（取最长 key）。
    """
    if used_keys is None:
        used_keys = set()

    # --- 策略 1 (主)：占位符文本直接精确匹配（含空值，防止误匹配） ---
    if placeholder_text in values:
        key = placeholder_text
        if key in used_keys:
            return None, None
        v = values[key]
        return (key, v) if v else (key, None)

    norm_ph = normalize_field_name(placeholder_text)
    if not norm_ph:
        return None, None

    # --- 策略 2 (主)：归一化后精确匹配 ---
    for key, value in values.items():
        if normalize_field_name(key) == norm_ph:
            if key in used_keys:
                return None, None
            return (key, value) if value else (key, None)

    # --- 策略 3 (备)：上下文关键词匹配 ---
    if context:
        ctx_candidates = {k: v for k, v in values.items() if v and k not in used_keys}
        if ctx_candidates:
            ctx_seqs = [s for s in re.findall(r"[\u4e00-\u9fff]{2,}", context) if len(s) <= 8]
            if ctx_seqs:
                best_key, best_score = None, 0
                for key in ctx_candidates:
                    nk = normalize_field_name(key)
                    score = sum(1 for seq in ctx_seqs if normalize_field_name(seq) in nk)
                    if score > best_score:
                        best_score = score
                        best_key = key
                if best_key and best_score > 0:
                    return best_key, ctx_candidates[best_key]

    # 以下兜底策略只在非空、未使用的 key 中搜索
    candidates = {k: v for k, v in values.items() if v and k not in used_keys}
    if not candidates:
        return None, None

    # --- 策略 4 (兜底)：包含匹配，要求重叠比例 ≥ 0.6（取最长 key） ---
    best_key2, best_len = None, 0
    for key in candidates:
        nk = normalize_field_name(key)
        if not nk or len(nk) < 2:
            continue
        shorter, longer = (norm_ph, nk) if len(norm_ph) <= len(nk) else (nk, norm_ph)
        if shorter in longer:
            overlap_ratio = len(shorter) / len(longer)
            if overlap_ratio >= 0.6 and len(nk) > best_len:
                best_key2 = key
                best_len = len(nk)
    if best_key2:
        return best_key2, candidates[best_key2]

    return None, None

# 模板中常见的尾部单位，用于去重
_UNIT_SUFFIXES = re.compile(
    r'[\s]*([‰%万亿元个人家座处所张枚块封件套台辆节层次床类粇顾条项座次里千米公里日天月年夜小时分钟秒]+)$'
)


def _strip_unit(value: str, context: str) -> str:
    """如果模板上下文已包含该单位，则从 value 尾部去掉它以避免重复。"""
    m = _UNIT_SUFFIXES.search(value)
    if not m:
        return value
    unit = m.group(1)
    # 只有当占位符后紧跟该单位时才去除
    if unit and unit in context:
        return value[: m.start()].rstrip()
    return value


def _safe_str(value: object) -> str:
    """将任意值安全转为字符串。None / NaN / float('inf') 均转为空串。"""
    if value is None:
        return ""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    return str(value).strip()


def fill_word(
    template_path: str,
    records: list[dict[str, object]],
    output_path: str,
    layout: TemplateLayout | None = None,
    single_record: dict[str, object] | None = None,
    table_records: dict[str, list[dict[str, object]]] | None = None,
    cn_values: dict[str, str] | None = None,
    table_cell_values: dict[int, dict[tuple[int, int], str]] | None = None,
) -> None:
    document = Document(template_path)
    layout = layout or inspect_template(template_path)
    single_record = single_record or (records[0] if records else {})
    table_records = table_records or {}
    cn_values = cn_values or {}
    table_cell_values = table_cell_values or {}

    for paragraph in document.paragraphs:
        _replace_text_placeholders(paragraph, single_record)
        if cn_values:
            _replace_cn_placeholders(paragraph, cn_values)
        _replace_blank_placeholders(paragraph, cn_values or single_record)

    # 构建目标表格索引集合，只对目标表格执行通用填写函数，避免误填非目标表格
    target_table_indices = {
        t.table_index for t in layout.table_targets
        if t.template_type == "docx" and t.table_index is not None
    }

    for table_index, table in enumerate(document.tables):
        # 占位符替换（{{xxx}} / 【xxx】）对所有表格都安全，保留
        _replace_table_placeholders(table, single_record)
        if cn_values:
            _replace_table_cn_placeholders(table, cn_values)

        # 以下通用填写函数只对目标表格执行，避免误填非目标表格
        if table_index in target_table_indices:
            _fill_label_value_pairs(table, single_record)
            _replace_table_blank_placeholders(table, cn_values or single_record)
            _fill_table_empty_cells(table, cn_values or single_record)

        # 标签式表格：直接填写指定单元格
        if table_index in table_cell_values:
            _fill_labeled_table_cells(table, table_cell_values[table_index])

        for target in layout.table_targets:
            if target.template_type != "docx" or target.table_index != table_index:
                continue
            # 标签式表格已通过 table_cell_values 填写，跳过记录追加
            if target.metadata.get("labeled"):
                continue
            target_records = table_records.get(target.identifier, records)
            # 使用原始列索引映射，避免过滤空列后列号错位
            header_col_map = target.metadata.get("header_col_map")
            _fill_table(table, target.headers, target_records, start_row=target.start_row, header_col_map=header_col_map)

    document.save(output_path)


def _replace_text_placeholders(paragraph, record: dict[str, object]) -> None:
    text = paragraph.text
    if not text:
        return
    updated = text
    used_keys: set[str] = set()
    # 1) 精确匹配
    for key, value in record.items():
        s = _safe_str(value)
        if not s:
            continue
        placeholder = "{{" + key + "}}"
        if placeholder in updated:
            s = _strip_unit(s, updated)
            updated = updated.replace(placeholder, s)
            used_keys.add(key)
    # 2) 模糊匹配剩余 {{xxx}}
    str_record = {k: _safe_str(v) for k, v in record.items()}
    for m in _MUSTACHE_RE.finditer(updated):
        ph_content = m.group(1)
        full_ph = m.group(0)
        matched_key, val = _fuzzy_find_value(ph_content, str_record, used_keys=used_keys)
        if matched_key is None or val is None or not val:
            continue
        val = _strip_unit(val, updated)
        updated = updated.replace(full_ph, val, 1)
        used_keys.add(matched_key)
    if updated != text:
        paragraph.text = updated


def _replace_table_placeholders(table, record: dict[str, object]) -> None:
    str_record = {k: _safe_str(v) for k, v in record.items()}
    for row in table.rows:
        for cell in row.cells:
            text = cell.text
            updated = text
            used_keys: set[str] = set()
            # 1) 精确匹配
            for key, value in record.items():
                s = _safe_str(value)
                if not s:
                    continue
                placeholder = "{{" + key + "}}"
                if placeholder in updated:
                    s = _strip_unit(s, updated)
                    updated = updated.replace(placeholder, s)
                    used_keys.add(key)
            # 2) 模糊匹配剩余 {{xxx}}
            for m in _MUSTACHE_RE.finditer(updated):
                ph_content = m.group(1)
                full_ph = m.group(0)
                matched_key, val = _fuzzy_find_value(ph_content, str_record, used_keys=used_keys)
                if matched_key is None or val is None or not val:
                    continue
                val = _strip_unit(val, updated)
                updated = updated.replace(full_ph, val, 1)
                used_keys.add(matched_key)
            if updated != text:
                cell.text = updated


def _fill_label_value_pairs(table, record: dict[str, object]) -> None:
    normalized_record = {
        normalize_field_name(key): normalize_text(value)
        for key, value in record.items()
    }
    for row in table.rows:
        for column_index, cell in enumerate(row.cells[:-1]):
            label = normalize_field_name(cell.text)
            if label and label in normalized_record and not normalize_text(row.cells[column_index + 1].text):
                row.cells[column_index + 1].text = normalized_record[label]


def _fuzzy_match_record_key(
    header_key: str,
    record_map: dict[str, str],
) -> str:
    """当精确 normalize 匹配失败时，尝试包含匹配和最长公共子串匹配。

    返回匹配到的值，未匹配返回空串。
    """
    if not header_key:
        return ""
    # 策略1：包含匹配（header_key 包含 record_key 或反之）
    best_key = ""
    best_overlap = 0
    for rk, rv in record_map.items():
        if not rk or not rv:
            continue
        shorter, longer = (header_key, rk) if len(header_key) <= len(rk) else (rk, header_key)
        if shorter in longer:
            overlap = len(shorter) / len(longer)
            if overlap >= 0.5 and len(rk) > best_overlap:
                best_key = rk
                best_overlap = len(rk)
    if best_key:
        return record_map[best_key]
    return ""


def _fill_table(
    table,
    headers: list[str],
    records: list[dict[str, object]],
    start_row: int = 2,
    header_col_map: list[tuple[int, str]] | None = None,
) -> None:
    if not records or not table.rows:
        return

    # 使用原始列索引映射（若有），否则回退到顺序索引
    if header_col_map:
        col_mapping = [(ci, normalize_field_name(h)) for ci, h in header_col_map]
    else:
        col_mapping = [(i, normalize_field_name(h)) for i, h in enumerate(headers)]

    start_index = max(1, start_row - 1)
    existing_rows = max(0, len(table.rows) - start_index)
    for _ in range(max(0, len(records) - existing_rows)):
        table.add_row()

    # 统计各列是否有过成功匹配（用于最后告警）
    column_matched = [False] * len(col_mapping)

    for record_index, record in enumerate(records, start=0):
        row = table.rows[start_index + record_index]
        record_map = {
            normalize_field_name(key): normalize_text(value)
            for key, value in record.items()
        }
        for map_index, (actual_col_index, header_key) in enumerate(col_mapping):
            if actual_col_index >= len(row.cells):
                continue
            value = record_map.get(header_key, "")
            if not value:
                # 模糊回退匹配
                value = _fuzzy_match_record_key(header_key, record_map)
            if value:
                column_matched[map_index] = True
            row.cells[actual_col_index].text = value

    # 对全部记录均未匹配到值的列发出警告
    for mi, (matched, (_, hdr_key)) in enumerate(zip(column_matched, col_mapping)):
        if not matched and hdr_key:
            orig_hdr = headers[mi] if mi < len(headers) else hdr_key
            sample_keys = list(records[0].keys())[:8] if records else []
            logger.warning(
                "[Word填表] 列 '%s' 在所有记录中均未匹配到值，记录字段示例: %s",
                orig_hdr, sample_keys,
            )


def _replace_cn_placeholders(paragraph, cn_values: dict[str, str]) -> None:
    """替换段落中的 【xxx】 占位符为实际值。空值跳过，保留原始占位符。采用模糊匹配。"""
    text = paragraph.text
    if not text or "\u3010" not in text:
        return
    updated = text
    used_keys: set[str] = set()
    for m in _CN_PH_RE.finditer(text):
        ph_content = m.group(1)
        full_ph = m.group(0)
        ctx = _context_window(text, m)
        matched_key, val = _fuzzy_find_value(ph_content, cn_values, context=ctx, used_keys=used_keys)
        if matched_key is None or val is None:
            continue
        s = _safe_str(val)
        if not s:
            continue
        s = _strip_unit(s, updated)
        updated = updated.replace(full_ph, s, 1)
        used_keys.add(matched_key)
    if updated != text:
        paragraph.text = updated


def _replace_table_cn_placeholders(table, cn_values: dict[str, str]) -> None:
    """替换表格单元格中的 【xxx】 占位符为实际值。采用上下文匹配。"""
    if not cn_values:
        return
    used_keys: set[str] = set()
    for row in table.rows:
        for cell in row.cells:
            text = cell.text
            if not text or "\u3010" not in text:
                continue
            updated = text
            for m in _CN_PH_RE.finditer(text):
                ph_content = m.group(1)
                full_ph = m.group(0)
                ctx = _context_window(text, m)
                matched_key, val = _fuzzy_find_value(ph_content, cn_values, context=ctx, used_keys=used_keys)
                if matched_key is None or val is None:
                    continue
                s = _safe_str(val)
                if not s:
                    continue
                s = _strip_unit(s, updated)
                updated = updated.replace(full_ph, s, 1)
                used_keys.add(matched_key)
            if updated != text:
                cell.text = updated


def _replace_blank_placeholders(paragraph, values: dict[str, object]) -> None:
    """替换段落中的 ____、[ ]、（） 空白占位符为匹配到的值。"""
    text = paragraph.text
    if not text:
        return
    updated = fill_blanks_in_text(text, values)
    if updated != text:
        paragraph.text = updated


def _replace_table_blank_placeholders(table, values: dict[str, object]) -> None:
    """替换表格单元格中的 ____/[ ]/（） 空白占位符。"""
    if not values:
        return
    for row in table.rows:
        for cell in row.cells:
            text = cell.text
            if not text or not has_blank_marker(text):
                continue
            updated = fill_blanks_in_text(text, values)
            if updated != text:
                cell.text = updated


def _fill_table_empty_cells(table, values: dict[str, object]) -> None:
    """填写表格中的空单元格：利用行标签 + 列标头在 values 中查找匹配值。

    仅当首列有中文标签、且目标单元格为空时触发。
    """
    if not values or len(table.rows) < 2:
        return
    headers = [cell.text.strip() for cell in table.rows[0].cells]

    for ri in range(1, len(table.rows)):
        row = table.rows[ri]
        row_label = row.cells[0].text.strip() if row.cells else ""
        if not row_label or not re.search(r"[\u4e00-\u9fff]", row_label):
            continue

        for ci in range(1, min(len(headers), len(row.cells))):
            cell = row.cells[ci]
            if cell.text.strip():
                continue
            col_header = headers[ci]
            if not col_header:
                continue
            value = _find_table_cell_value(row_label, col_header, values)
            if value:
                cell.text = value


def _find_table_cell_value(
    row_label: str, col_header: str, values: dict[str, object]
) -> str:
    """根据行标签和列标头在 values 中查找对应值。"""
    norm_label = normalize_field_name(row_label)
    norm_header = normalize_field_name(col_header)
    if not norm_label:
        return ""

    # 优先：key 同时包含行标签和列标头
    for key, value in values.items():
        nk = normalize_field_name(key)
        if norm_label in nk and norm_header and norm_header in nk:
            return normalize_text(value)

    # 次选：key 等于行标签
    for key, value in values.items():
        nk = normalize_field_name(key)
        if nk == norm_label:
            return normalize_text(value)

    return ""


def _fill_labeled_table_cells(
    table,
    cell_values: dict[tuple[int, int], str],
) -> None:
    """填写标签式表格的指定单元格。"""
    for (row_idx, col_idx), value in cell_values.items():
        if row_idx >= len(table.rows):
            continue
        row = table.rows[row_idx]
        if col_idx >= len(row.cells):
            continue
        row.cells[col_idx].text = value
