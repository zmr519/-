"""
io_utils.py — 文件读取工具

提供多编码回退的文本读取，优先尝试 utf-8，再回退到 gb18030/gbk，
最终以忽略错误模式读取。主要用于解析 txt/md 等纯文本文件。
"""

from __future__ import annotations

from pathlib import Path


def read_text_with_fallback(path: str | Path) -> str:
    """尝试多种编码读取文本文件，确保不会因编码问题失败。"""
    target = Path(path)
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
        try:
            return target.read_text(encoding=encoding)
        except Exception:
            continue
    return target.read_text(encoding="utf-8", errors="ignore")
