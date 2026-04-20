"""
schemas.py — 全局数据结构定义

定义了项目中流转的所有核心数据对象：
  - Chunk            : 文档切分后的最小文本块
  - ParsedDocument   : 解析后的整份文档，包含多个 Chunk
  - SearchHit        : 检索返回的一条命中结果
  - FieldSpec        : 字段定义（名称、类型、别名、正则等）
  - ExtractionCandidate : 单个抽取候选值，带分数和来源信息
  - FusionDecision   : 融合后的最终决策
  - QueryConstraints : 用户查询中解析出的约束条件（地点、日期范围等）
  - TemplateTableTarget : 模板中一个表格的元信息
  - TemplateLayout   : 模板的整体布局（占位符字段 + 表格字段）

另外提供一些全局工具函数：
  - stable_id()          : 生成稳定的 MD5 ID
  - normalize_text()     : 统一文本清洗
  - normalize_field_name(): 字段名标准化（去空格/符号，转小写）
  - infer_value_type()   : 根据字段名自动推断字段类型
  - build_field_spec()   : 快速构建一个 FieldSpec
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from typing import Any
import hashlib
import re


def stable_id(*parts: Any) -> str:
    """将多个部分拼接后取 MD5，生成稳定的唯一 ID。"""
    payload = "|".join(str(part) for part in parts if part is not None)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def normalize_text(value: Any) -> str:
    """将任意值转为去前后空白的字符串，None 返回空串。"""
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    return str(value).strip()


def normalize_field_name(name: str) -> str:
    """字段名标准化：去空格/符号并转小写，便于模糊匹配。"""
    return re.sub(r"[\s\-_()（）:/：]+", "", normalize_text(name)).lower()


def dataclass_to_dict(value: Any) -> Any:
    """递归地将 dataclass 转为原生 dict，便于 JSON 序列化。"""
    if is_dataclass(value):
        return {
            key: dataclass_to_dict(item)
            for key, item in asdict(value).items()
        }
    if isinstance(value, dict):
        return {
            key: dataclass_to_dict(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [dataclass_to_dict(item) for item in value]
    return value


@dataclass
class Chunk:
    """Chunk：文档被切分后的最小文本块，是检索和抽取的基本单位。"""
    chunk_id: str
    doc_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclass_to_dict(self)


@dataclass
class ParsedDocument:
    """ParsedDocument：一份解析后的文档，包含全文文本和切分后的 Chunk 列表。"""
    doc_id: str
    name: str
    path: str
    file_type: str
    text: str
    chunks: list["Chunk"] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclass_to_dict(self)


@dataclass
class SearchHit:
    """SearchHit：向量检索返回的一条命中结果，包含分数和排名。"""
    chunk: Chunk
    score: float
    rank: int
    keyword_score: float = 0.0
    vector_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk": self.chunk.to_dict(),
            "score": self.score,
            "rank": self.rank,
            "keyword_score": self.keyword_score,
            "vector_score": self.vector_score,
        }


@dataclass
class FieldSpec:
    """
    FieldSpec：字段定义规格。

    定义了一个待抽取字段的全部元信息：名称、别名、值类型、
    正则模式、关键词提示、示例、检索查询语句等。
    会被规则抽取器和 LLM 抽取器同时使用。
    """
    name: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    value_type: str = "string"
    multi_value: bool = False
    required: bool = False
    regex_patterns: list[str] = field(default_factory=list)
    keyword_hints: list[str] = field(default_factory=list)
    entity_hints: list[str] = field(default_factory=list)
    prompt_hint: str = ""
    retrieval_query: str = ""
    examples: list[str] = field(default_factory=list)
    template_targets: list[str] = field(default_factory=list)
    source_weights: dict[str, float] = field(default_factory=dict)
    schema: dict[str, Any] = field(default_factory=dict)
    normalizer: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    computation: str = ""  # "sum", "average", "ratio", "count", "growth_rate", ""
    source_fields: list[str] = field(default_factory=list)  # 计算依赖的其他字段名

    def all_names(self) -> list[str]:
        """返回字段名 + 所有别名 + 关键词提示，去重后用于模糊匹配。"""
        values = [self.name, *self.aliases, *self.keyword_hints]
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = normalize_text(value)
            if not text:
                continue
            key = normalize_field_name(text)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(text)
        return deduped

    def effective_schema(self) -> dict[str, Any]:
        """生成用于校验抽取结果的 JSON Schema，若用户未自定义则根据 value_type 自动生成。"""
        if self.schema:
            return self.schema

        if self.value_type in {"number", "float"}:
            leaf = {"type": ["number", "string", "null"]}
        elif self.value_type == "integer":
            leaf = {"type": ["integer", "string", "null"]}
        else:
            leaf = {"type": ["string", "number", "integer", "null"]}

        if self.multi_value:
            return {"type": ["array", "null"], "items": leaf}
        return leaf

    def prompt_dict(self) -> dict[str, Any]:
        """将字段规格转为 dict，用于构建 LLM Prompt。"""
        return {
            "name": self.name,
            "aliases": self.aliases,
            "description": self.description,
            "value_type": self.value_type,
            "multi_value": self.multi_value,
            "required": self.required,
            "regex_patterns": self.regex_patterns,
            "keyword_hints": self.keyword_hints,
            "entity_hints": self.entity_hints,
            "prompt_hint": self.prompt_hint,
            "examples": self.examples,
            "schema": self.effective_schema(),
        }


@dataclass
class ExtractionCandidate:
    """
    ExtractionCandidate：一个抽取候选值。

    包含抽取出的值、来源信息、各维度分数，
    以及 final_score() 方法用于融合时的综合排序。
    """
    candidate_id: str
    values: dict[str, Any]
    extractor: str
    confidence: float
    evidence_quote: str
    source_chunk_ids: list[str]
    source_path: str
    retrieval_score: float = 0.0
    keyword_score: float = 0.0
    evidence_quality: float = 0.0
    source_weight: float = 0.0
    extractor_weight: float = 0.0
    recency_score: float = 0.0
    validation_errors: list[str] = field(default_factory=list)
    explanation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def final_score(self) -> float:
        """
        加权综合得分，用于融合决策时排序。
        权重：来源权重 35% + 抽取器权重 25% + 证据质量 15%
              + 检索分 20% + 时效分 5%
        """
        weights = self.metadata.get(
            "fusion_weights",
            {
                "source_weight": 0.35,
                "extractor_weight": 0.25,
                "evidence_quality": 0.15,
                "retrieval_score": 0.20,
                "recency_score": 0.05,
            },
        )
        return round(
            weights["source_weight"] * self.source_weight
            + weights["extractor_weight"] * self.extractor_weight
            + weights["evidence_quality"] * self.evidence_quality
            + weights["retrieval_score"] * self.retrieval_score
            + weights["recency_score"] * self.recency_score,
            4,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = dataclass_to_dict(self)
        payload["final_score"] = self.final_score()
        return payload


@dataclass
class FusionDecision:
    """
    FusionDecision：融合决策结果。

    记录某个字段/记录的最终值、置信度、解释说明和全部候选列表。
    """
    subject: str
    final_values: dict[str, Any]
    confidence: float
    explanation: str
    selected_candidate_id: str | None
    candidates: list[ExtractionCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "final_values": self.final_values,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "selected_candidate_id": self.selected_candidate_id,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass
class QueryConstraints:
    """从用户查询中解析出的约束条件（地点、日期范围、最大记录数）。"""
    locations: list[str] = field(default_factory=list)
    exact_terms: list[str] = field(default_factory=list)
    date_range: tuple[str | None, str | None] = (None, None)
    max_records: int | None = None

    def active(self) -> bool:
        return bool(self.locations or self.exact_terms or any(self.date_range))


@dataclass
class TemplateTableTarget:
    """模板中一个表格区域的元信息：表头、起始行、容量、目标城市等。"""
    template_type: str
    identifier: str
    headers: list[str]
    start_row: int = 2
    sheet_name: str | None = None
    table_index: int | None = None
    capacity: int | None = None
    context_text: str = ""
    target_city: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclass_to_dict(self)


@dataclass
class TemplateLayout:
    """模板的整体布局：占位符字段 + 标签字段 + 表格目标列表。"""
    template_path: str
    template_type: str
    mode: str
    placeholder_fields: list[str] = field(default_factory=list)
    label_fields: list[str] = field(default_factory=list)
    table_targets: list[TemplateTableTarget] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # key = 占位符内文本（如 "增加数量，单位：个"）；
    # value = 该占位符所在段落去掉所有 【xxx】 后的纯文本，作为检索/匹配的上下文。
    placeholder_contexts: dict[str, str] = field(default_factory=dict)

    def all_fields(self) -> list[str]:
        fields = [*self.placeholder_fields, *self.label_fields]
        for table in self.table_targets:
            fields.extend(table.headers)

        deduped: list[str] = []
        seen: set[str] = set()
        for field_name in fields:
            key = normalize_field_name(field_name)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(field_name)
        return deduped

    @property
    def is_tabular(self) -> bool:
        return bool(self.table_targets)

    def to_dict(self) -> dict[str, Any]:
        return dataclass_to_dict(self)


def infer_value_type(field_name: str) -> str:
    """根据字段名中的关键词自动推断值类型（date/number/location 等）。"""
    normalized = normalize_text(field_name)
    if re.search(r"(日期|date)", normalized, re.I):
        return "date"
    if re.search(r"(时间|time)", normalized, re.I):
        return "datetime"
    if re.search(r"(金额|预算|gdp|收入|支出|pm|aqi|指数|人口|检测数|病例数|死亡数|数值|数量"
                 r"|率|死亡|寿命|人次|费用|床位|每千|每万|人均|总数|总量|总费|个数|万人|亿元"
                 r"|万张|机构数|人员数|医师|护士|比重|占比|增长|增加|出生|婴儿|孕产"
                 r"|单位：岁|单位：元|单位：个|单位：万|单位：亿|单位：‰|单位：%)", normalized, re.I):
        return "number"
    if re.search(r"(城市|地区|国家|大洲|省|市|区|县)", normalized):
        return "location"
    return "string"


def build_field_spec(
    field_name: str,
    context: str | None = None,
    **overrides: Any,
) -> FieldSpec:
    """从字段名快速构建 FieldSpec，自动推断 value_type 和 computation。

    当传入 `context`（字段所在段落的上下文文本）时，会把它作为
    `retrieval_query` 的默认值，从而让向量检索基于段落语义而非占位符本身，
    显著减少占位符中示例文字、单位说明等对检索结果的干扰。
    """
    default_query = context.strip() if isinstance(context, str) and context.strip() else field_name
    return FieldSpec(
        name=field_name,
        value_type=overrides.pop("value_type", infer_value_type(field_name)),
        retrieval_query=overrides.pop("retrieval_query", default_query),
        computation=overrides.pop("computation", infer_computation_type(field_name)),
        **overrides,
    )


# ---------- 计算类型自动推断 ----------

_COMPUTATION_KEYWORDS: dict[str, list[str]] = {
    "sum":         ["总计", "合计", "总数", "总量", "累计", "总人数", "总感染", "总死亡",
                    "总确诊", "总治愈", "汇总"],
    "average":     ["平均", "均值", "人均", "均价", "平均值"],
    "ratio":       ["占比", "比例", "比率", "百分比", "感染率", "死亡率", "治愈率",
                    "阳性率", "覆盖率"],
    "growth_rate": ["增长率", "增速", "同比", "环比", "增幅", "涨幅", "降幅"],
    "count":       ["总共多少", "一共多少"],
}


def infer_computation_type(field_name: str) -> str:
    """根据字段名中的关键词自动推断是否需要计算及计算类型。"""
    normalized = normalize_text(field_name)
    for comp_type, keywords in _COMPUTATION_KEYWORDS.items():
        if any(kw in normalized for kw in keywords):
            return comp_type
    return ""
