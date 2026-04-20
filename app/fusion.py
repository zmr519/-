"""
fusion.py — 多候选融合与冲突解决

对来自规则抽取器和 LLM 抽取器的多个候选值，进行综合评分、去重、
校验，最终为每个字段/记录选出最优结果。
主要提供两个入口：
  - fuse_field_candidates()  : 单字段融合，结果为 {field_name: FusionDecision}
  - fuse_record_candidates() : 记录级融合，结果为 [FusionDecision]
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
import json
import re

from app.schemas import ExtractionCandidate, FieldSpec, FusionDecision, normalize_text


def _validate_value_against_schema(value: Any, schema: dict[str, Any]) -> list[str]:
    """用 jsonschema 校验单个值是否符合字段 Schema。"""
    try:
        import jsonschema

        jsonschema.validate(value, schema)
        return []
    except Exception as exc:
        return [str(exc)]


def _normalize_for_compare(field_spec: FieldSpec, value: Any) -> str:
    text = normalize_text(value)
    if field_spec.value_type in {"number", "currency", "integer", "float"}:
        text = re.sub(r"[,\s元人民币¥￥亿元万元]", "", text)
    return text.lower()


def fuse_field_candidates(
    field_specs: list[FieldSpec],
    candidates_by_field: dict[str, list[ExtractionCandidate]],
) -> dict[str, FusionDecision]:
    """
    单字段融合：对每个字段的多个候选值，按 final_score + confidence 排序，
    校验 Schema，选出最优值并生成可解释的 FusionDecision。
    """
    decisions: dict[str, FusionDecision] = {}

    for field_spec in field_specs:
        field_candidates = list(candidates_by_field.get(field_spec.name, []))
        for candidate in field_candidates:
            value = candidate.values.get(field_spec.name)
            candidate.validation_errors.extend(
                _validate_value_against_schema(value, field_spec.effective_schema())
            )

        valid_candidates = [candidate for candidate in field_candidates if not candidate.validation_errors]

        # 计算多源相互印证加分：相同值出现次数越多，奖励分越高
        corroboration_by_val: dict[str, float] = defaultdict(float)
        for candidate in (valid_candidates or field_candidates):
            val_key = _normalize_for_compare(field_spec, candidate.values.get(field_spec.name, ""))
            if val_key:
                corroboration_by_val[val_key] += 1.0

        def _sort_key(candidate: ExtractionCandidate) -> tuple[float, float]:
            val_key = _normalize_for_compare(field_spec, candidate.values.get(field_spec.name, ""))
            corroboration_bonus = min(0.10, 0.04 * (corroboration_by_val.get(val_key, 1.0) - 1.0))
            return (candidate.final_score() + corroboration_bonus, candidate.confidence)

        ranked = sorted(
            valid_candidates or field_candidates,
            key=_sort_key,
            reverse=True,
        )

        if not ranked:
            decisions[field_spec.name] = FusionDecision(
                subject=field_spec.name,
                final_values={field_spec.name: ""},
                confidence=0.0,
                explanation="未找到可用候选值。",
                selected_candidate_id=None,
                candidates=[],
            )
            continue

        best = ranked[0]
        value = best.values.get(field_spec.name, "")
        alternatives = len(ranked) - 1
        explanation = (
            f"选择该值是因为来源权重={best.source_weight:.2f}、抽取器权重={best.extractor_weight:.2f}、"
            f"证据质量={best.evidence_quality:.2f}、检索分数={best.retrieval_score:.2f}；"
            f"共比较 {len(ranked)} 个候选，保留 {alternatives} 个备选。"
        )
        if best.explanation:
            explanation = f"{best.explanation} {explanation}"

        decisions[field_spec.name] = FusionDecision(
            subject=field_spec.name,
            final_values={field_spec.name: value},
            confidence=round(min(1.0, (best.confidence + best.final_score()) / 2), 4),
            explanation=explanation,
            selected_candidate_id=best.candidate_id,
            candidates=ranked,
        )

    return decisions


def fuse_record_candidates(
    field_specs: list[FieldSpec],
    candidates: list[ExtractionCandidate],
    max_records: int | None = None,
) -> list[FusionDecision]:
    """
    记录级融合：将同一实体的互补候选合并，组内取最优，
    组间按证据得分 + 字段完整度排序，多源确认者获得奖励分。
    """
    if not candidates:
        return []

    # 按实体兼容性分组（非空字段重叠且一致的候选归入同组，互补字段合并）
    groups = _group_compatible_candidates(candidates, field_specs)

    scored_groups: list[tuple[float, dict[str, Any], list[ExtractionCandidate]]] = []
    for merged_values, group_candidates in groups:
        best = max(group_candidates, key=lambda candidate: (candidate.final_score(), candidate.confidence))
        corroboration_bonus = min(0.15, 0.03 * (len(group_candidates) - 1))
        completeness = _record_completeness(merged_values, field_specs)
        score = best.final_score() + corroboration_bonus + completeness * 0.1
        scored_groups.append((score, merged_values, group_candidates))

    scored_groups.sort(key=lambda item: item[0], reverse=True)
    if max_records:
        scored_groups = scored_groups[:max_records]

    decisions: list[FusionDecision] = []
    for _, merged_values, group_candidates in scored_groups:
        best = max(group_candidates, key=lambda candidate: (candidate.final_score(), candidate.confidence))
        explanation = (
            f"该记录来自 {best.extractor}，字段完整度={_record_completeness(merged_values, field_specs):.2f}，"
            f"并由 {len(group_candidates)} 个候选合并确认。"
        )
        if best.explanation:
            explanation = f"{best.explanation} {explanation}"

        decisions.append(
            FusionDecision(
                subject="record",
                final_values=merged_values,
                confidence=round(min(1.0, (best.confidence + best.final_score()) / 2), 4),
                explanation=explanation,
                selected_candidate_id=best.candidate_id,
                candidates=sorted(
                    group_candidates,
                    key=lambda candidate: (candidate.final_score(), candidate.confidence),
                    reverse=True,
                ),
            )
        )
    return decisions


def _values_compatible(
    values_a: dict[str, Any],
    values_b: dict[str, Any],
    field_specs: list[FieldSpec],
) -> bool:
    """判断两组值是否代表同一实体：所有共同非空字段必须一致，且至少有一个共同非空字段。"""
    shared_matches = 0
    for spec in field_specs:
        va = _normalize_for_compare(spec, values_a.get(spec.name, ""))
        vb = _normalize_for_compare(spec, values_b.get(spec.name, ""))
        if va and vb:
            if va != vb:
                return False
            shared_matches += 1
    return shared_matches >= 1


def _group_compatible_candidates(
    candidates: list[ExtractionCandidate],
    field_specs: list[FieldSpec],
) -> list[tuple[dict[str, Any], list[ExtractionCandidate]]]:
    """按实体兼容性分组：非空字段一致的候选归入同组，互补字段合并。

    高分候选的值优先保留。
    """
    # 按得分降序排列，确保高质量值优先写入 merged_values
    sorted_candidates = sorted(
        candidates,
        key=lambda c: (c.final_score(), c.confidence),
        reverse=True,
    )
    groups: list[tuple[dict[str, Any], list[ExtractionCandidate]]] = []

    for candidate in sorted_candidates:
        best_group_idx = -1
        for idx, (merged_vals, _group_cands) in enumerate(groups):
            if _values_compatible(merged_vals, candidate.values, field_specs):
                best_group_idx = idx
                break

        if best_group_idx >= 0:
            merged_vals, group_cands = groups[best_group_idx]
            group_cands.append(candidate)
            # 用当前候选补全缺失字段
            for spec in field_specs:
                existing = _normalize_for_compare(spec, merged_vals.get(spec.name, ""))
                new_val = normalize_text(candidate.values.get(spec.name, ""))
                if not existing and new_val:
                    merged_vals[spec.name] = candidate.values[spec.name]
        else:
            groups.append((dict(candidate.values), [candidate]))

    return groups


def _record_completeness(values: dict[str, Any], field_specs: list[FieldSpec]) -> float:
    if not field_specs:
        return 0.0
    filled = sum(1 for spec in field_specs if normalize_text(values.get(spec.name)))
    return filled / len(field_specs)
