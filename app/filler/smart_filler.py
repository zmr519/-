"""
smart_filler.py — 智能填写器

根据模板类型自动分发到 Excel 或 Word 填写器。
处理 data 参数的多种格式（dict / list / 嵌套包含 table_records）。
"""

from __future__ import annotations

from pathlib import Path

from app.filler.excel_filler import fill_excel
from app.filler.utils import inspect_template
from app.filler.word_filler import fill_word


def smart_fill(template: str, data, output: str | None = None) -> str:
    """
    智能填写入口：根据模板后缀（.xlsx/.docx）自动分发，
    并处理 data 参数的多种格式。
    """
    output_path = output or f"output{Path(template).suffix}"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    layout = inspect_template(template)
    records: list[dict] = []
    single_record: dict = {}
    table_records: dict[str, list[dict]] = {}
    cn_values: dict[str, str] = {}
    table_cell_values: dict[int, dict[tuple[int, int], str]] = {}

    if isinstance(data, dict):
        # 统一从 data 中提取所有可能的字段，不再互斥
        cn_values = data.get("cn_values", {}) or {}
        table_cell_values = data.get("table_cell_values", {}) or {}
        table_records = data.get("table_records", {}) or {}
        records = data.get("records", []) or []
        single_record = data.get("single_record", {}) or (records[0] if records else {})
        if not records and single_record:
            records = [single_record]
    elif isinstance(data, list):
        records = data
        single_record = records[0] if records else {}
    else:
        single_record = data or {}
        records = [single_record] if single_record else []

    if template.endswith(".xlsx"):
        fill_excel(
            template,
            records,
            output_path,
            layout=layout,
            single_record=single_record,
            table_records=table_records,
        )
    elif template.endswith(".docx"):
        fill_word(
            template,
            records,
            output_path,
            layout=layout,
            single_record=single_record,
            table_records=table_records,
            cn_values=cn_values,
            table_cell_values=table_cell_values,
        )
    else:
        raise ValueError(f"不支持的模板类型: {template}")

    return output_path
