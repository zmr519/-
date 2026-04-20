"""
exceptions.py — 自定义异常类

统一定义项目中使用的异常类型，方便上层捕获和区分。
"""

class AppException(Exception):
    """项目级基础异常。"""
    pass

class ParseError(AppException):
    """文档解析失败时抛出。"""
    pass

class ModelError(AppException):
    """模型推理/加载失败时抛出。"""
    pass

class FillError(AppException):
    """模板填写失败时抛出。"""
    pass