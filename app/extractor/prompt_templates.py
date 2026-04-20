"""
prompt_templates.py — LLM Prompt 模板构建器

为 LLM 抽取器生成结构化 Prompt，包含：
  - 字段定义、输出 JSON Schema、示例输出、候选证据
  主要提供：
  - build_field_extraction_prompt()  : 单字段抽取的 Prompt
  - build_record_extraction_prompt() : 表格记录抽取的 Prompt
  - build_retry_prompt()             : 失败重试时追加的错误提示
"""

from __future__ import annotations

import json
import os

from app.schemas import FieldSpec, SearchHit


def _serialize_hits(hits: list[SearchHit], limit: int = 8, text_limit: int = 400) -> str:
    """将 SearchHit 列表序列化为 JSON 字符串，作为 Prompt 中的证据部分。"""
    payload = []
    for hit in hits[:limit]:
        payload.append(
            {
                "chunk_id": hit.chunk.chunk_id,
                "score": round(hit.score, 4),
                "source_path": hit.chunk.metadata.get("file_path", ""),
                "block_type": hit.chunk.metadata.get("block_type", ""),
                "sheet_name": hit.chunk.metadata.get("sheet_name", ""),
                "row_index": hit.chunk.metadata.get("row_index", ""),
                "text": hit.chunk.text[:text_limit],
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


# 公共证据池字符预算（总上限 ~16686，预留 ~3500 给指令/schema/示例/字段定义）
_EVIDENCE_CHAR_BUDGET = 12500
_EVIDENCE_TEXT_LIMIT = 400


def _build_shared_evidence(
    hits_by_field: dict[str, list[SearchHit]],
    char_budget: int = _EVIDENCE_CHAR_BUDGET,
    text_limit: int = _EVIDENCE_TEXT_LIMIT,
) -> tuple[str, dict[str, list[str]]]:
    """将所有字段的 hits 合并去重，构建公共证据池 JSON 字符串。

    - 同一 chunk 在多个字段命中时只出现一次，标注 relevant_for 字段列表
    - LLM 通过 relevant_for 感知跨字段语义关联（如"总数→增加→增长率"在同一条中）
    - 按 score 降序排列，受 char_budget 约束截断，保证不超出模型上下文
    返回：
      evidence_json   — 直接嵌入 prompt 的 JSON 字符串
      field_chunk_ids — {field_name: [chunk_id, ...]}
    """
    merged: dict[str, tuple[SearchHit, list[str]]] = {}
    for field_name, hits in hits_by_field.items():
        for hit in hits:
            cid = hit.chunk.chunk_id
            if cid not in merged:
                merged[cid] = (hit, [field_name])
            else:
                best_hit, fields = merged[cid]
                if field_name not in fields:
                    fields.append(field_name)
                if hit.score > best_hit.score:
                    merged[cid] = (hit, fields)

    sorted_items = sorted(merged.values(), key=lambda x: x[0].score, reverse=True)

    field_chunk_ids: dict[str, list[str]] = {name: [] for name in hits_by_field}
    for hit, relevant_fields in sorted_items:
        for fn in relevant_fields:
            if fn in field_chunk_ids:
                field_chunk_ids[fn].append(hit.chunk.chunk_id)

    pool: list[dict] = []
    used = 0
    for hit, relevant_fields in sorted_items:
        src = hit.chunk.metadata.get("file_path", "")
        entry = {
            "chunk_id": hit.chunk.chunk_id,
            "score": round(hit.score, 4),
            "source_file": os.path.basename(src) if src else "",
            "relevant_for": relevant_fields,
            "text": hit.chunk.text[:text_limit],
        }
        s = json.dumps(entry, ensure_ascii=False)
        if used + len(s) > char_budget:
            break
        pool.append(entry)
        used += len(s)

    return json.dumps(pool, ensure_ascii=False, indent=2), field_chunk_ids


def build_field_extraction_prompt(
    user_query: str,
    field_specs: list[FieldSpec],
    hits_by_field: dict[str, list[SearchHit]],
) -> str:
    """构建字段抽取 LLM Prompt。

    证据采用共享池结构：所有字段 hits 合并去重，每条标注 relevant_for，
    消除重复 token，同时让 LLM 通过完整上下文感知关联字段语义。
    """
    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_name": {"type": "string"},
                        "value": {},
                        "confidence": {"type": "number"},
                        "evidence_quote": {"type": "string"},
                        "source_chunk_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "field_name",
                        "value",
                        "confidence",
                        "evidence_quote",
                        "source_chunk_id",
                        "reason",
                    ],
                },
            }
        },
        "required": ["results"],
    }

    # 共享证据池：去重 + relevant_for 标注
    evidence_json, field_chunk_ids = _build_shared_evidence(hits_by_field)

    # 字段定义附带关联 chunk_id，便于 LLM 定向查找
    field_payload = []
    for spec in field_specs:
        d = spec.prompt_dict()
        d["相关证据块ID"] = field_chunk_ids.get(spec.name, [])
        field_payload.append(d)

    example = {
        "results": [
            {
                "field_name": field_specs[0].name if field_specs else "负责人",
                "value": field_specs[0].examples[0] if field_specs and field_specs[0].examples else "张三",
                "confidence": 0.96,
                "evidence_quote": "项目负责人：张三",
                "source_chunk_id": "doc1:chunk:12",
                "reason": "字段名与证据中的键值对完全匹配。",
            }
        ]
    }

    no_data_example = {
        "results": [
            {
                "field_name": field_specs[0].name if field_specs else "负责人",
                "value": "",
                "confidence": 0,
                "evidence_quote": "",
                "source_chunk_id": "",
                "reason": "证据中未找到该字段的信息。",
            }
        ]
    }

    return f"""
你是一个严格的信息抽取助手。请只依据给定证据输出 JSON，不要补充解释，不要编造缺失值。

任务要求:
1. 你要抽取用户所需字段，并尽量绑定最直接的证据。
2. 若字段在证据中找不到对应数据，value 必须置为空字符串 ""，confidence 置 0。严禁编造、猜测或使用默认值填充。
3. evidence_quote 必须来自证据原文，尽量短。
4. source_chunk_id 必须来自证据块编号。
5. 只输出一个 JSON 对象，格式必须满足下面的 schema。
6. value 只输出纯数值（如 "4.0"），不要附带单位（如 "‰"、"万"、"个"、"人"、"%"）。单位已在模板中标注，重复会导致错误。
7. 严禁将年份、日期等时间信息作为其他字段的值。例如"2024"只能填入"年份"字段，不能填入"机构数"等其他字段。
8. 字段名中的 " > " 表示多级表头层级关系。例如 "性别分布 > 男" 表示大类是"性别分布"，子列是"男"。请根据证据中的多级结构精确对应数据，切勿混淆同一大类下的不同子列。
9. 若字段语义明确要求聚合计算（如字段名含"总计"、"合计"、"总数"、"总量"、"累计"、"平均"、"增长率"、"占比"等），且证据中提供了足够的原始明细数据但未直接给出聚合结果，你可以基于证据中的数值进行简单计算（加总、求均值、计算比率等），并在 reason 中写明完整计算过程（如 "100 + 200 + 300 = 600"）。此时 confidence 应根据数据完整性适当降低（建议 0.6~0.8）。

用户要求:
{user_query}

字段定义:
{json.dumps(field_payload, ensure_ascii=False, indent=2)}

输出 JSON Schema:
{json.dumps(schema, ensure_ascii=False, indent=2)}

正确示例（找到数据时）:
{json.dumps(example, ensure_ascii=False, indent=2)}

正确示例（未找到数据时 — value 必须为空字符串）:
{json.dumps(no_data_example, ensure_ascii=False, indent=2)}

候选证据（共享证据池，按 score 降序；relevant_for 表示该证据块与哪些字段相关）:
{evidence_json}
""".strip()


def build_record_extraction_prompt(
    user_query: str,
    field_specs: list[FieldSpec],
    hits: list[SearchHit],
    max_records: int | None = None,
) -> str:
    """构建表格记录抽取的 LLM Prompt，侧重多行结构化抽取。"""
    properties = {
        spec.name: spec.effective_schema()
        for spec in field_specs
    }
    schema = {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "values": {
                            "type": "object",
                            "properties": properties,
                            "required": [spec.name for spec in field_specs if spec.required],
                        },
                        "confidence": {"type": "number"},
                        "source_chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_quote": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "values",
                        "confidence",
                        "source_chunk_ids",
                        "evidence_quote",
                        "reason",
                    ],
                },
            }
        },
        "required": ["records"],
    }
    example_values = {
        spec.name: spec.examples[0] if spec.examples else ""
        for spec in field_specs
    }
    example = {
        "records": [
            {
                "values": example_values,
                "confidence": 0.94,
                "source_chunk_ids": ["doc1:row:18"],
                "evidence_quote": "城市: 德州市 | 监测时间: 2025-11-25 09:00:00.0 | PM10监测值: 87",
                "reason": "该行与用户要求中的城市和时间条件完全一致，并且字段覆盖最完整。",
            }
        ]
    }

    limit_hint = f"最多返回 {max_records} 条记录。" if max_records else "返回所有满足条件的记录。"

    # 多级表头提示：当字段名包含 " > " 时，说明模板表格有合并表头
    has_hierarchy = any(" > " in spec.name for spec in field_specs)
    hierarchy_hint = (
        "\n6. 字段名中的 \" > \" 表示多级表头层级关系。"
        "例如 \"性别分布 > 男\" 表示大类是“性别分布”，子列是“男”。"
        "请根据证据中的多级结构精确对应数据，切勿混淆同一大类下的不同子列。"
    ) if has_hierarchy else ""

    return f"""
你是一个严格的结构化表格抽取助手。请从候选证据中抽取可以直接写入模板表格的记录。

抽取规则:
1. 仅依据证据作答，不得编造。
2. 输出必须是 JSON，且满足给定 schema。
3. 记录必须尽可能完整；若个别字段缺失，可保留空字符串。
4. source_chunk_ids 必须引用证据中的 chunk_id。
5. {limit_hint}{hierarchy_hint}

用户要求:
{user_query}

字段定义:
{json.dumps([spec.prompt_dict() for spec in field_specs], ensure_ascii=False, indent=2)}

输出 JSON Schema:
{json.dumps(schema, ensure_ascii=False, indent=2)}

示例输出:
{json.dumps(example, ensure_ascii=False, indent=2)}

候选证据:
{_serialize_hits(hits, limit=8, text_limit=220)}
""".strip()


def build_retry_prompt(previous_prompt: str, validation_error: str) -> str:
    """构建重试 Prompt：在原 Prompt 后追加校验错误信息。"""
    return (
        f"{previous_prompt}\n\n"
        "上一次输出未通过校验，请严格修正后重新输出 JSON。\n"
        f"校验错误: {validation_error}"
    )


def build_computation_prompt(
    field_name: str,
    computation_type: str,
    evidence_text: str,
    user_query: str,
) -> str:
    """构建计算专用 Prompt：让 LLM 从证据中提取相关数值并完成指定计算。"""
    type_desc = {
        "sum": "对所有相关数值求和",
        "average": "对所有相关数值求算术平均",
        "ratio": "计算比率（分子/分母），需从语义判断分子和分母",
        "growth_rate": "计算增长率 = (新值 - 旧值) / 旧值，需从语义判断新旧值",
        "count": "统计符合条件的记录总数",
    }.get(computation_type, "根据字段语义完成所需计算")

    return f"""
你是一个数据计算助手。请根据证据中的数据，计算所需的结果。

计算任务: 求"{field_name}"的值
计算类型: {computation_type} — {type_desc}
用户要求: {user_query}

计算规则:
1. 先从证据中找出所有与"{field_name}"相关的数值，逐一列出
2. 写出完整的计算公式和步骤
3. 给出最终结果（纯数值，不带单位）
4. 若证据中数据不足以完成计算，result 填空字符串 ""，confidence 置 0
5. 不要编造数据，只使用证据中明确出现的数值

请输出 JSON（不要输出其他内容）:
{{"values_found": [数值列表], "formula": "计算公式", "result": "最终数值", "confidence": 0.0}}

证据:
{evidence_text}
""".strip()
