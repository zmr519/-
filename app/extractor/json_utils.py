"""
json_utils.py — JSON 解析工具

从 LLM 的原始输出文本中提取 JSON 对象。
支持去除 ```json ``` 包裹、从混合文本中查找 JSON 对象/数组。
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> dict[str, Any]:
    """
    从 LLM 输出文本中提取 JSON 对象。
    依次尝试：直接解析 → 去除 markdown 包裹 → 正则提取 {} 或 []。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return {"error": "empty_output", "raw": text}

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.I).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    for candidate in [cleaned, *_extract_json_candidates(cleaned)]:
        try:
            return json.loads(candidate)
        except Exception:
            continue

    return {"error": "json_parse_failed", "raw": text}


def _extract_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    object_match = re.search(r"\{.*\}", text, flags=re.S)
    if object_match:
        candidates.append(object_match.group())
    array_match = re.search(r"\[.*\]", text, flags=re.S)
    if array_match:
        candidates.append(array_match.group())
    return candidates
