"""
pipeline.py — 核心流水线编排

本文件是整个系统的中枢，Pipeline 类负责将各组件串联为完整的工作流：

  ingest()  → 文档解析 + 切分 + 向量化
  query()   → ingest + 字段检索抽取
  fill()    → ingest + 字段或记录抽取 + 模板填写
  run()     → fill() 的别名

调用顺序（以 fill 表格模式为例）：
  1. ingest()           : 解析文档 → chunk → 构建向量索引
  2. extract_records()  : 检索相关 chunk → 规则抽取 → LLM 抽取 → 融合
  3. smart_fill()       : 将抽取结果写入 Word/Excel 模板
  4. save_json()        : 保存审计报告
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.city_utils import detect_target_city, record_matches_city, text_mentions_city
from app.chunker.simple_chunker import chunk_document_blocks, chunk_two_level
from app.extractor.llm_extractor import VLLMExtractor
from app.extractor.simple_extractor import (
    extract_field_candidates,
    extract_query_constraints,
    extract_record_candidates,
    _row_matches_constraints,
    _parse_datetime,
    _date_in_range,
)
from app.field_specs import infer_field_specs, load_field_specs
from app.filler.smart_filler import smart_fill
from app.filler.utils import (
    build_output_path,
    build_report_path,
    get_template_fields,
    inspect_template,
    save_json,
)
from app.fusion import fuse_field_candidates, fuse_record_candidates
from app.io_utils import read_text_with_fallback
from app.parser.dispatcher import parse_file
from app.retriever.simple_retriever import Retriever
from app.schemas import FieldSpec, ParsedDocument, SearchHit, Chunk, dataclass_to_dict, normalize_field_name, stable_id

logger = logging.getLogger(__name__)


class Pipeline:
    """
    核心流水线，封装了全部组件：
      - retriever     : 向量检索器（Embedding + VectorIndex）
      - llm_extractor : LLM 抽取器（调用 vLLM/HuggingFace）
      - documents     : 已解析的文档列表
      - chunks        : 切分后的全部 Chunk
    """
    def __init__(self, settings: Any):
        self.settings = settings
        self.documents: list[ParsedDocument] = []
        self.chunks = []
        self._parent_lookup: dict[str, object] = {}
        self._source_headers: list[str] = []  # ingest 阶段收集到的源数据列头
        self.retriever = Retriever(settings)
        self.llm_extractor = VLLMExtractor(settings)

    def reset(self) -> None:
        self.documents = []
        self.chunks = []
        self._parent_lookup = {}
        self._source_headers = []

    def _normalize_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()

    def _load_query_text(self, query: str | None = None, query_file: str | None = None) -> str:
        """加载用户查询文本：优先从文件读取，否则用 query 字符串。"""
        if query_file:
            path = Path(query_file)
            if not path.exists():
                raise FileNotFoundError(f"需求文件不存在: {query_file}")
            if path.suffix.lower() in {".txt", ".md", ".docx"}:
                parsed = parse_file(str(path))
                return parsed.text.strip()
            return read_text_with_fallback(path).strip()
        if not query:
            raise ValueError("必须提供 query 或 query_file")
        return query.strip()

    def _resolve_field_specs(
        self,
        fields: list[str] | None = None,
        field_specs_path: str | None = None,
        template: str | None = None,
        contexts: dict[str, str] | None = None,
    ) -> list[FieldSpec]:
        """
        解析字段规格，优先级：
        field_specs_path > fields 列表 > 模板表头推断。

        `contexts` 可将字段名映射到其所在段落上下文，传入时会作为检索 query。
        """
        if field_specs_path:
            return load_field_specs(field_specs_path)
        if fields:
            return infer_field_specs(fields, contexts=contexts)
        if template:
            return infer_field_specs(get_template_fields(template))
        return []

    def _detect_auxiliary_paths(self, directory: str) -> list[str]:
        auxiliary_keywords = ("模板", "用户要求")
        exclude_paths: list[str] = []
        for root, _, files in os.walk(directory):
            for file_name in files:
                if any(keyword in file_name for keyword in auxiliary_keywords):
                    exclude_paths.append(str(Path(root) / file_name))
        return exclude_paths

    def ingest(self, directory: str, exclude_paths: list[str] | None = None):
        """
        文档注入：遍历目录 → 解析每个文件 → Chunk 切分 → 构建向量索引。
        这是所有后续操作（检索、抽取、填表）的前提。
        """
        if not directory:
            raise ValueError("必须提供目录")

        exclude_set = {
            str(Path(path).resolve())
            for path in (exclude_paths or [])
            if path
        }

        self.reset()
        supported_suffixes = {".txt", ".md", ".docx", ".xlsx"}

        for root, _, files in os.walk(directory):
            for file_name in files:
                path = Path(root) / file_name
                if str(path.resolve()) in exclude_set:
                    continue
                if path.suffix.lower() not in supported_suffixes:
                    continue

                try:
                    document = parse_file(str(path))
                    # --- 方案 B：为每个文件生成文件名元 chunk ---
                    file_stem = path.stem  # 不含后缀的文件名
                    meta_chunk = Chunk(
                        chunk_id=stable_id(document.doc_id, "file_meta"),
                        doc_id=document.doc_id,
                        text=f"文件名: {path.name}\n标题: {file_stem}",
                        metadata={
                            "block_type": "file_meta",
                            "file_path": str(path),
                            "file_name": path.name,
                        },
                    )

                    if self.settings.get("retrieval.use_parent_context", False):
                        chunked_blocks, doc_parents = chunk_two_level(
                            document.chunks,
                            child_size=int(self.settings.get("retrieval.child_chunk_size", 300)),
                            child_overlap=int(self.settings.get("retrieval.child_overlap", 50)),
                        )
                        self._parent_lookup.update(doc_parents)
                    else:
                        chunked_blocks = chunk_document_blocks(
                            document.chunks,
                            chunk_size=int(self.settings.get("retrieval.chunk_size", 600)),
                            overlap=int(self.settings.get("retrieval.chunk_overlap", 80)),
                        )

                    # --- 方案 A：给每个 child chunk 的 text 加文件名前缀 ---
                    source_prefix = f"[来源: {file_stem}]\n"
                    for chunk in chunked_blocks:
                        # 只给 child chunk 加前缀（parent chunk 不加，避免多余重复）
                        if not chunk.metadata.get("is_parent"):
                            chunk.text = source_prefix + chunk.text
                        # 确保每个 chunk 都有 file_name 元数据
                        chunk.metadata.setdefault("file_name", path.name)

                    document.chunks = chunked_blocks
                    if not document.text.strip():
                        logger.warning("跳过空文档: %s", path.name)
                        continue

                    self.documents.append(document)
                    self.chunks.append(meta_chunk)   # 文件名元 chunk 也加入索引
                    self.chunks.extend(chunked_blocks)
                    logger.info("已解析文档: %s (%s chunks + 1 meta)", path.name, len(chunked_blocks))
                except Exception as exc:
                    logger.exception("解析失败 %s: %s", path.name, exc)

        if self.chunks:
            self.retriever.build(
                self.chunks,
                parent_lookup=self._parent_lookup if self.settings.get("retrieval.use_parent_context", False) else None,
            )

        # 收集 xlsx 结构化行的列头，用于后续中英文跨语言对齐
        seen_headers: set[str] = set()
        for chunk in self.chunks:
            row = chunk.metadata.get("structured_row")
            if isinstance(row, dict):
                for key in row:
                    k = str(key).strip()
                    if k and k not in seen_headers:
                        seen_headers.add(k)
        self._source_headers = list(seen_headers)
        if self._source_headers:
            logger.info("收集到源数据列头 %d 个，将用于跨语言字段对齐", len(self._source_headers))

        logger.info("文档加载完成，共 %s 份文档，%s 个 chunk", len(self.documents), len(self.chunks))
        return self.documents

    def extract_fields(
        self,
        query: str | None = None,
        query_file: str | None = None,
        fields: list[str] | None = None,
        field_specs_path: str | None = None,
        directory: str | None = None,
        contexts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        字段抽取流程：
        1. 为每个字段检索相关 chunk
        2. 规则抽取候选值
        3. 规则不足时调用 LLM 补充
        4. 融合决策选出最优值

        `contexts` 可选：field_name → 段落上下文，传入后会作为向量检索 query，
        让检索基于段落语义，避免占位符本身的示例/单位说明干扰。
        """
        if directory and not self.documents:
            exclude_paths = self._detect_auxiliary_paths(directory)
            if query_file:
                exclude_paths.append(query_file)
            self.ingest(directory, exclude_paths=exclude_paths)
        if not self.documents:
            raise ValueError("请先导入文档")

        query_text = self._load_query_text(query=query, query_file=query_file)
        field_specs = self._resolve_field_specs(
            fields=fields,
            field_specs_path=field_specs_path,
            contexts=contexts,
        )
        if not field_specs:
            raise ValueError("字段模式下必须提供 fields、field_specs_path 或 template")

        # 跨语言对齐：把源数据英文列头映射到中文字段 aliases
        self._align_source_headers(field_specs)

        hits_by_field = {
            spec.name: self.retriever.search_for_field(
                spec,
                user_query=query_text,
                top_k=int(self.settings.get("retrieval.top_k", 8)),
            )
            for spec in field_specs
        }

        candidates_by_field = {
            spec.name: extract_field_candidates(spec, hits_by_field.get(spec.name, []))
            for spec in field_specs
        }

        llm_needed = [
            spec
            for spec in field_specs
            if not candidates_by_field.get(spec.name)
        ]
        llm_error = ""
        if llm_needed:
            try:
                llm_candidates = self.llm_extractor.extract_fields(
                    user_query=query_text,
                    field_specs=llm_needed,
                    hits_by_field={spec.name: hits_by_field.get(spec.name, []) for spec in llm_needed},
                )
                for candidate in llm_candidates:
                    for field_name in candidate.values:
                        candidates_by_field.setdefault(field_name, []).append(candidate)
            except Exception as exc:
                llm_error = str(exc)
                logger.error("LLM 抽取失败，已回退到规则结果: %s", llm_error)

        decisions = fuse_field_candidates(field_specs, candidates_by_field)
        fused_data = {
            field_name: next(iter(decision.final_values.values()), "")
            for field_name, decision in decisions.items()
        }

        # 后置计算：对需要聚合/计算的字段，用 Python 精确计算或 LLM 辅助计算
        fused_data = self._post_compute(fused_data, field_specs, hits_by_field, query_text)

        return {
            "mode": "fields",
            "query": query_text,
            "field_specs": [spec.prompt_dict() for spec in field_specs],
            "fused_data": fused_data,
            "decisions": {field_name: decision.to_dict() for field_name, decision in decisions.items()},
            "retrieval": {
                field_name: [hit.to_dict() for hit in hits]
                for field_name, hits in hits_by_field.items()
            },
            "documents": [document.to_dict() for document in self.documents],
            "llm_error": llm_error,
        }

    def extract_records(
        self,
        query: str | None = None,
        query_file: str | None = None,
        template: str | None = None,
        field_specs_path: str | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """
        记录抽取流程（用于表格填写）：
        1. 解析模板布局，获取各表格的表头和目标城市
        2. 对每个表格分别检索 + 抽取 + 城市校验
        3. 融合多个抽取器的候选记录
        """
        if directory and not self.documents:
            exclude_paths = self._detect_auxiliary_paths(directory)
            if query_file:
                exclude_paths.append(query_file)
            if template:
                exclude_paths.append(template)
            self.ingest(directory, exclude_paths=exclude_paths)
        if not self.documents:
            raise ValueError("请先导入文档")

        query_text = self._load_query_text(query=query, query_file=query_file)
        layout = inspect_template(template) if template else None
        constraints = extract_query_constraints(query_text)

        if layout and len(layout.table_targets) > 1:
            table_records: dict[str, list[dict[str, Any]]] = {}
            table_details: dict[str, dict[str, Any]] = {}
            all_records: list[dict[str, Any]] = []

            for target in layout.table_targets:
                target_city = target.target_city or detect_target_city([target.context_text])
                table_query_parts = [query_text]
                if target.context_text:
                    table_query_parts.append(f"模板上下文:\n{target.context_text}")
                if target_city:
                    table_query_parts.append(f"模板目标城市：{target_city}")
                table_query = "\n\n".join(part for part in table_query_parts if part)
                target_specs = infer_field_specs(target.headers)
                target_max_records = constraints.max_records
                if self._table_requires_city_lock(target) and not target_city:
                    table_result = self._build_empty_table_result(
                        query_text=table_query,
                        field_specs=target_specs,
                        target_city="",
                        message="未能从模板标题、前言、表头或已有内容中识别目标城市，已按城市约束留空。",
                    )
                    table_records[target.identifier] = []
                    table_details[target.identifier] = table_result
                    continue
                table_result = self._extract_records_for_specs(
                    query_text=table_query,
                    field_specs=target_specs,
                    max_records=target_max_records,
                    locked_city=target_city or None,
                )
                table_result = self._apply_table_city_validation(
                    target=target,
                    table_result=table_result,
                    target_city=target_city,
                )
                table_records[target.identifier] = table_result["records"]
                table_details[target.identifier] = table_result
                all_records.extend(table_result["records"])

            return {
                "mode": "records",
                "query": query_text,
                "records": all_records,
                "table_records": table_records,
                "table_details": table_details,
                "documents": [document.to_dict() for document in self.documents],
                "template_layout": layout.to_dict() if layout else None,
            }

        field_specs = self._resolve_field_specs(
            fields=layout.all_fields() if layout else None,
            field_specs_path=field_specs_path,
            template=template,
        )
        if not field_specs:
            raise ValueError("记录模式下必须提供 template 或 field_specs_path")

        max_records = constraints.max_records

        result = self._extract_records_for_specs(
            query_text=query_text,
            field_specs=field_specs,
            max_records=max_records,
            locked_city=layout.table_targets[0].target_city if layout and len(layout.table_targets) == 1 else None,
        )
        if layout and len(layout.table_targets) == 1:
            result = self._apply_table_city_validation(
                target=layout.table_targets[0],
                table_result=result,
                target_city=layout.table_targets[0].target_city,
            )
        result["documents"] = [document.to_dict() for document in self.documents]
        result["template_layout"] = layout.to_dict() if layout else None
        return result

    def query(
        self,
        query: str | None = None,
        query_file: str | None = None,
        directory: str | None = None,
        fields: list[str] | None = None,
        field_specs_path: str | None = None,
    ) -> dict[str, Any]:
        """查询模式入口：ingest + extract_fields，返回抽取结果 JSON。"""
        auto_excludes = self._detect_auxiliary_paths(directory) if directory else []
        if directory and not self.documents:
            self.ingest(
                directory,
                exclude_paths=[
                    *auto_excludes,
                    *([query_file] if query_file else []),
                ],
            )
        return self.extract_fields(
            query=query,
            query_file=query_file,
            fields=fields,
            field_specs_path=field_specs_path,
            directory=None,
        )

    def fill(
        self,
        template: str,
        query: str | None = None,
        query_file: str | None = None,
        directory: str | None = None,
        output: str | None = None,
        field_specs_path: str | None = None,
    ) -> dict[str, Any]:
        """
        模板填写入口：
        1. 解析模板布局，判断是表格模式还是占位符模式
        2. 调用 extract_records 或 extract_fields
        3. 调用 smart_fill 写入模板
        4. 保存审计报告 JSON
        """
        if not template:
            raise ValueError("必须提供模板路径")
        if not Path(template).exists():
            raise FileNotFoundError(f"模板不存在: {template}")

        layout = inspect_template(template)

        # 混合模式：既有段落占位符字段，又有表格需要填写
        has_placeholder_fields = bool(layout.placeholder_fields)

        if layout.is_tabular and has_placeholder_fields:
            # 混合模式：含 【xxx】 占位符段落 + 表格
            # 若已 ingest 则跳过重复解析
            if directory and not self.documents:
                excl = self._detect_auxiliary_paths(directory)
                if template:
                    excl.append(template)
                if query_file:
                    excl.append(query_file)
                self.ingest(directory, exclude_paths=excl)

            field_extraction = self.extract_fields(
                query=query,
                query_file=query_file,
                fields=layout.placeholder_fields,
                field_specs_path=field_specs_path,
                directory=None,
                contexts=layout.placeholder_contexts,
            )
            record_extraction = self.extract_records(
                query=query,
                query_file=query_file,
                template=template,
                field_specs_path=field_specs_path,
                directory=None,
            )
            cn_values = dict(field_extraction.get("fused_data", {}))
            # 用表格抽取结果反哺 fused_data：若某字段为空但表格记录中已抽到对应值，则回填
            for records_list in record_extraction.get("table_records", {}).values():
                for rec in records_list:
                    for rec_key, rec_val in rec.items():
                        val_str = str(rec_val).strip() if rec_val is not None else ""
                        if not val_str:
                            continue
                        for cn_key in cn_values:
                            if cn_values[cn_key]:
                                continue  # 已有值，不覆盖
                            if normalize_field_name(rec_key) in normalize_field_name(cn_key) or \
                               normalize_field_name(cn_key) in normalize_field_name(rec_key):
                                cn_values[cn_key] = val_str
            payload = {
                "cn_values": cn_values,
                "records": record_extraction.get("records", []),
                "single_record": {},
                "table_records": record_extraction.get("table_records", {}),
            }
            extraction = {
                "mode": "mixed",
                "fused_data": cn_values,
                **{k: v for k, v in record_extraction.items() if k != "mode"},
            }

        elif layout.is_tabular:
            extraction = self.extract_records(
                query=query,
                query_file=query_file,
                template=template,
                field_specs_path=field_specs_path,
                directory=directory,
            )
            payload = {
                "records": extraction.get("records", []),
                "single_record": extraction.get("records", [{}])[0] if extraction.get("records") else {},
                "table_records": extraction.get("table_records", {}),
            }
        else:
            extraction = self.extract_fields(
                query=query,
                query_file=query_file,
                fields=layout.all_fields(),
                field_specs_path=field_specs_path,
                directory=directory,
                contexts=layout.placeholder_contexts,
            )
            payload = {"cn_values": extraction["fused_data"]}

        output_dir = self.settings.get("output.dir", "outputs")
        output_path = Path(output) if output else build_output_path(template, output_dir)
        smart_fill(template, payload, str(output_path))

        report = {
            "template": str(Path(template).resolve()),
            "output": str(output_path.resolve()),
            "document_count": len(self.documents),
            "template_layout": layout.to_dict(),
            "extraction": extraction,
        }
        report_path = build_report_path(output_path)
        save_json(report, report_path)

        return {
            "output_path": str(output_path.resolve()),
            "report_path": str(report_path.resolve()),
            "document_count": len(self.documents),
            "mode": extraction["mode"],
            "filled_records": extraction.get("records", []),
            "fused_data": extraction.get("fused_data", {}),
        }

    def run(
        self,
        directory: str,
        template: str,
        query: str | None = None,
        query_file: str | None = None,
        output: str | None = None,
        field_specs_path: str | None = None,
    ) -> dict[str, Any]:
        """fill() 的别名，对应 CLI 的 run 子命令。"""
        return self.fill(
            template=template,
            query=query,
            query_file=query_file,
            directory=directory,
            output=output,
            field_specs_path=field_specs_path,
        )

    # ---------- 后置计算 ----------

    def _post_compute(
        self,
        fused_data: dict[str, Any],
        field_specs: list[FieldSpec],
        hits_by_field: dict[str, list[SearchHit]],
        query_text: str,
    ) -> dict[str, Any]:
        """对标记了 computation 的字段，基于已提取的数据或证据做二次计算。

        简单聚合（sum / average / count）由 Python 精确完成；
        复杂计算（ratio / growth_rate）交给 LLM 用专门的计算 prompt。
        仅当融合阶段未能提取到值时才触发。
        """
        for spec in field_specs:
            if not spec.computation:
                continue
            # 已有值则跳过
            existing = self._normalize_value(fused_data.get(spec.name, ""))
            if existing:
                continue

            hits = hits_by_field.get(spec.name, [])
            if not hits:
                continue

            if spec.computation in ("sum", "average", "count"):
                result = self._compute_simple(spec, hits, fused_data)
                if result is not None:
                    fused_data[spec.name] = str(result)
                    logger.info(
                        "[后置计算] 字段 '%s' (%s) = %s",
                        spec.name, spec.computation, result,
                    )
            elif spec.computation in ("ratio", "growth_rate"):
                result = self._compute_via_llm(spec, hits, query_text)
                if result:
                    fused_data[spec.name] = result
                    logger.info(
                        "[后置计算] 字段 '%s' (%s) = %s (LLM)",
                        spec.name, spec.computation, result,
                    )

        return fused_data

    def _extract_numeric_values(self, hits: list[SearchHit]) -> list[float]:
        """从检索命中的 chunk 中提取所有数值。"""
        import re
        values: list[float] = []
        for hit in hits:
            # 优先从结构化行中提取
            structured_row = hit.chunk.metadata.get("structured_row")
            if isinstance(structured_row, dict):
                for v in structured_row.values():
                    try:
                        cleaned = re.sub(r"[,\s]", "", str(v))
                        values.append(float(cleaned))
                    except (ValueError, TypeError):
                        continue
            else:
                # 从文本中提取数值
                for match in re.finditer(r"-?\d[\d,]*(?:\.\d+)?", hit.chunk.text):
                    try:
                        cleaned = match.group().replace(",", "")
                        values.append(float(cleaned))
                    except ValueError:
                        continue
        return values

    def _compute_simple(
        self,
        spec: FieldSpec,
        hits: list[SearchHit],
        fused_data: dict[str, Any],
    ) -> float | int | None:
        """用 Python 完成简单聚合计算（sum / average / count）。

        优先尝试从 fused_data 中已提取到的 source_fields 汇总；
        回退到从证据 chunk 中提取数值。
        """
        # 尝试从已提取的其他字段值中聚合
        if spec.source_fields:
            source_values: list[float] = []
            for sf in spec.source_fields:
                raw = self._normalize_value(fused_data.get(sf, ""))
                if raw:
                    import re
                    cleaned = re.sub(r"[,\s]", "", raw)
                    try:
                        source_values.append(float(cleaned))
                    except ValueError:
                        pass
            if source_values:
                if spec.computation == "sum":
                    return round(sum(source_values), 4)
                elif spec.computation == "average":
                    return round(sum(source_values) / len(source_values), 4)
                elif spec.computation == "count":
                    return len(source_values)

        # 回退：从证据 chunk 中提取数值
        numeric_values = self._extract_numeric_values(hits)
        if not numeric_values:
            return None

        if spec.computation == "sum":
            return round(sum(numeric_values), 4)
        elif spec.computation == "average":
            return round(sum(numeric_values) / len(numeric_values), 4)
        elif spec.computation == "count":
            return len(numeric_values)
        return None

    def _compute_via_llm(
        self,
        spec: FieldSpec,
        hits: list[SearchHit],
        query_text: str,
    ) -> str:
        """复杂计算交给 LLM（ratio / growth_rate）。"""
        evidence_parts = []
        for hit in hits[:8]:
            evidence_parts.append(hit.chunk.text[:400])
        evidence_text = "\n---\n".join(evidence_parts)

        try:
            result = self.llm_extractor.compute_field(
                field_name=spec.name,
                computation_type=spec.computation,
                evidence_text=evidence_text,
                user_query=query_text,
            )
            value = str(result.get("result", "")).strip()
            confidence = float(result.get("confidence", 0))
            if value and confidence > 0.3:
                formula = result.get("formula", "")
                if formula:
                    logger.info("[后置计算] LLM 计算过程: %s", formula)
                return value
        except Exception as exc:
            logger.error("[后置计算] LLM 计算失败 (字段=%s): %s", spec.name, exc)
        return ""

    # ---------- 跨语言对齐 ----------

    def _align_source_headers(
        self,
        field_specs: list[FieldSpec],
        threshold: float | None = None,
    ) -> None:
        """用 embedding 模型将源数据列头（英文）语义对齐到模板字段（中文）。

        匹配结果自动追加进对应 FieldSpec.aliases，让后续的结构化行映射和 LLM Prompt
        都能识别英文列头。

        使用用户已配置的多语言 embedding 模型（paraphrase-multilingual-MiniLM-L12-v2 等），
        中英文语义相近的词向量余弦相似度通常 > 0.75。
        """
        if not self._source_headers or not field_specs:
            return

        cos_threshold = threshold or float(self.settings.get("retrieval.header_align_threshold", 0.75))

        try:
            import numpy as np
        except ImportError:
            logger.warning("跨语言对齐需要 numpy，未安装，跳过")
            return

        # 编码源数据列头
        try:
            header_vecs = np.asarray(
                self.retriever.embedder.encode(self._source_headers), dtype="float32"
            )  # (H, D)
        except Exception as exc:
            logger.warning("列头 embedding 失败，跳过跨语言对齐: %s", exc)
            return

        # 编码字段名（字段名 + 现有 aliases）
        spec_texts = [
            spec.name + (" " + " ".join(spec.aliases) if spec.aliases else "")
            for spec in field_specs
        ]
        try:
            spec_vecs = np.asarray(
                self.retriever.embedder.encode(spec_texts), dtype="float32"
            )  # (S, D)
        except Exception as exc:
            logger.warning("字段 embedding 失败，跳过跨语言对齐: %s", exc)
            return

        # 余弦相似度矩阵 (S, H)
        sim_matrix = spec_vecs @ header_vecs.T

        added_count = 0
        for s_idx, spec in enumerate(field_specs):
            existing_aliases = {a.lower() for a in spec.aliases}
            for h_idx, header in enumerate(self._source_headers):
                if sim_matrix[s_idx, h_idx] >= cos_threshold:
                    if header.lower() not in existing_aliases:
                        spec.aliases.append(header)
                        existing_aliases.add(header.lower())
                        added_count += 1
                        logger.debug(
                            "[跨语言对齐] '%s' → '%s' (sim=%.3f)",
                            spec.name, header, sim_matrix[s_idx, h_idx],
                        )

        if added_count:
            logger.info("[跨语言对齐] 共添加 %d 个英文列头到对应字段 aliases", added_count)

    def _extract_records_for_specs(
        self,
        query_text: str,
        field_specs: list[FieldSpec],
        max_records: int | None = None,
        locked_city: str | None = None,
    ) -> dict[str, Any]:
        logger.info(
            "[表格抽取] field_specs=%d 个字段: %s | max_records=%s | locked_city=%s",
            len(field_specs),
            [s.name for s in field_specs],
            max_records,
            locked_city,
        )
        # 跨语言对齐：把源数据英文列头映射到中文字段 aliases
        self._align_source_headers(field_specs)

        constraints = extract_query_constraints(query_text)
        if locked_city:
            constraints.locations = [locked_city]
        search_query = "\n".join(
            [
                query_text,
                f"目标城市 {locked_city}" if locked_city else "",
                " ".join(spec.name for spec in field_specs),
                " ".join(alias for spec in field_specs for alias in spec.aliases),
            ]
        ).strip()
        configured_top_k = int(self.settings.get("retrieval.record_top_k", 20))
        if max_records is None:
            search_top_k = max(configured_top_k, len(self.chunks))
        else:
            search_top_k = max(configured_top_k, max_records * 4)
        hits = self._select_record_hits(
            query_text=query_text,
            constraints=constraints,
            search_query=search_query,
            search_top_k=search_top_k,
        )
        original_hit_count = len(hits)
        logger.info("[表格抽取] 检索到 %d 条 hits (top_k=%d)", original_hit_count, search_top_k)
        if locked_city:
            hits = self._filter_hits_by_city(hits, locked_city)

        if locked_city and not hits:
            return self._build_empty_table_result(
                query_text=query_text,
                field_specs=field_specs,
                target_city=locked_city,
                message="未找到符合当前城市约束的数据。",
                retrieval=[],
                city_validation={
                    "target_city": locked_city,
                    "retrieval_hits_before_city_filter": original_hit_count,
                    "retrieval_hits_after_city_filter": 0,
                    "cross_city_rows_removed": 0,
                    "message": "未找到符合当前城市约束的数据。",
                },
            )

        rule_candidates = extract_record_candidates(field_specs, hits, query_text=query_text)
        logger.info("[表格抽取] 规则候选 %d 条", len(rule_candidates))
        if locked_city:
            rule_candidates = [
                candidate
                for candidate in rule_candidates
                if self._candidate_matches_city(candidate, locked_city)
            ]
        candidates = list(rule_candidates)
        llm_error = ""

        if not candidates or len(candidates) < min(3, max_records or 3):
            try:
                llm_candidates = self.llm_extractor.extract_records(
                    user_query=query_text,
                    field_specs=field_specs,
                    hits=hits[: min(len(hits), max(8, (max_records or 4) * 2))],
                    max_records=max_records,
                )
                if locked_city:
                    llm_candidates = [
                        candidate
                        for candidate in llm_candidates
                        if self._candidate_matches_city(candidate, locked_city)
                    ]
                candidates.extend(llm_candidates)
            except Exception as exc:
                llm_error = str(exc)
                logger.error("LLM 记录抽取失败，已回退到规则结果: %s", llm_error)

        decisions = fuse_record_candidates(field_specs, candidates, max_records=max_records)
        kept_records: list[dict[str, Any]] = []
        kept_decisions = []
        removed_cross_city_rows = 0
        for decision in decisions:
            record = decision.final_values
            if locked_city and not record_matches_city(record, locked_city):
                removed_cross_city_rows += 1
                continue
            kept_records.append(record)
            kept_decisions.append(decision)

        return {
            "mode": "records",
            "query": query_text,
            "field_specs": [spec.prompt_dict() for spec in field_specs],
            "records": kept_records,
            "decisions": [decision.to_dict() for decision in kept_decisions],
            "retrieval": [hit.to_dict() for hit in hits],
            "llm_error": llm_error,
            "target_city": locked_city or "",
            "city_validation": {
                "target_city": locked_city or "",
                "retrieval_hits_before_city_filter": original_hit_count,
                "retrieval_hits_after_city_filter": len(hits),
                "cross_city_rows_removed": removed_cross_city_rows,
                "message": "未找到符合当前城市约束的数据。" if locked_city and not kept_records else "",
            },
        }

    def _select_record_hits(
        self,
        query_text: str,
        constraints: Any,
        search_query: str,
        search_top_k: int,
    ) -> list[SearchHit]:
        if constraints.active():
            exact_hits: list[SearchHit] = []
            narrative_hits: list[SearchHit] = []
            start_dt = _parse_datetime(constraints.date_range[0]) if constraints.date_range[0] else None
            end_dt = _parse_datetime(constraints.date_range[1]) if constraints.date_range[1] else None
            for chunk in self.chunks:
                structured_row = chunk.metadata.get("structured_row")
                if isinstance(structured_row, dict):
                    if not _row_matches_constraints(structured_row, constraints):
                        continue
                    exact_hits.append(
                        SearchHit(
                            chunk=chunk,
                            score=1.0,
                            rank=len(exact_hits) + 1,
                            keyword_score=1.0,
                            vector_score=1.0,
                        )
                    )
                elif start_dt or end_dt:
                    # 对非结构化段落（如docx），检查date_context是否在约束范围内
                    date_context_str = chunk.metadata.get("date_context", "")
                    if date_context_str:
                        chunk_dt = _parse_datetime(date_context_str)
                        if chunk_dt and _date_in_range(chunk_dt, start_dt, end_dt):
                            narrative_hits.append(
                                SearchHit(
                                    chunk=chunk,
                                    score=0.85,
                                    rank=len(narrative_hits) + 1,
                                    keyword_score=0.85,
                                    vector_score=0.85,
                                )
                            )
            if exact_hits or narrative_hits:
                all_hits = exact_hits + narrative_hits
                for i, hit in enumerate(all_hits):
                    hit.rank = i + 1
                logger.info(
                    "记录抽取命中 %s 条结构化行 + %s 条叙述性段落（约束匹配）。",
                    len(exact_hits),
                    len(narrative_hits),
                )
                return all_hits

        return self.retriever.search(
            query=search_query,
            top_k=search_top_k,
        )

    def _table_requires_city_lock(self, target: Any) -> bool:
        header_keys = {normalize_field_name(header) for header in getattr(target, "headers", [])}
        city_like_keys = {
            normalize_field_name("城市"),
            normalize_field_name("地市"),
            normalize_field_name("所属城市"),
        }
        return bool(getattr(target, "target_city", "")) or bool(header_keys & city_like_keys)

    def _build_empty_table_result(
        self,
        query_text: str,
        field_specs: list[FieldSpec],
        target_city: str,
        message: str,
        retrieval: list[dict[str, Any]] | None = None,
        city_validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "mode": "records",
            "query": query_text,
            "field_specs": [spec.prompt_dict() for spec in field_specs],
            "records": [],
            "decisions": [],
            "retrieval": retrieval or [],
            "llm_error": "",
            "target_city": target_city,
            "city_validation": city_validation or {
                "target_city": target_city,
                "cross_city_rows_removed": 0,
                "message": message,
            },
        }

    def _hit_matches_city(self, hit: SearchHit, target_city: str) -> bool:
        structured_row = hit.chunk.metadata.get("structured_row")
        if isinstance(structured_row, dict) and structured_row:
            return record_matches_city(structured_row, target_city)

        metadata = hit.chunk.metadata or {}
        text_candidates = [
            metadata.get("entity_context"),
            metadata.get("title_path"),
            hit.chunk.text,
        ]
        return any(text_mentions_city(text, target_city) for text in text_candidates)

    def _filter_hits_by_city(self, hits: list[SearchHit], target_city: str) -> list[SearchHit]:
        filtered = [hit for hit in hits if self._hit_matches_city(hit, target_city)]
        if not filtered:
            logger.warning("模板目标城市=%s，但检索结果中没有命中同城数据。", target_city)
        return filtered

    def _candidate_matches_city(self, candidate: Any, target_city: str) -> bool:
        if record_matches_city(candidate.values, target_city):
            return True
        return text_mentions_city(candidate.evidence_quote, target_city)

    def _apply_table_city_validation(
        self,
        target: Any,
        table_result: dict[str, Any],
        target_city: str,
    ) -> dict[str, Any]:
        validation = dict(table_result.get("city_validation", {}))
        validation.setdefault("target_city", target_city)
        validation.setdefault("cross_city_rows_removed", 0)

        if not target_city and self._table_requires_city_lock(target):
            validation["message"] = "未能从模板中确认目标城市，已按规则留空。"
            table_result["records"] = []
            table_result["decisions"] = []
            table_result["target_city"] = ""
            table_result["city_validation"] = validation
            return table_result

        filtered_records = []
        filtered_decisions = []
        for record, decision in zip(table_result.get("records", []), table_result.get("decisions", [])):
            if target_city and not record_matches_city(record, target_city):
                validation["cross_city_rows_removed"] += 1
                continue
            filtered_records.append(record)
            filtered_decisions.append(decision)

        table_result["records"] = filtered_records
        table_result["decisions"] = filtered_decisions
        table_result["target_city"] = target_city
        if target_city and filtered_records:
            validation["message"] = f"已按模板城市 {target_city} 完成校验，仅保留同城记录。"
        elif target_city:
            validation["message"] = "未找到符合当前城市约束的数据。"
        else:
            validation.setdefault("message", "")
        table_result["city_validation"] = validation
        return table_result
