"""
simple_extractor.py — 规则/NER 抽取器

不依赖 LLM 的轻量抽取手段：
  - 从结构化表格行直接映射字段（最高置信度）
  - 从“字段名: 值”键值对模式提取
  - 使用自定义正则匹配
  - 针对城市经济、COVID-19 等场景的专用抽取逻辑
  - 用户查询约束解析（地点、日期范围、最大记录数）

主要入口：
  - extract_query_constraints()  : 解析用户查询中的约束条件
  - extract_field_candidates()   : 为单个字段抽取候选值
  - extract_record_candidates()  : 为多字段抽取完整记录
"""

from __future__ import annotations

from datetime import datetime
import re

from app.city_utils import city_matches, extract_record_city, text_mentions_city
from app.schemas import (
    ExtractionCandidate,
    FieldSpec,
    QueryConstraints,
    SearchHit,
    normalize_field_name,
    normalize_text,
    stable_id,
)


# 来源权重：xlsx 最高（0.98），因为结构化表格数据最可靠
SOURCE_WEIGHTS = {
    "rule_paragraph": 0.93,
    "rule_regex": 0.88,
    "ner": 0.74,
    "llm": 0.70,
}

# 提取器权重
EXTRACTOR_WEIGHTS = {
    "rule_paragraph": 0.93,
    "rule_regex": 0.88,
    "ner": 0.74,
    "llm": 0.70,
}

GENERIC_ENTITY_PATTERNS = {
    "date": [
        r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?",
    ],
    "datetime": [
        r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?:\s+\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)?",
    ],
    "number": [
        r"-?\d[\d,]*(?:\.\d+)?",
    ],
    "currency": [
        r"(?:人民币|¥|￥)?\s*-?\d[\d,]*(?:\.\d+)?\s*(?:元|万元|亿元)?",
    ],
    "location": [
        r"[\u4e00-\u9fff]{2,}(?:省|市|区|县|州|旗|自治区|特别行政区)",
        r"[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*",
    ],
    "person": [
        r"[\u4e00-\u9fff]{2,4}",
    ],
    "string": [
        r"[^\n|；;，,。]{1,60}",
    ],
}


def simple_extract(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    city = re.search(r"([\u4e00-\u9fff]{2,}(?:省|市|区|县|州|自治区))", text)
    if city:
        data["城市"] = city.group(1)
    number = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if number:
        data["数值"] = number.group(0)
    return data


def _extract_location_mentions(text: str) -> list[str]:
    patterns = [
        r"([\u4e00-\u9fff]{2,}(?:省|市|自治区|特别行政区))(?:各(?:监测站点)?|的|空气|环境|数据|记录)",
        r"(?:时刻|时间|关于|针对|对于|记录|位于|来自)([\u4e00-\u9fff]{2,}(?:省|市|自治区|特别行政区))",
        r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,}(?:省|市|自治区|特别行政区))(?![\u4e00-\u9fff])",
    ]
    matches: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = _clean_location_candidate(match.group(1))
            if value and value not in matches:
                matches.append(value)
    return matches


def _clean_location_candidate(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"^(?:本表|时刻|时间|关于|针对|对于|记录|位于|来自)+", "", text)
    return text


def _normalize_datetime_token(match: tuple[str, str, str, str]) -> str:
    year, month, day, time_part = match
    date_part = f"{year}-{int(month):02d}-{int(day):02d}"
    if not time_part:
        return date_part
    return f"{date_part} {time_part}"


def _extract_datetime_mentions(text: str) -> list[str]:
    pattern = re.compile(
        r"(\d{4})\s*(?:[-/年])\s*(\d{1,2})\s*(?:[-/月])\s*(\d{1,2})(?:\s*日)?(?:\s*(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?))?"
    )
    values: list[str] = []
    for match in pattern.findall(text):
        value = _normalize_datetime_token(match)
        if value not in values:
            values.append(value)
    return values


def extract_query_constraints(query_text: str) -> QueryConstraints:
    """
    解析用户查询文本中的约束条件：
    - 地点（城市/省份）
    - 日期范围
    - 最大记录数（如“前10条”）
    """
    text = normalize_text(query_text)
    locations = _extract_location_mentions(text)
    date_matches = _extract_datetime_mentions(text)
    max_records_match = re.search(r"(?:前|最多|top)\s*(\d+)\s*(?:条|行|个)?", text, re.I)
    max_records = int(max_records_match.group(1)) if max_records_match else None
    if len(date_matches) >= 2:
        date_range = (date_matches[0], date_matches[1])
    elif len(date_matches) == 1:
        date_range = (date_matches[0], date_matches[0])
    else:
        date_range = (None, None)

    return QueryConstraints(
        locations=locations,
        exact_terms=[],
        date_range=date_range,
        max_records=max_records,
    )


def extract_field_candidates(
    field_spec: FieldSpec,
    hits: list[SearchHit],
) -> list[ExtractionCandidate]:
    """
    为单个字段从检索结果中抽取候选值。
    依次尝试：结构化行映射 → 文本模式/正则匹配。
    """
    candidates: list[ExtractionCandidate] = []
    seen: set[tuple[str, str]] = set()

    for hit in hits:
        structured_row = hit.chunk.metadata.get("structured_row", {})
        row_candidate = _extract_from_structured_row(field_spec, hit, structured_row)
        if row_candidate:
            key = (field_spec.name, normalize_text(row_candidate.values.get(field_spec.name)))
            if key not in seen:
                seen.add(key)
                candidates.append(row_candidate)

        for candidate in _extract_from_text(field_spec, hit):
            key = (field_spec.name, normalize_text(candidate.values.get(field_spec.name)))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

    return candidates


def extract_record_candidates(
    field_specs: list[FieldSpec],
    hits: list[SearchHit],
    query_text: str,
) -> list[ExtractionCandidate]:
    """
    为多字段抽取完整记录。
    从结构化表格行 + 正文段落中抽取，并按约束条件过滤。
    """
    constraints = extract_query_constraints(query_text)
    candidates: list[ExtractionCandidate] = []
    seen: set[tuple[str, ...]] = set()

    for hit in hits:
        structured_row = hit.chunk.metadata.get("structured_row")
        if isinstance(structured_row, dict):
            values = {}
            matched_fields = 0
            for field_spec in field_specs:
                row_value = _lookup_structured_value(field_spec, structured_row)
                if row_value:
                    values[field_spec.name] = row_value
                    matched_fields += 1

            if values and matched_fields >= max(1, len(field_specs) // 3):
                if not constraints.active() or _row_matches_constraints(structured_row, constraints):
                    dedupe_key = tuple(
                        normalize_text(values.get(spec.name, ""))
                        for spec in field_specs
                    )
                    if dedupe_key not in seen:
                        seen.add(dedupe_key)
                        completeness = matched_fields / max(len(field_specs), 1)
                        candidates.append(
                            _build_candidate(
                                values=values,
                                extractor="rule_row",
                                confidence=round(min(0.99, 0.65 + completeness * 0.3), 4),
                                hit=hit,
                                evidence_quote=hit.chunk.text,
                                explanation="直接从结构化表格行映射到模板字段，字段覆盖度较高。",
                                evidence_quality=round(min(1.0, 0.7 + completeness * 0.3), 4),
                            )
                        )

        narrative_candidate = _extract_from_narrative_record(field_specs, hit, constraints)
        if narrative_candidate:
            dedupe_key = tuple(
                normalize_text(narrative_candidate.values.get(spec.name, ""))
                for spec in field_specs
            )
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                candidates.append(narrative_candidate)

    return candidates


def _extract_from_narrative_record(
    field_specs: list[FieldSpec],
    hit: SearchHit,
    constraints: QueryConstraints,
) -> ExtractionCandidate | None:
    text = normalize_text(hit.chunk.text)
    if not text:
        return None

    values = {}
    matched_fields = 0
    for field_spec in field_specs:
        value = _extract_narrative_field_value(field_spec, text, hit.chunk.metadata)
        if value:
            values[field_spec.name] = value
            matched_fields += 1

    minimum_fields = _minimum_narrative_fields(field_specs, text, hit.chunk.metadata)
    if matched_fields < minimum_fields:
        return None
    if constraints.active() and not _narrative_matches_constraints(values, constraints, hit.chunk.metadata, text):
        return None

    completeness = matched_fields / max(len(field_specs), 1)
    return _build_candidate(
        values=values,
        extractor="rule_paragraph",
        confidence=round(min(0.97, 0.68 + completeness * 0.25), 4),
        hit=hit,
        evidence_quote=text,
        explanation="从正文段落中的字段标签和数值模式批量抽取出一条记录。",
        evidence_quality=round(min(1.0, 0.72 + completeness * 0.2), 4),
    )


def _extract_narrative_field_value(
    field_spec: FieldSpec,
    text: str,
    metadata: dict[str, str] | None = None,
) -> str:
    normalized_name = normalize_field_name(field_spec.name)
    compact_text = normalize_text(text)
    metadata = metadata or {}
    entity_context = normalize_text(metadata.get("entity_context"))
    source_path = normalize_text(metadata.get("file_path"))
    if not compact_text:
        return ""

    if _looks_like_china_covid_context(source_path=source_path, entity_context=entity_context, text=compact_text):
        region_name = entity_context or _extract_china_region_from_text(text)
        if _is_region_field(normalized_name):
            return region_name or "中国"
        if _is_continent_field(normalized_name):
            continent = _extract_continent(text)
            return continent or "Asia"
        if _is_population_field(normalized_name):
            return _extract_population_value(text)
        if _is_per_capita_gdp_field(normalized_name):
            return _extract_per_capita_gdp_value(text)
        if _is_daily_tests_field(normalized_name):
            return _extract_daily_tests_value(text)
        if _is_case_count_field(normalized_name):
            return _extract_case_count_value(text)

    if "城市" in normalized_name:
        # 优先：城市名紧接着 GDP/人口/收入 关键词
        city_match = re.search(
            r"^\s*([\u4e00-\u9fffA-Za-z·]+?)(?=\s*(?:G\s*D\s*P|人均\s*G\s*D\s*P|常住人口|一般公共预算收入))",
            compact_text,
            flags=re.I,
        )
        if city_match:
            return city_match.group(1).strip()
        # 降级：段落开头 2-4 个汉字（城市名），匹配最少字符（非贪婪），
        # 要求段落含 GDP 关键词且包含至少 3 个数值（确保是城市经济简介段落）
        if re.search(r"G\s*D\s*P|一般公共预算收入", compact_text, flags=re.I):
            if len(re.findall(r"\d[\d,]*\.?\d*", compact_text)) >= 3:
                city_start = re.match(r"\s*([\u4e00-\u9fff]{2,4}?)", compact_text)
                if city_start:
                    return city_start.group(1).strip()

    if "人均gdp" in normalized_name:
        result = _match_number_after_labels(text, ["人均GDP"])
        if result:
            return result
        # 连接词容忍：人均GDP高达/为 X
        m = re.search(r"人\s*均\s*G\s*D\s*P[^0-9]{0,8}([0-9][0-9,]*(?:\.\d+)?)", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        # 数字在标签前：X 元的人均GDP
        m = re.search(r"([0-9][0-9,]*(?:\.\d+)?)\s*元[^0-9]{0,12}人\s*均\s*G\s*D\s*P", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        return ""

    if "gdp总量" in normalized_name:
        result = _match_number_after_labels(text, ["GDP总量"])
        if result:
            return result
        # 连接词容忍：GDP总量达/为 X（限制≤4个非数字字符，避免误跨字段）
        m = re.search(r"G\s*D\s*P\s*总\s*量[^0-9]{0,4}([0-9][0-9,]*(?:\.\d+)?)", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        # 数字在标签前：X 亿元的 GDP总量（如"以38,731.80亿元的GDP总量"）
        m = re.search(r"([0-9][0-9,]*(?:\.\d+)?)\s*亿\s*元[^0-9]{0,6}G\s*D\s*P\s*总\s*量", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        return ""

    if "常住人口" in normalized_name:
        result = _match_number_after_labels(text, ["常住人口"])
        if result:
            return result
        # 连接词容忍：常住人口达/约 X（严格限制≤1字符，防止跨字段误匹配）
        m = re.search(r"常\s*住\s*人\s*口[^0-9]{0,1}([0-9][0-9,]*(?:\.\d+)?)", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        # 数字在标签前：X 万常住人口
        m = re.search(r"([0-9][0-9,]*(?:\.\d+)?)\s*万[^0-9]{0,8}常\s*住\s*人\s*口", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        # 降级：X 万人口（无"常住"限定词，如"3,191.43万的庞大人口基数"）
        m = re.search(r"([0-9][0-9,]*(?:\.\d+)?)\s*万[^0-9]{0,10}人\s*口", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        # 极端降级：人口...X万（如"人口严控至2,185.3万"）
        m = re.search(r"人\s*口[^0-9]{0,8}([0-9][0-9,]*(?:\.\d+)?)\s*万", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        return ""

    if "一般公共预算收入" in normalized_name:
        result = _match_number_after_labels(text, ["一般公共预算收入"])
        if result:
            return result
        # 连接词容忍：收入突破/跃升至/增长至 X
        m = re.search(r"一\s*般\s*公\s*共\s*预\s*算\s*收\s*入[^0-9]{0,12}([0-9][0-9,]*(?:\.\d+)?)", compact_text, re.I)
        if m:
            return _normalize_number_text(m.group(1))
        return ""

    labels = [field_spec.name, *field_spec.aliases]

    # 解析 "字段名，单位：xxx" 格式，提取纯字段名和单位
    clean_name = re.sub(r"[，,]\s*单位[：:].+$", "", field_spec.name).strip()
    unit_match = re.search(r"单位[：:]\s*(.+)$", field_spec.name)
    unit_hint = unit_match.group(1).strip() if unit_match else ""

    # 构建搜索标签：把清理后的字段名加入候选（避免重复）
    extended_labels = [clean_name] + [l for l in labels if l != clean_name]
    label_patterns = [_strip_unit_suffix(label) for label in extended_labels]

    if field_spec.value_type in {"number", "integer", "float"}:
        # 优先：带单位的精确搜索
        if unit_hint:
            unit_esc = re.escape(unit_hint)
            for lbl in label_patterns:
                if not lbl:
                    continue
                p = rf"{_spaced_label_regex(lbl)}\s*{_CN_CONNECTOR}([0-9][0-9,]*(?:\.\d+)?)\s*{unit_esc}"
                m = re.search(p, text, flags=re.I)
                if m:
                    return _normalize_number_text(m.group(1))
        return _match_number_after_labels(text, label_patterns)

    for label in label_patterns:
        if not label:
            continue
        pattern = rf"{_spaced_label_regex(label)}\s*[：:]\s*([^\n|；;，,。]+)"
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1).strip()
    return ""


def _minimum_narrative_fields(
    field_specs: list[FieldSpec],
    text: str,
    metadata: dict[str, str] | None = None,
) -> int:
    metadata = metadata or {}
    entity_context = normalize_text(metadata.get("entity_context"))
    source_path = normalize_text(metadata.get("file_path"))
    default_minimum = max(2, int(len(field_specs) * 0.6))
    if _looks_like_china_covid_context(source_path=source_path, entity_context=entity_context, text=text):
        # 中国疫情日报中省级段落通常带有 entity_context，可放宽到 2；
        # 但标题/总述段落只有“中国/Asia”等弱信号时，不应被当作一条地区记录写回模板。
        if not entity_context:
            return max(default_minimum, 4)
        return min(default_minimum, 2)
    return default_minimum


def _looks_like_china_covid_context(source_path: str, entity_context: str, text: str) -> bool:
    normalized_path = normalize_text(source_path).lower()
    if "\u4e2d\u56fd" not in normalized_path or "covid" not in normalized_path:
        return False
    # 文件路径明确包含"中国"和"covid"即可判定为中国疫情语境
    # 即使没有找到具体省市名称（如全国概述段落），也应进入专用提取分支
    return True


def _is_region_field(normalized_name: str) -> bool:
    return normalized_name in {
        "\u56fd\u5bb6\u5730\u533a",
        "\u5730\u533a",
    } or ("\u56fd\u5bb6" in normalized_name and "\u5730\u533a" in normalized_name)


def _is_continent_field(normalized_name: str) -> bool:
    return normalized_name == "\u5927\u6d32"


def _is_population_field(normalized_name: str) -> bool:
    return normalized_name == "\u4eba\u53e3" or normalized_name.endswith("\u4eba\u53e3")


def _is_per_capita_gdp_field(normalized_name: str) -> bool:
    return "gdp" in normalized_name and "\u4eba\u5747" in normalized_name


def _is_daily_tests_field(normalized_name: str) -> bool:
    return normalized_name in {
        "\u6bcf\u65e5\u68c0\u6d4b\u6570",
        "\u65e5\u68c0\u6d4b\u6570",
        "\u68c0\u6d4b\u6570",
    } or ("\u68c0\u6d4b" in normalized_name and "\u6570" in normalized_name)


def _is_case_count_field(normalized_name: str) -> bool:
    return normalized_name in {
        "\u75c5\u4f8b\u6570",
        "\u786e\u8bca\u75c5\u4f8b\u6570",
        "\u65b0\u589e\u75c5\u4f8b\u6570",
    } or "\u75c5\u4f8b" in normalized_name


def _extract_continent(text: str) -> str:
    match = re.search(
        r"(Asia|Europe|Africa|North America|South America|Oceania|Antarctica)",
        text,
        flags=re.I,
    )
    if match:
        return match.group(1)
    chinese_match = re.search(r"(\u4e9a\u6d32|\u6b27\u6d32|\u975e\u6d32|\u5317\u7f8e\u6d32|\u5357\u7f8e\u6d32|\u5927\u6d0b\u6d32)", text)
    if not chinese_match:
        return ""
    mapping = {
        "\u4e9a\u6d32": "Asia",
        "\u6b27\u6d32": "Europe",
        "\u975e\u6d32": "Africa",
        "\u5317\u7f8e\u6d32": "North America",
        "\u5357\u7f8e\u6d32": "South America",
        "\u5927\u6d0b\u6d32": "Oceania",
    }
    return mapping.get(chinese_match.group(1), "")


def _extract_china_region_from_text(text: str) -> str:
    # 去掉 pipeline 注入的 [来源:...] 前缀，使省市名能从开头匹配
    cleaned = re.sub(r"^\[来源:[^\]]*\]\s*", "", normalize_text(text))
    match = re.match(
        r"^\s*([\u4e00-\u9fff]{2,}(?:省|市|自治区|回族自治区|维吾尔自治区|壮族自治区|特别行政区))",
        cleaned,
    )
    if match:
        return match.group(1)
    return ""


def _extract_population_value(text: str) -> str:
    return _extract_chinese_unit_number(
        text,
        [
            r"\u5e38\u4f4f\u4eba\u53e3",
            r"\u4eba\u53e3",
        ],
    )


def _extract_per_capita_gdp_value(text: str) -> str:
    return _extract_chinese_unit_number(
        text,
        [
            r"\u4eba\u5747\s*G\s*D\s*P",
            r"\u4eba\u5747GDP",
        ],
        suffix_pattern=r"\s*\u5143?",
    )


def _extract_daily_tests_value(text: str) -> str:
    return _extract_chinese_unit_number(
        text,
        [
            r"\u5f53\u65e5(?:\u6838\u9178)?\u68c0\u6d4b(?:\u91cf|\u6570)?",
            r"\u6bcf\u65e5(?:\u6838\u9178)?\u68c0\u6d4b(?:\u91cf|\u6570)?",
            r"(?:\u6838\u9178)?\u68c0\u6d4b(?:\u91cf|\u6570)",
        ],
        suffix_pattern=r"\s*(?:\u4efd|\u4eba\u6b21)?",
    )


def _extract_case_count_value(text: str) -> str:
    zero_patterns = [
        r"\u65e0\u65b0\u589e(?:\u786e\u8bca)?\u75c5\u4f8b",
        r"\u65e0\u65b0\u589e\u786e\u8bca\u4e0e\u7591\u4f3c\u75c5\u4f8b",  # 无新增确诊与疑似病例
        r"\u96f6\u65b0\u589e(?:\u786e\u8bca)?\u75c5\u4f8b?",
        r"\u96f6\u65b0\u589e\u786e\u8bca",  # 零新增确诊（不带"病例"后缀）
        r"\u5168\u96f6\u62a5\u544a",
        r"\u65e0\u75ab\u60c5\u65b0\u589e",  # 无疫情新增
        r"\u65e0\u65b0\u589e\u75c5\u4f8b",  # 无新增病例
    ]
    for pattern in zero_patterns:
        if re.search(pattern, text):
            return "0"

    patterns = [
        r"\u65b0\u589e\s*(\d+)\s*\u4f8b(?:\u672c\u571f|\u5883\u5916\u8f93\u5165)?\u786e\u8bca\u75c5\u4f8b",
        r"\u65b0\u589e\s*(\d+)\s*\u4f8b(?:\u672c\u571f|\u5883\u5916\u8f93\u5165)?\u65e0\u75c7\u72b6\u611f\u67d3\u8005",
        r"(?:\u65b0\u589e|\u62a5\u544a\u65b0\u589e)\s*(\d+)\s*\u4f8b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _normalize_number_text(match.group(1))
    return ""


def _extract_chinese_unit_number(
    text: str,
    label_patterns: list[str],
    suffix_pattern: str = "",
) -> str:
    for label_pattern in label_patterns:
        pattern = (
            rf"{label_pattern}[^\d]{{0,12}}"
            rf"(?P<number>\d+(?:\.\d+)?)\s*"
            rf"(?P<unit>\u4ebf|\u4e07)?"
            rf"{suffix_pattern}"
        )
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        return _convert_chinese_number(match.group("number"), match.group("unit"))
    return ""


def _convert_chinese_number(number_text: str, unit: str | None = None) -> str:
    try:
        value = float(_normalize_number_text(number_text))
    except ValueError:
        return ""

    multiplier = 1
    if unit == "\u4e07":
        multiplier = 10000
    elif unit == "\u4ebf":
        multiplier = 100000000

    converted = value * multiplier
    if converted.is_integer():
        return str(int(converted))
    return f"{converted:.4f}".rstrip("0").rstrip(".")


def _narrative_matches_constraints(
    values: dict[str, str],
    constraints: QueryConstraints,
    metadata: dict[str, str] | None = None,
    text: str = "",
) -> bool:
    metadata = metadata or {}
    values_text = " ".join(normalize_text(value) for value in values.values())
    if constraints.locations:
        record_city = extract_record_city(values)
        if record_city:
            if not any(city_matches(record_city, location) for location in constraints.locations):
                return False
        elif not any(text_mentions_city(values_text or text, location) for location in constraints.locations):
            return False
    if not any(constraints.date_range):
        return True

    date_candidates = [
        normalize_text(metadata.get("date_context")),
        *re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", normalize_text(text)),
        *[
            f"{year}-{int(month):02d}-{int(day):02d}"
            for year, month, day in re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", normalize_text(text))
        ],
    ]
    start_text, end_text = constraints.date_range
    start_dt = _parse_datetime(start_text) if start_text else None
    end_dt = _parse_datetime(end_text) if end_text else None
    for candidate in date_candidates:
        parsed = _parse_datetime(candidate)
        if parsed and _date_in_range(parsed, start_dt, end_dt):
            return True
    return False


# 标签与数字之间允许出现的中文连接词（达到/为/是/超过/降至 等）
_CN_CONNECTOR = r"(?:[达到达为是约增至增加到增长到增长至增加至超过突破降至降到保持约为][^\d]{0,3})?"


def _match_number_after_labels(text: str, labels: list[str]) -> str:
    for label in labels:
        clean_label = _strip_unit_suffix(label)
        if not clean_label:
            continue
        # 先尝试精确紧贴（无连接词），再尝试带连接词的宽松匹配
        for gap in (r"\s*", rf"\s*{_CN_CONNECTOR}"):
            pattern = rf"{_spaced_label_regex(clean_label)}{gap}([0-9][0-9,]*(?:\.\d+)?)"
            match = re.search(pattern, text, flags=re.I)
            if match:
                return _normalize_number_text(match.group(1))
    return ""


def _strip_unit_suffix(label: str) -> str:
    text = normalize_text(label)
    text = re.sub(r"[（(].*?[)）]", "", text)
    return text.strip()


def _spaced_label_regex(label: str) -> str:
    return "".join(f"{re.escape(char)}\\s*" for char in label).rstrip("\\s*")


def _normalize_number_text(value: str) -> str:
    return normalize_text(value).replace(",", "")


def _extract_from_structured_row(
    field_spec: FieldSpec,
    hit: SearchHit,
    structured_row: dict[str, str],
) -> ExtractionCandidate | None:
    if not structured_row:
        return None
    value = _lookup_structured_value(field_spec, structured_row)
    if not value:
        return None
    return _build_candidate(
        values={field_spec.name: value},
        extractor="rule_row",
        confidence=0.95,
        hit=hit,
        evidence_quote=hit.chunk.text,
        explanation="字段名与表格列名或别名匹配，直接取结构化行值。",
        evidence_quality=0.95,
    )


def _lookup_structured_value(field_spec: FieldSpec, structured_row: dict[str, str]) -> str:
    normalized_map = {
        normalize_field_name(key): normalize_text(value)
        for key, value in structured_row.items()
    }
    for key in field_spec.all_names():
        normalized = normalize_field_name(key)
        if normalized in normalized_map and normalized_map[normalized]:
            return normalized_map[normalized]

    try:
        from rapidfuzz import fuzz
    except Exception:
        fuzz = None

    if fuzz:
        best_key = ""
        best_score = 0
        for row_key in structured_row:
            for alias in field_spec.all_names():
                score = fuzz.ratio(normalize_field_name(row_key), normalize_field_name(alias))
                if score > best_score:
                    best_key = row_key
                    best_score = score
        if best_key and best_score >= 85:
            return normalize_text(structured_row.get(best_key, ""))

    return ""


def _extract_from_text(field_spec: FieldSpec, hit: SearchHit) -> list[ExtractionCandidate]:
    text = hit.chunk.text
    candidates: list[ExtractionCandidate] = []

    for pattern in field_spec.regex_patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = match.groupdict().get("value") or match.group(0)
            candidates.append(
                _build_candidate(
                    values={field_spec.name: value},
                    extractor="rule_regex",
                    confidence=0.87,
                    hit=hit,
                    evidence_quote=match.group(0),
                    explanation=f"命中字段自定义正则: {pattern}",
                    evidence_quality=0.88,
                )
            )

    alias_candidates = []
    for alias in field_spec.all_names():
        pattern = rf"{re.escape(alias)}\s*[：:]\s*(?P<value>[^\n|；;，,。]+)"
        alias_candidates.extend(re.finditer(pattern, text, flags=re.I))
    for match in alias_candidates:
        value = match.group("value").strip()
        candidates.append(
            _build_candidate(
                values={field_spec.name: value},
                extractor="rule_regex",
                confidence=0.90,
                hit=hit,
                evidence_quote=match.group(0),
                explanation="命中字段名/别名后的键值对模式。",
                evidence_quality=0.90,
            )
        )

    entity_patterns = GENERIC_ENTITY_PATTERNS.get(field_spec.value_type) or GENERIC_ENTITY_PATTERNS["string"]
    if field_spec.value_type == "currency":
        entity_patterns = GENERIC_ENTITY_PATTERNS["currency"]
    elif field_spec.value_type == "number":
        entity_patterns = GENERIC_ENTITY_PATTERNS["number"]

    sentences = re.split(r"[。！？\n]", text)
    for sentence in sentences:
        if not sentence.strip():
            continue
        sentence_has_alias = any(alias in sentence for alias in field_spec.all_names())
        if not sentence_has_alias:
            continue
        for pattern in entity_patterns:
            match = re.search(pattern, sentence, flags=re.I)
            if match:
                candidates.append(
                    _build_candidate(
                        values={field_spec.name: match.group(0).strip()},
                        extractor="ner",
                        confidence=0.70,
                        hit=hit,
                        evidence_quote=sentence.strip(),
                        explanation="在包含字段别名的句子中命中了轻量 NER/实体模式。",
                        evidence_quality=0.68,
                    )
                )
                break

    return candidates


def _row_matches_constraints(row: dict[str, str], constraints: QueryConstraints) -> bool:
    values_text = " ".join(normalize_text(value) for value in row.values())
    if constraints.locations:
        row_city = extract_record_city(row)
        if row_city:
            if not any(city_matches(row_city, location) for location in constraints.locations):
                return False
        elif not any(text_mentions_city(values_text, location) for location in constraints.locations):
            return False

    start_text, end_text = constraints.date_range
    if start_text or end_text:
        row_dates = [
            _parse_datetime(normalize_text(value))
            for value in row.values()
            if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", normalize_text(value))
        ]
        row_dates = [value for value in row_dates if value]
        start_dt = _parse_datetime(start_text) if start_text else None
        end_dt = _parse_datetime(end_text) if end_text else None
        if row_dates and (start_dt or end_dt):
            if not any(_date_in_range(value, start_dt, end_dt) for value in row_dates):
                return False

    return True


def _date_in_range(value: datetime, start_dt: datetime | None, end_dt: datetime | None) -> bool:
    if start_dt and value < start_dt:
        return False
    if end_dt and value > end_dt:
        return False
    return True


def _parse_datetime(value: str | None) -> datetime | None:
    text = normalize_text(value)
    if not text:
        return None
    chinese = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s*(\d{1,2}:\d{2}(?::\d{2})?))?", text)
    if chinese:
        year, month, day, time_part = chinese.groups()
        text = f"{year}-{int(month):02d}-{int(day):02d}"
        if time_part:
            text = f"{text} {time_part}"
    candidates = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _build_candidate(
    values: dict[str, str],
    extractor: str,
    confidence: float,
    hit: SearchHit,
    evidence_quote: str,
    explanation: str,
    evidence_quality: float,
) -> ExtractionCandidate:
    file_type = hit.chunk.metadata.get("file_type", "")
    source_weight = SOURCE_WEIGHTS.get(file_type, 0.6)
    extractor_weight = EXTRACTOR_WEIGHTS.get(extractor, 0.65)
    candidate_id = stable_id(
        extractor,
        hit.chunk.chunk_id,
        tuple(sorted(values.items())),
    )
    return ExtractionCandidate(
        candidate_id=candidate_id,
        values=values,
        extractor=extractor,
        confidence=confidence,
        evidence_quote=evidence_quote,
        source_chunk_ids=[hit.chunk.chunk_id],
        source_path=str(hit.chunk.metadata.get("file_path", "")),
        retrieval_score=max(0.0, min(1.0, float(hit.score))),
        keyword_score=max(0.0, min(1.0, float(hit.keyword_score))),
        evidence_quality=evidence_quality,
        source_weight=source_weight,
        extractor_weight=extractor_weight,
        recency_score=0.5,
        explanation=explanation,
        metadata={
            "file_type": file_type,
            "block_type": hit.chunk.metadata.get("block_type", ""),
        },
    )
