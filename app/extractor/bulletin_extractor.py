"""
bulletin_extractor.py — 通用模板直接抽取器

针对使用 【xxx】 中文占位符的模板，直接从源文档文本中
通过正则匹配提取数值。不依赖向量检索，比通用 RAG 管线更精准。

三种抽取策略：
  1. 段落对齐法：利用占位符前后的模板文字作为锚点精确定位
  2. 结构模式法：处理"占比%"、"增长%"、"每千人口"等通用复合结构
  3. 字段名搜索法：用字段名及其变体在源文本中搜索
"""

from __future__ import annotations

import re

CN_PLACEHOLDER_RE = re.compile(r"【([^【】]+?)】")

# 表格中的通用填写标记（不含实质性字段描述）
GENERIC_TABLE_MARKERS = {"填写", "合计填写", "合计", "合计变化"}

def _extract_keywords(text: str, max_len: int = 6) -> list[str]:
    """从文本中提取搜索关键词，将过长的中文连续串拆分成短段以提高匹配率。"""
    raw = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    result: list[str] = []
    seen: set[str] = set()
    for kw in raw:
        if len(kw) <= max_len:
            if kw not in seen:
                result.append(kw)
                seen.add(kw)
        else:
            # 滑动窗口拆分：窗口4字符，步长2
            for i in range(0, len(kw) - 3, 2):
                seg = kw[i : i + 4]
                if seg not in seen:
                    result.append(seg)
                    seen.add(seg)
    return result


# 运行时字段名别名映射（模板简称 → 源文本全称），由调用方注入
_field_aliases: dict[str, list[str]] = {}


def parse_placeholder_hint(hint: str) -> tuple[str, str]:
    """
    解析占位符提示文本，返回 (字段名, 单位)。
    示例: '人均预期寿命，单位：岁' → ('人均预期寿命', '岁')
    """
    parts = re.split(r"[，,]", hint.strip())
    field_name = parts[0].strip()
    unit = ""
    for part in parts[1:]:
        m = re.search(r"单位[：:]\s*(.+)", part.strip())
        if m:
            unit = m.group(1).strip()
    if not unit and field_name.endswith("%"):
        unit = "%"
        field_name = field_name[:-1].rstrip()
    return field_name, unit


def set_field_aliases(aliases: dict[str, list[str]]) -> None:
    """注入字段名别名映射（模板简称 → 源文本全称）。"""
    global _field_aliases
    _field_aliases = dict(aliases)


def extract_paragraph_values(
    paragraphs: list[str],
    source_text: str,
    *,
    field_aliases: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """
    对模板中每个段落的 【xxx】 占位符，从源文本中提取对应值。

    策略（优先级从高到低）：
    1. 结构模式法：处理占比%、增长%、每千人口等通用复合结构
    2. 字段名搜索法：用字段名及其变体在定位后的文本区域中搜索
    3. 段落对齐法：利用占位符前后的模板文字作为锚点定位（兜底）

    Args:
        field_aliases: 可选字段别名映射，用于模板简称→源文本全称的对应。
    """
    if field_aliases is not None:
        set_field_aliases(field_aliases)

    result: dict[str, str] = {}

    for para in paragraphs:
        matches = list(CN_PLACEHOLDER_RE.finditer(para))
        if not matches:
            continue

        # 预备：段落对齐结果（仅作兜底使用）
        aligned = _align_paragraph(para, source_text)

        # 用段落上下文关键词定位源文本区域
        context = CN_PLACEHOLDER_RE.sub("", para)
        keywords = _extract_keywords(context)
        scope = _find_scope(source_text, keywords)

        for m in matches:
            ph = m.group(1)
            if ph in result or ph in GENERIC_TABLE_MARKERS:
                continue
            field_name, unit = parse_placeholder_hint(ph)

            # 年份字段：在源文本开头区域找年份
            if "年份" in field_name or "年度" in field_name:
                year_m = re.search(r"(\d{4})年[^\n]{0,20}统计", source_text)
                if not year_m:
                    year_m = re.search(r"(\d{4})年", source_text[:500])
                result[ph] = year_m.group(1) if year_m else ""
                continue

            value = ""

            # 优先级 1：特殊模式（最精准）
            if not value:
                value = _try_special_patterns(scope, field_name, unit, para, m.start())
            if not value:
                value = _try_special_patterns(source_text, field_name, unit, para, m.start())

            # 优先级 2：字段名搜索（在定位后的区域中）
            if not value:
                value = _search_value(scope, field_name, unit)
            if not value and scope != source_text:
                value = _search_value(source_text, field_name, unit)

            # 优先级 3：段落对齐（兜底）
            if not value and ph in aligned:
                value = aligned[ph]

            if value:
                result[ph] = value

    return result


def extract_table_values(
    rows: list[list[str]],
    headers: list[str],
    source_text: str,
    context_text: str = "",
) -> dict[tuple[int, int], str]:
    """
    为标签式表格（第一列为行标签，其余列含 【填写】）提取各单元格的值。
    """
    result: dict[tuple[int, int], str] = {}

    ctx_keywords = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}", context_text)]
    scope = _find_scope(source_text, ctx_keywords, window=1200) if ctx_keywords else source_text

    for ri in range(1, len(rows)):
        row = rows[ri]
        label = _clean_label(row[0] if row else "")
        if not label:
            continue

        for ci in range(1, min(len(headers), len(row))):
            cell = row[ci]
            if "【" not in cell:
                continue

            header = headers[ci]
            value = _extract_cell_value(scope, source_text, label, header)
            if value:
                result[(ri, ci)] = value

    return result


# ─── 段落对齐提取 ──────────────────────────────────────────────


def _align_paragraph(para_text: str, source_text: str) -> dict[str, str]:
    """
    通过段落对齐从源文本中提取值。
    将模板段落按 【】 拆分，用前后文字片段作为锚点定位数值。
    """
    parts = CN_PLACEHOLDER_RE.split(para_text)
    if len(parts) < 3:
        return {}

    text_segs = parts[0::2]   # 文本段
    ph_names = parts[1::2]    # 占位符名

    result: dict[str, str] = {}

    for i, ph in enumerate(ph_names):
        if ph in GENERIC_TABLE_MARKERS:
            continue

        before_seg = text_segs[i] if i < len(text_segs) else ""
        after_seg = text_segs[i + 1] if i + 1 < len(text_segs) else ""

        before_anchors = _extract_anchors_tail(before_seg)
        after_anchors = _extract_anchors_head(after_seg)

        value = _find_number_between(source_text, before_anchors, after_anchors)
        if value:
            result[ph] = value

    return result


def _extract_anchors_tail(text: str) -> list[str]:
    """从文本尾部提取锚点词组（占位符的前文锚点）。"""
    phrases = re.findall(r"[\u4e00-\u9fffA-Za-z]+", text)
    return phrases[-3:] if phrases else []


def _extract_anchors_head(text: str) -> list[str]:
    """从文本头部提取锚点词组（占位符的后文锚点）。"""
    phrases = re.findall(r"[\u4e00-\u9fffA-Za-z]+", text)
    return phrases[:2] if phrases else []


def _find_number_between(
    text: str,
    before_anchors: list[str],
    after_anchors: list[str],
) -> str:
    """在 before 锚点之后、after 锚点之前找到数值。"""
    if not before_anchors:
        return ""

    anchor = before_anchors[-1]
    flex = _make_flex_pattern(anchor)
    for b_match in re.finditer(flex, text):
        start = b_match.end()
        window = text[start:start + 60]
        num_m = re.search(r"([\d,]+(?:\.\d+)?)", window)
        if not num_m:
            continue
        num_val = num_m.group(1).replace(",", "")

        if after_anchors:
            check_start = start + num_m.end()
            check_window = text[check_start:check_start + 40]
            a_flex = _make_flex_pattern(after_anchors[0])
            if re.search(a_flex, check_window):
                return num_val
            unit_words = re.findall(r"[\u4e00-\u9fff]+", after_anchors[0])
            if unit_words and unit_words[0] in check_window[:10]:
                return num_val
        else:
            return num_val

    return ""


def _make_flex_pattern(text: str) -> str:
    """将文本转为灵活的正则模式，允许字符间有标点和空格。"""
    chars = list(text)
    parts = [re.escape(c) for c in chars]
    return r"[，,：:；;、\s]*".join(parts)


# ─── 特殊模式提取 ──────────────────────────────────────────────


def _try_special_patterns(
    text: str,
    field_name: str,
    unit: str,
    para_text: str,
    placeholder_pos: int,
) -> str:
    """处理通用复合字段模式（占比、增长、每千/万人口等）。"""
    fn = field_name.strip()

    # 模式 1: "XXX占比%" — 搜索 "XXX...占N%"
    pct_match = re.match(r"(.+?)(?:支出)?占比$", fn)
    if pct_match:
        label = pct_match.group(1)
        return _search_pct_after_label(text, label)

    # 模式 2: 通用"占比"（无具体标签） — 用段落上下文定位
    if fn == "占比":
        return _extract_contextual_pct(text, para_text, placeholder_pos)

    # 模式 3: "占XXX比重" — 搜索 "占XXX...比重...N%"（如"占GDP比重"）
    ratio_m = re.match(r"占(.+?)比重$", fn)
    if ratio_m:
        target = ratio_m.group(1)
        p = rf"占\s*{re.escape(target)}[^\d]{{0,20}}([\d,.]+)\s*[%％]"
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).replace(",", "")

    # 模式 4: "增加万人数/增加亿人次/增加万人次" — 用前文上下文定位
    if fn.startswith("增加") and len(fn) > 2:
        return _extract_contextual_increase(text, fn, para_text, placeholder_pos)

    # 模式 5: "增长百分比" 或 "增长%" — 用前文上下文定位
    if fn.startswith("增长") and len(fn) >= 2:
        return _extract_contextual_growth(text, fn, para_text, placeholder_pos)

    # 模式 6: "每千人口XXX" / "每万人口XXX" — 处理"由X增加到Y"结构
    if fn.startswith("每千人口") or fn.startswith("每万人口"):
        return _extract_per_capita(text, fn, unit)

    return ""


def _search_pct_after_label(text: str, label: str) -> str:
    """搜索 label 后面的占比百分数（非贪婪匹配，找最近的占比）。"""
    escaped = re.escape(label)
    # 非贪婪匹配，找标签后最近的"占N%"
    p = f"{escaped}[^\\n]{{0,80}}?占\\s*([\\d,.]+)\\s*[%％]"
    m = re.search(p, text)
    if m:
        return m.group(1).replace(",", "")
    return ""


def _extract_contextual_pct(text: str, para_text: str, pos: int) -> str:
    """从段落上下文中提取通用'占N%'的值（用于无具体标签的占比字段）。"""
    # 提取占位符前面文字中的关键词定位
    before_text = para_text[:pos]
    context_words = re.findall(r"[\u4e00-\u9fff]{2,}", before_text)
    context_words = context_words[-3:]
    scope = _find_scope(text, context_words, window=400)

    # 搜索"占N%"
    m = re.search(r"占\s*([\d,.]+)\s*[%％]", scope)
    if m:
        return m.group(1).replace(",", "")
    return ""


def _extract_per_capita(text: str, field_name: str, unit: str) -> str:
    """提取'每千人口/每万人口'类字段的值，处理'由X增加到Y'结构。"""
    # 提取内容部分
    prefix_match = re.match(r"(每[千万]人口)(.*)", field_name)
    if not prefix_match:
        return ""
    prefix = prefix_match.group(1)
    suffix = prefix_match.group(2)

    for short, fulls in _field_aliases.items():
        if short in suffix:
            suffix = suffix.replace(short, fulls[0])
            break

    # 构建搜索模式，允许中间有额外文字
    escaped_prefix = re.escape(prefix)
    escaped_suffix = re.escape(suffix)
    gap = r"[\u4e00-\u9fff（）]{0,12}"
    number = r"([\d,]+(?:\.\d+)?)"

    # 优先：查找"增加到YYYY年N"或"到YYYY年N"结构（取最新年份的值）
    p = f"{escaped_prefix}{gap}{escaped_suffix}[^\\n]{{0,50}}(?:增加到|到)\\d{{4}}年\\s*{number}"
    m = re.search(p, text)
    if m:
        return m.group(1).replace(",", "")

    # 次选：直接查找 prefix + gap + suffix + number + unit
    if unit:
        p = f"{escaped_prefix}{gap}{escaped_suffix}[^\\d\\n]{{0,20}}{number}\\s*{re.escape(unit)}"
        m = re.search(p, text)
        if m:
            return m.group(1).replace(",", "")

    p = f"{escaped_prefix}{gap}{escaped_suffix}[^\\d\\n]{{0,20}}{number}"
    m = re.search(p, text)
    if m:
        return m.group(1).replace(",", "")

    return ""


def _extract_contextual_increase(
    text: str,
    field_name: str,
    para_text: str,
    pos: int,
) -> str:
    """从段落上下文中提取'增加N'的值。"""
    # 用整个段落的上下文关键词（去掉占位符）来定位
    clean_para = CN_PLACEHOLDER_RE.sub("", para_text)
    context_words = _extract_keywords(clean_para)

    unit = ""
    if "亿人次" in field_name:
        unit = "亿人次"
    elif "万人次" in field_name:
        unit = "万人次"
    elif "万人" in field_name:
        unit = "万人"

    scope = _find_scope(text, context_words, window=500)

    if unit:
        p = rf"增加\s*([\d,]+(?:\.\d+)?)\s*{re.escape(unit)}"
        m = re.search(p, scope)
        if m:
            return m.group(1).replace(",", "")

    p = r"增加\s*([\d,]+(?:\.\d+)?)"
    m = re.search(p, scope)
    if m:
        return m.group(1).replace(",", "")

    return ""


def _extract_contextual_growth(
    text: str,
    field_name: str,
    para_text: str,
    pos: int,
) -> str:
    """从段落上下文中提取'增长N%'的值。"""
    # 用整个段落的上下文关键词来定位
    clean_para = CN_PLACEHOLDER_RE.sub("", para_text)
    context_words = _extract_keywords(clean_para)

    scope = _find_scope(text, context_words, window=500)

    p = r"增长\s*([\d,]+(?:\.\d+)?)\s*[%％]"
    m = re.search(p, scope)
    if m:
        return m.group(1).replace(",", "")

    return ""


# ─── 字段名搜索提取 ────────────────────────────────────────────


def _search_value(text: str, field_name: str, unit: str = "") -> str:
    """从文本中搜索字段名对应的数值。"""
    if not field_name or not text:
        return ""

    variants = _field_variants(field_name)
    number = r"([\d,]+(?:\.\d+)?)"

    for variant in variants:
        escaped = re.escape(variant)
        gap = r"[^\d\n]{0,20}"

        if unit:
            unit_esc = re.escape(unit)
            p = f"{escaped}{gap}{number}\\s*{unit_esc}"
            m = re.search(p, text)
            if m:
                return m.group(1).replace(",", "")

        p = f"{escaped}{gap}{number}"
        m = re.search(p, text)
        if m:
            return m.group(1).replace(",", "")

    # 子短语灵活匹配：允许字段名中间插入额外文字
    if len(field_name) >= 4:
        value = _search_with_subphrases(text, field_name, unit)
        if value:
            return value

    return ""


def _field_variants(name: str) -> list[str]:
    """生成字段名的多种变体用于模糊匹配。"""
    variants = [name]

    for short, fulls in _field_aliases.items():
        if short in name:
            for full in fulls:
                variants.append(name.replace(short, full))

    # 对所有变体进行后缀移除
    for v in list(variants):
        base = re.sub(r"(总数|总量|数量|数)$", "", v)
        if base and base != v and base not in variants:
            variants.append(base)

    # 去掉空格
    for v in list(variants):
        no_space = v.replace(" ", "")
        if no_space != v and no_space not in variants:
            variants.append(no_space)

    return variants


def _search_with_subphrases(text: str, field_name: str, unit: str = "") -> str:
    """
    将字段名拆分为子短语，允许中间插入额外文字。
    例如: '每千人口床位数' → '每千人口' + '床位数'，允许中间有'医疗卫生机构'
    """
    name = field_name.replace(" ", "")
    number = r"([\d,]+(?:\.\d+)?)"

    for split_pos in range(2, len(name) - 1):
        prefix = name[:split_pos]
        suffix = name[split_pos:]
        if len(prefix) >= 2 and len(suffix) >= 2:
            escaped_prefix = re.escape(prefix)
            escaped_suffix = re.escape(suffix)
            gap = r"[\u4e00-\u9fff（）]{0,10}"

            if unit:
                p = f"{escaped_prefix}{gap}{escaped_suffix}[^\\d\\n]{{0,20}}{number}\\s*{re.escape(unit)}"
                m = re.search(p, text)
                if m:
                    return m.group(1).replace(",", "")

            p = f"{escaped_prefix}{gap}{escaped_suffix}[^\\d\\n]{{0,20}}{number}"
            m = re.search(p, text)
            if m:
                return m.group(1).replace(",", "")

    return ""


# ─── 表格单元格提取 ────────────────────────────────────────────


def _extract_cell_value(
    scope: str,
    full_text: str,
    row_label: str,
    col_header: str,
) -> str:
    """根据行标签和列头从文本中提取对应单元格的值。"""
    header = col_header.strip()

    unit_m = re.search(r"[（(]([^）)]+)[）)]", header)
    unit = unit_m.group(1) if unit_m else ""

    if "数量" in header or "金额" in header:
        value = _search_value(scope, row_label, unit)
        if not value:
            value = _search_value(full_text, row_label, unit)
        return value

    if "占比" in header:
        value = _extract_pct(scope, row_label)
        if not value:
            value = _extract_pct(full_text, row_label)
        return value

    if "增减" in header or "变化" in header:
        value = _extract_change(scope, row_label)
        if not value:
            value = _extract_change(full_text, row_label)
        return value

    value = _search_value(scope, row_label, unit)
    if not value:
        value = _search_value(full_text, row_label, unit)
    return value


def _extract_pct(text: str, label: str) -> str:
    """提取标签对应的百分比。"""
    escaped = re.escape(label)
    p = f"{escaped}[^\\n]{{0,80}}(?:占|占比)[^\\d]{{0,5}}([\\d,.]+)\\s*[%％]"
    m = re.search(p, text)
    if m:
        return m.group(1)
    return ""


def _extract_change(text: str, label: str) -> str:
    """提取标签对应的较上年变化。"""
    escaped = re.escape(label)
    p = f"{escaped}\\s*(?:增加|减少|增长|下降)\\s*([\\d,]+(?:\\.\\d+)?)"
    m = re.search(p, text)
    if m:
        full = m.group(0)
        sign = "-" if ("减少" in full or "下降" in full) else "+"
        return f"{sign}{m.group(1).replace(',', '')}"

    p = f"{escaped}[^\\n]{{0,30}}(增加|减少|增长|下降)\\s*([\\d,]+(?:\\.\\d+)?)"
    m = re.search(p, text)
    if m:
        sign = "-" if m.group(1) in ("减少", "下降") else "+"
        return f"{sign}{m.group(2).replace(',', '')}"

    return ""


# ─── 通用辅助 ──────────────────────────────────────────────────


def _find_scope(text: str, keywords: list[str], window: int = 800) -> str:
    """根据关键词在文本中找到包含最多关键词的区域。"""
    if not keywords:
        return text

    best_pos = -1
    best_score = 0

    for kw in keywords[:8]:
        for m in re.finditer(re.escape(kw), text):
            pos = m.start()
            s = max(0, pos - 200)
            e = min(len(text), pos + 600)
            scope = text[s:e]
            score = sum(1 for k in keywords if k in scope)
            if score > best_score:
                best_score = score
                best_pos = pos

    if best_pos < 0:
        return text

    s = max(0, best_pos - 300)
    e = min(len(text), best_pos + window)
    return text[s:e]


def _clean_label(label: str) -> str:
    """清理表格行标签。"""
    label = label.strip()
    label = re.sub(r"^\s*其中[：:]\s*", "", label)
    return label.strip()
