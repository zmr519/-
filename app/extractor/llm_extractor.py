"""
llm_extractor.py — LLM 抽取器

通过调用 OpenAI 兼容的 LLM API（如 vLLM / HuggingFace Router），
从检索到的证据中抽取字段值或表格记录。
内建重试机制：调用 LLM → 解析 JSON → Schema 校验 → 失败则重试。
主要提供：
  - extract_fields()  : 单字段抽取
  - extract_records() : 表格记录抽取
"""

from __future__ import annotations

from typing import Any
import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

from app.extractor.json_utils import extract_json
from app.extractor.prompt_templates import (
    build_field_extraction_prompt,
    build_record_extraction_prompt,
    build_computation_prompt,
    build_retry_prompt,
)
from app.schemas import ExtractionCandidate, FieldSpec, SearchHit, stable_id


DEFAULT_LLM_URL = "https://router.huggingface.co/v1/chat/completions"
DEFAULT_MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"


def _validate_schema(payload: dict[str, Any], schema: dict[str, Any]) -> tuple[bool, str]:
    """用 jsonschema 校验 LLM 返回的 JSON 是否符合预期结构。"""
    try:
        import jsonschema

        jsonschema.validate(payload, schema)
        return True, ""
    except Exception as exc:
        return False, str(exc)


class VLLMExtractor:
    """
    LLM 抽取器：调用远程 LLM 接口，结合 Prompt 模板和 JSON Schema 校验，
    从检索证据中抽取结构化信息。
    """
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.url = settings.get("llm.url", DEFAULT_LLM_URL)
        self.model = settings.get("llm.model", DEFAULT_MODEL_NAME)
        self.api_key_env = str(settings.get("llm.api_key_env", "HF_TOKEN") or "HF_TOKEN").strip()
        self.api_key = str(settings.get("llm.api_key", "") or "").strip() or os.getenv(self.api_key_env, "").strip()
        self.timeout = int(settings.get("llm.timeout_seconds", 90))
        self.max_retries = int(settings.get("llm.max_retries", 3))
        self.temperature = float(settings.get("llm.temperature", 0.1))
        self.use_response_format = bool(settings.get("llm.use_response_format", False))
        self.max_context_tokens = int(settings.get("llm.max_context_tokens", 30000))
        self.field_batch_size = int(settings.get("llm.field_batch_size", 5))
        self.session = requests.Session()

    def extract_fields(
        self,
        user_query: str,
        field_specs: list[FieldSpec],
        hits_by_field: dict[str, list[SearchHit]],
    ) -> list[ExtractionCandidate]:
        """按 field_batch_size 分批调用 LLM，每批独立构建 prompt，避免 prompt 过长截断。"""
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

        # 全局 hit 查找表，所有批次共享
        hit_lookup = {
            hit.chunk.chunk_id: hit
            for hits in hits_by_field.values()
            for hit in hits
        }

        candidates: list[ExtractionCandidate] = []
        # 动态分批：尽量把多个字段合并到同一批次，超出模型上限才拆分
        max_prompt_chars = int((self.max_context_tokens - 50) / 1.8)
        batches = self._dynamic_split(user_query, field_specs, hits_by_field, max_prompt_chars)

        for batch_idx, batch_specs in enumerate(batches):
            batch_hits = {spec.name: hits_by_field.get(spec.name, []) for spec in batch_specs}

            logger.info(
                "字段 LLM 抽取批次 %d/%d（含 %d 个字段），字段: %s",
                batch_idx + 1, len(batches), len(batch_specs),
                [s.name for s in batch_specs],
            )

            try:
                prompt = build_field_extraction_prompt(user_query, batch_specs, batch_hits)
                payload = self._chat_with_retry(prompt=prompt, schema=schema)
                results = payload.get("results", []) if isinstance(payload, dict) else []
            except Exception as exc:
                logger.error("LLM 字段批次抽取失败 (批次 %d/%d): %s", batch_idx + 1, len(batches), exc)
                continue

            for item in results:
                field_name = str(item.get("field_name", "")).strip()
                value = item.get("value")
                if not field_name:
                    continue

                source_chunk_id = str(item.get("source_chunk_id", "")).strip()
                hit = hit_lookup.get(source_chunk_id)
                source_path = ""
                retrieval_score = 0.0
                evidence_quality = 0.72
                if hit:
                    source_path = str(hit.chunk.metadata.get("file_path", ""))
                    retrieval_score = max(0.0, min(1.0, float(hit.score)))
                    evidence_quality = 0.78

                candidates.append(
                    ExtractionCandidate(
                        candidate_id=stable_id("llm", field_name, source_chunk_id, value),
                        values={field_name: value},
                        extractor="llm",
                        confidence=float(item.get("confidence", 0.0) or 0.0),
                        evidence_quote=str(item.get("evidence_quote", "")).strip(),
                        source_chunk_ids=[source_chunk_id] if source_chunk_id else [],
                        source_path=source_path,
                        retrieval_score=retrieval_score,
                        keyword_score=0.0,
                        evidence_quality=evidence_quality,
                        source_weight=0.78,
                        extractor_weight=0.70,
                        recency_score=0.5,
                        explanation=str(item.get("reason", "")).strip(),
                    )
                )

        return candidates

    def extract_records(
        self,
        user_query: str,
        field_specs: list[FieldSpec],
        hits: list[SearchHit],
        max_records: int | None = None,
    ) -> list[ExtractionCandidate]:
        prompt = build_record_extraction_prompt(user_query, field_specs, hits, max_records=max_records)
        schema = {
            "type": "object",
            "properties": {
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "values": {"type": "object"},
                            "confidence": {"type": "number"},
                            "source_chunk_ids": {"type": "array"},
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

        payload = self._chat_with_retry(prompt=prompt, schema=schema)
        records = payload.get("records", []) if isinstance(payload, dict) else []
        hit_lookup = {hit.chunk.chunk_id: hit for hit in hits}

        candidates: list[ExtractionCandidate] = []
        for item in records:
            values = item.get("values", {}) or {}
            source_chunk_ids = [str(value) for value in item.get("source_chunk_ids", []) if str(value).strip()]
            source_path = ""
            retrieval_score = 0.0
            if source_chunk_ids:
                first_hit = hit_lookup.get(source_chunk_ids[0])
                if first_hit:
                    source_path = str(first_hit.chunk.metadata.get("file_path", ""))
                    retrieval_score = max(0.0, min(1.0, float(first_hit.score)))

            candidates.append(
                ExtractionCandidate(
                    candidate_id=stable_id("llm_record", tuple(sorted(values.items())), tuple(source_chunk_ids)),
                    values={str(key): value for key, value in values.items()},
                    extractor="llm",
                    confidence=float(item.get("confidence", 0.0) or 0.0),
                    evidence_quote=str(item.get("evidence_quote", "")).strip(),
                    source_chunk_ids=source_chunk_ids,
                    source_path=source_path,
                    retrieval_score=retrieval_score,
                    keyword_score=0.0,
                    evidence_quality=0.74,
                    source_weight=0.76,
                    extractor_weight=0.70,
                    recency_score=0.5,
                    explanation=str(item.get("reason", "")).strip(),
                )
            )
        return candidates

    def compute_field(
        self,
        field_name: str,
        computation_type: str,
        evidence_text: str,
        user_query: str,
    ) -> dict[str, Any]:
        """调用 LLM 完成复杂计算（比率、增长率等），返回计算结果。"""
        prompt = build_computation_prompt(field_name, computation_type, evidence_text, user_query)
        schema = {
            "type": "object",
            "properties": {
                "values_found": {"type": "array"},
                "formula": {"type": "string"},
                "result": {},
                "confidence": {"type": "number"},
            },
            "required": ["result", "confidence"],
        }
        try:
            payload = self._chat_with_retry(prompt=prompt, schema=schema)
            return payload
        except Exception as exc:
            logger.error("LLM 计算失败 (字段=%s, 类型=%s): %s", field_name, computation_type, exc)
            return {"result": "", "confidence": 0, "formula": "", "values_found": []}

    def _chat_with_retry(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        current_prompt = prompt
        last_error = "unknown_error"

        for attempt in range(1, self.max_retries + 1):
            try:
                raw = self._chat_once(current_prompt)
                payload = extract_json(raw)
                if payload.get("error"):
                    last_error = str(payload["error"])
                    current_prompt = build_retry_prompt(prompt, last_error)
                    continue

                valid, error = _validate_schema(payload, schema)
                if valid:
                    return payload
                last_error = error
                current_prompt = build_retry_prompt(prompt, error)
            except Exception as exc:
                last_error = str(exc)
            time.sleep(min(2 ** (attempt - 1), 5))

        raise RuntimeError(f"LLM 结构化抽取失败: {last_error}")

    def _dynamic_split(
        self,
        user_query: str,
        field_specs: list[FieldSpec],
        hits_by_field: dict[str, list[SearchHit]],
        max_chars: int,
    ) -> list[list[FieldSpec]]:
        """动态分批：尽量把多个字段合并到同一批次，只有 prompt 超出上限时才对半拆分。

        最终每批次的 prompt 长度 <= max_chars，
        最坏情况退化到每个字段一个批次（batch_size=1）。
        """
        if not field_specs:
            return []
        if len(field_specs) == 1:
            return [field_specs]

        batch_hits = {spec.name: hits_by_field.get(spec.name, []) for spec in field_specs}
        prompt = build_field_extraction_prompt(user_query, field_specs, batch_hits)
        if len(prompt) <= max_chars:
            return [field_specs]

        # 超长 → 对半拆分，递归
        mid = len(field_specs) // 2
        left = self._dynamic_split(user_query, field_specs[:mid], hits_by_field, max_chars)
        right = self._dynamic_split(user_query, field_specs[mid:], hits_by_field, max_chars)
        return left + right

    def _truncate_prompt(self, prompt: str) -> str:
        """根据 max_context_tokens 截断 prompt，防止超出模型上下文窗口。"""
        # 粗略估算：中文约 1.5~2 token/字符，英文/数字约 0.3 token/字符
        # 取保守估计 1.8 token/字符，加上 system prompt 约 30 tokens
        max_chars = int((self.max_context_tokens - 50) / 1.8)
        if len(prompt) > max_chars:
            logger.warning(
                "Prompt 过长 (%d 字符 ≈ %d tokens)，已截断至 %d 字符以适配模型上下文窗口",
                len(prompt), int(len(prompt) * 1.8), max_chars,
            )
            prompt = prompt[:max_chars] + "\n...(证据已截断)"
        return prompt

    def _chat_once(self, prompt: str) -> str:
        prompt = self._truncate_prompt(prompt)
        data = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个只输出 JSON 的信息抽取助手。",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": self.temperature,
        }
        if self.use_response_format:
            data["response_format"] = {"type": "json_object"}
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.session.post(self.url, json=data, timeout=self.timeout, headers=headers)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = (response.text or "").strip()
            body_preview = body[:500]
            raise RuntimeError(
                f"LLM HTTP error {response.status_code} for {self.url}; response={body_preview}"
            ) from exc
        payload = response.json()
        return payload["choices"][0]["message"]["content"]


def extract_with_llm(context: str, query: str, settings: Any, fields: list[str] | None = None, source_name: str | None = None) -> str:
    extractor = VLLMExtractor(settings)
    field_specs = [
        FieldSpec(name=field_name, retrieval_query=field_name)
        for field_name in (fields or [])
    ]
    hits_by_field = {}
    if context.strip():
        synthetic_hit = SearchHit(
            chunk=type("ChunkLike", (), {
                "chunk_id": f"legacy:{source_name or 'context'}",
                "text": context,
                "metadata": {"file_path": source_name or "", "file_type": "txt", "block_type": "legacy"},
            })(),
            score=1.0,
            rank=1,
        )
        for spec in field_specs:
            hits_by_field[spec.name] = [synthetic_hit]
    results = extractor.extract_fields(query, field_specs, hits_by_field)
    payload = {
        "results": [
            {
                "field_name": next(iter(candidate.values.keys()), ""),
                "value": next(iter(candidate.values.values()), ""),
                "confidence": candidate.confidence,
                "evidence_quote": candidate.evidence_quote,
                "source_chunk_id": candidate.source_chunk_ids[0] if candidate.source_chunk_ids else "",
                "reason": candidate.explanation,
            }
            for candidate in results
        ]
    }
    return json.dumps(payload, ensure_ascii=False)
