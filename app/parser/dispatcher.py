"""
dispatcher.py — 文档解析分发器

根据文件后缀名自动分发到对应的解析器：
  .txt  → parse_txt()
  .md   → parse_md()
  .docx → parse_docx()
  .xlsx → parse_xlsx()
返回 ParsedDocument 对象。
"""

import os
from app.parser.txt_parser import parse_txt
from app.parser.md_parser import parse_md
from app.parser.docx_parser import parse_docx
from app.parser.xlsx_parser import parse_xlsx

def parse_file(path):
    """根据文件后缀名调用对应解析器，返回 ParsedDocument。"""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".txt":
        return parse_txt(path)

    elif ext == ".md":
        return parse_md(path)

    elif ext == ".docx":
        return parse_docx(path)

    elif ext == ".xlsx":
        return parse_xlsx(path)

    else:
        raise ValueError(f"不支持的文件类型: {path}")
