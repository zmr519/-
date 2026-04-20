"""
settings.py — 配置管理器

读取 config/config.yaml 并提供点分隔的键访问，例如：
  settings.get("llm.url")  →  config["llm"]["url"]
全局只需一个 Settings 实例，由 main.py 创建后传递给各组件。
"""

from __future__ import annotations

from pathlib import Path

import yaml


class Settings:
    """YAML 配置加载器，支持点分隔路径访问。"""

    def __init__(self, config_path: str | Path = "config/config.yaml"):
        target = Path(config_path)
        if not target.exists():
            raise FileNotFoundError(f"配置文件不存在: {target}")
        # 将整个 YAML 加载为嵌套 dict
        self.config = yaml.safe_load(target.read_text(encoding="utf-8")) or {}

    def get(self, key: str, default=None):
        """按点分隔路径取值，如 'llm.url' → config['llm']['url']。"""
        value = self.config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return default if value is None else value
