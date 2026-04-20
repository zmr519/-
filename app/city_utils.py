"""
city_utils.py — 城市名称识别与匹配工具

提供城市名的提取、标准化、匹配和约束过滤功能。
用于从模板上下文识别目标城市，并在抽取记录时按城市过滤。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping
import re

from app.schemas import normalize_field_name, normalize_text


# "市"后面跟这些字符时不是城市名：市场/市值/市价/市集/市面/市井/市侩/市斤/市里/市尺/市亩/市制/市售
_NOT_CITY_SUFFIX_CHARS = "场值价集面井侩斤里尺亩制售"
# 城市行政区划后缀正则片段（对"市"加负向前瞻，排除"市场"等复合词）
_CITY_SUFFIX_RE = rf"(?:市(?![{_NOT_CITY_SUFFIX_CHARS}])|地区|自治州|盟)"

# 城市实体的通用正则：匹配"XX市""XX地区""XX自治州""XX盟"
CITY_ENTITY_PATTERN = re.compile(rf"([\u4e00-\u9fff]{{2,}}{_CITY_SUFFIX_RE})")
# 显式城市意图模式：例如"目标城市：济南市""济南市各监测站点"
EXPLICIT_CITY_PATTERNS = [
    re.compile(rf"(?:目标城市|模板城市|所属城市|城市|地市)\s*[：:]\s*([\u4e00-\u9fff]{{2,}}{_CITY_SUFFIX_RE})"),
    re.compile(rf"([\u4e00-\u9fff]{{2,}}{_CITY_SUFFIX_RE})各(?:监测站点|站点|区县|区域|行政区)"),
    re.compile(rf"(?:记录|反映|掌握|用于|针对)\s*([\u4e00-\u9fff]{{2,}}{_CITY_SUFFIX_RE})"),
]
CITY_FIELD_KEYS = {
    normalize_field_name("城市"),
    normalize_field_name("地市"),
    normalize_field_name("地区"),
    normalize_field_name("所属城市"),
    normalize_field_name("国家/地区"),
    normalize_field_name("国家地区"),
}


def normalize_city_name(value: Any) -> str:
    """城市名标准化：去空格、去前缀废词、去后缀废词。"""
    text = re.sub(r"\s+", "", normalize_text(value))
    text = re.sub(r"^(?:本表记录|本表|记录|反映|掌握|用于|针对|时刻|关于|位于|模板目标城市|目标城市|城市|地市)+", "", text)
    text = re.sub(r"(?:各监测站点|监测站点|各站点|站点).*?$", "", text)
    return text


def _city_aliases(value: Any) -> set[str]:
    text = normalize_city_name(value)
    if not text:
        return set()

    aliases = {text}
    suffixes = ("自治州", "地区", "市", "盟")
    for suffix in suffixes:
        if text.endswith(suffix) and len(text) > len(suffix):
            aliases.add(text[: -len(suffix)])
    return {alias for alias in aliases if alias}


def city_matches(actual_city: Any, target_city: Any) -> bool:
    """判断两个城市名是否指向同一城市（支持“济南”≈“济南市”）。"""
    actual_aliases = _city_aliases(actual_city)
    target_aliases = _city_aliases(target_city)
    if not actual_aliases or not target_aliases:
        return False
    return bool(actual_aliases & target_aliases)


def text_mentions_city(text: Any, target_city: Any) -> bool:
    value = normalize_text(text)
    target = normalize_city_name(target_city)
    if not value or not target:
        return False
    if target in value:
        return True
    return any(alias and alias in value for alias in _city_aliases(target))


def extract_city_mentions(text: Any) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []

    mentions: list[str] = []
    for pattern in EXPLICIT_CITY_PATTERNS:
        for match in pattern.finditer(normalized):
            value = normalize_city_name(match.group(1))
            if value and value not in mentions:
                mentions.append(value)

    for match in CITY_ENTITY_PATTERN.finditer(normalized):
        value = normalize_city_name(match.group(1))
        if value and value not in mentions:
            mentions.append(value)

    return mentions


def detect_target_city(texts: Iterable[Any]) -> str:
    """
    从多段文本中检测“目标城市”。
    使用加权计数策略：显式模式得分更高，靠前的文本得分更高。
    用于从模板标题、前言、表头中推断模板针对哪个城市。
    """
    weighted = Counter()

    for index, text in enumerate(texts):
        normalized = normalize_text(text)
        if not normalized:
            continue

        explicit = False
        for pattern in EXPLICIT_CITY_PATTERNS:
            match = pattern.search(normalized)
            if not match:
                continue
            value = normalize_city_name(match.group(1))
            if value:
                weighted[value] += max(4, 10 - index)
                explicit = True
                break

        for mention in extract_city_mentions(normalized):
            weighted[mention] += max(1, 6 - index)

        if explicit:
            continue

    if not weighted:
        return ""
    return weighted.most_common(1)[0][0]


def extract_record_city(record: Mapping[str, Any] | None) -> str:
    """从一条记录中提取城市名（优先检查“城市”“地市”等字段）。"""
    if not isinstance(record, Mapping):
        return ""

    normalized_map = {
        normalize_field_name(key): normalize_text(value)
        for key, value in record.items()
    }

    for key in CITY_FIELD_KEYS:
        value = normalized_map.get(key, "")
        if not value:
            continue
        mentions = extract_city_mentions(value)
        if mentions:
            return mentions[0]
        normalized = normalize_city_name(value)
        if normalized:
            return normalized

    merged = " ".join(value for value in normalized_map.values() if value)
    mentions = extract_city_mentions(merged)
    if mentions:
        return mentions[0]
    return ""


def record_matches_city(record: Mapping[str, Any] | None, target_city: Any) -> bool:
    """判断一条记录是否属于目标城市（综合字段匹配 + 全文提及）。"""
    if not target_city:
        return True
    record_city = extract_record_city(record)
    if record_city:
        return city_matches(record_city, target_city)
    merged = " ".join(normalize_text(value) for value in (record or {}).values())
    return text_mentions_city(merged, target_city)
