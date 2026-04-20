"""
simple_retriever.py — 向量检索器

整合 Embedding + VectorIndex，提供统一的检索接口：
  - build(chunks)           : 将 Chunk 列表向量化并建立索引
  - search(query, top_k)    : 向量检索 + 关键词重排，返回 SearchHit 列表
  - search_for_field(spec)  : 为单个 FieldSpec 构建查询并检索
"""

from __future__ import annotations

from typing import Any
import math
import re

from app.retriever.embedding import EmbeddingModel
from app.retriever.vector_index import build_vector_index
from app.schemas import Chunk, FieldSpec, SearchHit, normalize_text


def _keyword_score(query: str, text: str) -> float:
    """简单的关键词匹配分：统计 query 中的 token 在 text 中出现的次数。"""
    # 在 ASCII 和中文标点处分词
    query_tokens = [
        token
        for token in re.split(r"[\s,;|，；：:。、！？（）()\[\]【】""'']+", query.lower())
        if len(token) >= 1
    ]
    if not query_tokens:
        return 0.0

    lowered_text = text.lower()
    matched = sum(lowered_text.count(token) for token in query_tokens)
    return matched / max(len(query_tokens), 1)


class Retriever:
    """向量检索器：封装 Embedding + VectorIndex + 关键词重排 + 文件名预过滤。"""
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.embedder = EmbeddingModel.from_settings(settings)
        self.index = build_vector_index(settings)
        self.chunks: list[Chunk] = []
        self._parent_lookup: dict[str, Chunk] = {}
        self._use_parent_context: bool = bool(
            settings.get("retrieval.use_parent_context", False))
        # 方案 C：文件名预过滤
        self._doc_filenames: dict[str, str] = {}        # doc_id → filename (stem)
        self._filename_vectors: dict[str, list[float]] = {}  # doc_id → embedding
        self._filename_filter_threshold: float = float(
            settings.get("retrieval.filename_filter_threshold", 0.15))

    def build(self, chunks: list[Chunk], parent_lookup: dict[str, Chunk] | None = None) -> None:
        self.chunks = chunks
        self._parent_lookup = parent_lookup or {}
        if not chunks:
            return

        # 收集每个 doc_id 的文件名（去后缀）
        self._doc_filenames.clear()
        self._filename_vectors.clear()
        for chunk in chunks:
            doc_id = chunk.doc_id
            if doc_id not in self._doc_filenames:
                fname = chunk.metadata.get("file_name", "")
                if fname:
                    import os
                    self._doc_filenames[doc_id] = os.path.splitext(fname)[0]

        # 批量编码文件名
        if self._doc_filenames:
            doc_ids = list(self._doc_filenames.keys())
            names = [self._doc_filenames[did] for did in doc_ids]
            vecs = self.embedder.encode(names)
            for did, vec in zip(doc_ids, vecs):
                self._filename_vectors[did] = vec

        vectors = self.embedder.encode([chunk.text for chunk in chunks])
        self.index.build(chunks, vectors)

    def _filter_docs_by_filename(self, query: str, min_docs: int = 2) -> set[str] | None:
        """方案 C：根据查询与文件名的向量相似度，预过滤不相关的 doc。

        返回 None 表示不过滤（文件名数据不足），否则返回允许的 doc_id 集合。
        至少保留 min_docs 个文档，避免过度过滤。
        """
        if not self._filename_vectors or len(self._filename_vectors) <= min_docs:
            return None

        import numpy as np
        query_vec = np.asarray(self.embedder.encode([query], is_query=True)[0], dtype="float32")
        scores: list[tuple[str, float]] = []
        for doc_id, fvec in self._filename_vectors.items():
            sim = float(np.dot(query_vec, np.asarray(fvec, dtype="float32")))
            scores.append((doc_id, sim))
        scores.sort(key=lambda x: x[1], reverse=True)

        # 保留相似度 >= 阈值的文档，至少保留 min_docs 个
        threshold = self._filename_filter_threshold
        allowed = {did for did, sim in scores if sim >= threshold}
        # 确保至少保留 top min_docs
        for did, _ in scores[:min_docs]:
            allowed.add(did)

        if len(allowed) >= len(scores):
            return None  # 全部通过，不需要过滤
        return allowed

    def _expand_to_parents(self, hits: list[SearchHit]) -> list[SearchHit]:
        """将 child hit 替换为其 Parent Chunk，按 parent_chunk_id 去重（保留最高分）。"""
        seen: set[str] = set()
        expanded: list[SearchHit] = []
        for hit in hits:
            parent_id = hit.chunk.metadata.get("parent_chunk_id")
            if parent_id and parent_id in self._parent_lookup:
                if parent_id in seen:
                    continue
                seen.add(parent_id)
                parent = self._parent_lookup[parent_id]
                expanded.append(SearchHit(
                    chunk=parent,
                    score=hit.score,
                    rank=hit.rank,
                    keyword_score=hit.keyword_score,
                    vector_score=hit.vector_score,
                ))
            else:
                key = hit.chunk.chunk_id
                if key not in seen:
                    seen.add(key)
                    expanded.append(hit)
        return expanded

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        if not self.chunks:
            return []
        top_k = min(max(top_k, 1), len(self.chunks))
        query_vector = self.embedder.encode([query], is_query=True)[0]
        # 父子模式扩大候选池，展开后去重会缩减数量
        initial_k = top_k * 5 if self._use_parent_context else top_k * 3
        hits = self.index.search(query_vector, top_k=initial_k, filters=filters)

        # --- 方案 C：文件名预过滤 ---
        allowed_docs = self._filter_docs_by_filename(query)
        if allowed_docs is not None:
            hits = [h for h in hits if h.chunk.doc_id in allowed_docs]

        reranked: list[SearchHit] = []
        for hit in hits:
            keyword_score = _keyword_score(query, hit.chunk.text)
            combined = 0.8 * hit.score + 0.2 * keyword_score
            reranked.append(
                SearchHit(
                    chunk=hit.chunk,
                    score=combined,
                    rank=hit.rank,
                    keyword_score=keyword_score,
                    vector_score=hit.score,
                )
            )
        reranked.sort(key=lambda item: item.score, reverse=True)
        if self._use_parent_context and self._parent_lookup:
            reranked = self._expand_to_parents(reranked)
        return reranked[:top_k]

    def search_for_field(
        self,
        field_spec: FieldSpec,
        user_query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        # 清理字段名：去掉 "，单位：xxx" 和 "例：xxx" 等修饰
        raw_name = field_spec.retrieval_query or field_spec.name
        clean_name = re.sub(r"[，,]\s*(?:单位|例)[：:].*$", "", raw_name).strip()
        query_parts = [
            normalize_text(clean_name),
            " ".join(field_spec.aliases),
            normalize_text(field_spec.description),
            normalize_text(user_query),
        ]
        query = "\n".join(part for part in query_parts if part).strip()
        return self.search(query=query, top_k=top_k or self.settings.get("retrieval.top_k", 8), filters=filters)


def retrieve(query: str, chunks: list[Chunk], top_k: int = 5) -> list[SearchHit]:
    class SimpleSettings:
        @staticmethod
        def get(key: str, default: Any = None) -> Any:
            defaults = {
                "embedding.backend": "sentence_transformers",
                "embedding.model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                "retrieval.backend": "numpy",
                "retrieval.collection": "legacy",
                "retrieval.top_k": top_k,
                "cache.dir": "cache",
            }
            return defaults.get(key, default)

    retriever = Retriever(SimpleSettings())
    retriever.build(chunks)
    return retriever.search(query=query, top_k=top_k)
