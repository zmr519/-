"""
cache.py — 简单的基于文件系统的缓存层

使用文本的 MD5 散列作为文件名，将 JSON 结果存储在 .cache 目录下。
主要用于缓存 LLM 调用结果或 Embedding 向量，避免重复计算。
"""

import hashlib
import json
from pathlib import Path

class Cache:
    """MD5-keyed 文件缓存。每个缓存项是一个以 MD5 命名的 JSON 文件。"""

    def __init__(self, cache_dir=".cache"):
        self.dir = Path(cache_dir)
        self.dir.mkdir(exist_ok=True)

    def _key(self, text):
        """将任意文本转为 MD5 散列，作为缓存文件名。"""
        return hashlib.md5(text.encode()).hexdigest()

    def get(self, text):
        """查缓存：命中则返回反序列化的 JSON 对象，未命中返回 None。"""
        key = self._key(text)
        file = self.dir / key
        if file.exists():
            return json.loads(file.read_text())
        return None

    def set(self, text, value):
        """写缓存：将 value 序列化为 JSON 存入文件。"""
        key = self._key(text)
        file = self.dir / key
        file.write_text(json.dumps(value, ensure_ascii=False))