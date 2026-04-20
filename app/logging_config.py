"""
logging_config.py — 日志初始化

根据 config.yaml 中的 logging.level 设置全局日志级别。
"""

import logging

def setup_logging(settings):
    """调用 logging.basicConfig 设置日志格式与级别。"""
    level = settings.get("logging.level", "INFO")
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s - %(levelname)s - %(message)s"
    )