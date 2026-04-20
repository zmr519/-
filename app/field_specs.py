"""
field_specs.py — 字段规格加载与推断

提供两种方式获取 FieldSpec 列表：
  1. load_field_specs(path) — 从 YAML/JSON 配置文件加载详细的字段定义
  2. infer_field_specs(names) — 仅从字段名列表自动推断（用于从模板表头推断）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json

import yaml

from app.schemas import FieldSpec, build_field_spec, infer_computation_type, infer_value_type


def _load_raw(path: str | Path) -> dict[str, Any] | list[dict[str, Any]]:
    """读取 YAML/JSON 文件并返回原始数据结构。"""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"字段配置文件不存在: {target}")

    if target.suffix.lower() == ".json":
        return json.loads(target.read_text(encoding="utf-8"))
    return yaml.safe_load(target.read_text(encoding="utf-8"))


def load_field_specs(path: str | Path) -> list[FieldSpec]:
    """
    从 YAML/JSON 配置文件加载字段规格列表。
    支持 {fields: [...]} 或纯列表格式。
    """
    raw = _load_raw(path)
    items: list[dict[str, Any]]

    if isinstance(raw, dict):
        items = raw.get("fields", [])
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("字段配置文件格式错误，必须是 list 或 {fields: [...]} 结构")

    specs: list[FieldSpec] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        specs.append(
            FieldSpec(
                name=str(item["name"]).strip(),
                description=str(item.get("description", "")).strip(),
                aliases=[str(value).strip() for value in item.get("aliases", []) if str(value).strip()],
                value_type=str(item.get("value_type", infer_value_type(item["name"]))).strip(),
                multi_value=bool(item.get("multi_value", False)),
                required=bool(item.get("required", False)),
                regex_patterns=[str(value).strip() for value in item.get("regex_patterns", []) if str(value).strip()],
                keyword_hints=[str(value).strip() for value in item.get("keyword_hints", []) if str(value).strip()],
                entity_hints=[str(value).strip() for value in item.get("entity_hints", []) if str(value).strip()],
                prompt_hint=str(item.get("prompt_hint", "")).strip(),
                retrieval_query=str(item.get("retrieval_query", item["name"])).strip(),
                examples=[str(value).strip() for value in item.get("examples", []) if str(value).strip()],
                template_targets=[str(value).strip() for value in item.get("template_targets", []) if str(value).strip()],
                source_weights={
                    str(key): float(value)
                    for key, value in item.get("source_weights", {}).items()
                },
                schema=item.get("schema", {}) or {},
                normalizer=item.get("normalizer"),
                metadata=item.get("metadata", {}) or {},
                computation=str(item.get("computation", "") or infer_computation_type(item["name"])).strip(),
                source_fields=[str(v).strip() for v in item.get("source_fields", []) if str(v).strip()],
            )
        )
    return specs


def infer_field_specs(
    field_names: list[str],
    contexts: dict[str, str] | None = None,
) -> list[FieldSpec]:
    """从字段名列表自动推断 FieldSpec，用于没有配置文件时的快速初始化。

    可选参数 `contexts` 将字段名映射到其所在段落的上下文文本；
    若提供，则该字段的 `retrieval_query` 会被设为上下文文本，
    让向量检索基于段落语义而非占位符字面意义，减少匹配歧义。
    """
    contexts = contexts or {}
    specs: list[FieldSpec] = []
    for field_name in field_names:
        name = str(field_name).strip()
        if not name:
            continue
        specs.append(build_field_spec(name, context=contexts.get(name)))
    return specs
