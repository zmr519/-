"""
embedding.py — 文本向量化模块

提供多种 Embedding 后端：
  - SentenceTransformerBackend : 使用 sentence-transformers 库
  - BGEBackend                : 使用 FlagEmbedding 库（BAAI/BGE M3 等）

两者都已实现 fallback：若依赖未安装，回退为简单的词袋向量。
EmbeddingModel.from_settings() 为统一入口，根据配置自动选择后端。
"""

from __future__ import annotations

from typing import Any
import logging


logger = logging.getLogger(__name__)


class BaseEmbeddingBackend:
    """抽象基类，定义 encode 接口。"""
    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        raise NotImplementedError


class SentenceTransformerBackend(BaseEmbeddingBackend):
    """基于 sentence-transformers 的 Embedding 后端，未安装时自动回退为词袋向量。"""
    def __init__(self, model_name: str, device: str | None = None, **kwargs: Any) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.warning("sentence_transformers 未安装，使用简单的关键词匹配后备方案")
            self._use_fallback = True
            return

        self._use_fallback = False
        init_kwargs = dict(kwargs)
        if device:
            init_kwargs["device"] = device
        # 若模型路径为本地目录，强制离线模式，避免向 HuggingFace 发起网络请求
        import os
        from pathlib import Path
        if Path(model_name).exists():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        self.model = SentenceTransformer(model_name, **init_kwargs)

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        if self._use_fallback:
            # 后备方案：使用简单的词袋模型
            import re
            from collections import Counter

            vectors = []
            for text in texts:
                tokens = re.findall(r'[\w]+', text.lower())
                counter = Counter(tokens)
                # 转换为简单的固定长度向量 (512维)
                vector = [0.0] * 512
                for i, (token, count) in enumerate(counter.items()):
                    idx = hash(token) % 512
                    vector[idx] += count
                # 归一化
                norm = sum(x * x for x in vector) ** 0.5
                if norm > 0:
                    vector = [x / norm for x in vector]
                vectors.append(vector)
            return vectors

        vectors = self.model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vectors.tolist()


class BGEBackend(BaseEmbeddingBackend):
    """基于 FlagEmbedding (BGE) 的 Embedding 后端，支持 M3 和 Dense 两种模式。"""
    def __init__(
        self,
        model_name: str,
        query_instruction: str = "",
        **kwargs: Any,
    ) -> None:
        self.query_instruction = query_instruction.strip()
        self.model_name = model_name

        try:
            from FlagEmbedding import BGEM3FlagModel

            self.model = BGEM3FlagModel(model_name, **kwargs)
            self.mode = "m3"
            self._use_fallback = False
        except Exception:
            try:
                from FlagEmbedding import FlagModel

                self.model = FlagModel(model_name, **kwargs)
                self.mode = "dense"
                self._use_fallback = False
            except ImportError:
                logger.warning("FlagEmbedding 未安装，使用简单的关键词匹配后备方案")
                self._use_fallback = True

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        if self._use_fallback:
            # 后备方案：使用简单的词袋模型
            import re
            from collections import Counter

            vectors = []
            for text in texts:
                processed = text
                if is_query and self.query_instruction:
                    processed = f"{self.query_instruction}{text}"
                tokens = re.findall(r'[\w]+', processed.lower())
                counter = Counter(tokens)
                # 转换为简单的固定长度向量 (512维)
                vector = [0.0] * 512
                for i, (token, count) in enumerate(counter.items()):
                    idx = hash(token) % 512
                    vector[idx] += count
                # 归一化
                norm = sum(x * x for x in vector) ** 0.5
                if norm > 0:
                    vector = [x / norm for x in vector]
                vectors.append(vector)
            return vectors

        processed = texts
        if is_query and self.query_instruction:
            processed = [f"{self.query_instruction}{text}" for text in texts]

        encoded = self.model.encode(processed)
        if isinstance(encoded, dict):
            vectors = encoded.get("dense_vecs") or encoded.get("dense_embeddings")
        else:
            vectors = encoded
        if hasattr(vectors, "tolist"):
            return vectors.tolist()
        return [list(vector) for vector in vectors]


class EmbeddingModel:
    """工厂类：根据配置创建并缓存 Embedding 后端实例。"""
    _instances: dict[tuple[str, str], BaseEmbeddingBackend] = {}

    @classmethod
    def from_settings(cls, settings: Any) -> BaseEmbeddingBackend:
        backend = settings.get("embedding.backend", "sentence_transformers")
        model_name = settings.get(
            "embedding.model",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )
        device = cls._resolve_device(settings.get("embedding.device", "auto"))
        cache_key = (backend, model_name)
        if cache_key in cls._instances:
            return cls._instances[cache_key]

        if backend == "bge":
            instance = BGEBackend(
                model_name=model_name,
                query_instruction=settings.get(
                    "embedding.query_instruction",
                    "Represent this sentence for searching relevant passages: ",
                ),
                use_fp16=bool(settings.get("embedding.use_fp16", False)),
            )
        else:
            instance = SentenceTransformerBackend(
                model_name=model_name,
                device=device,
            )

        cls._instances[cache_key] = instance
        logger.info("Embedding backend=%s model=%s device=%s", backend, model_name, device or "auto")
        return instance

    @staticmethod
    def _resolve_device(configured_device: str | None) -> str | None:
        device = str(configured_device or "").strip().lower()
        if device and device not in {"auto", "default"}:
            return device

        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            return None
        return None
