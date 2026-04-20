"""
vector_index.py — 向量索引后端

提供三种向量索引实现：
  - NumpyVectorIndex  : 纯 NumPy 点积，无额外依赖
  - FaissVectorIndex  : Facebook FAISS 库
  - QdrantVectorIndex : Qdrant 向量数据库（本地模式）

build_vector_index(settings) 根据配置自动选择后端。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.schemas import Chunk, SearchHit


class BaseVectorIndex:
    """向量索引抽象基类，定义 build + search 接口。"""
    def build(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        raise NotImplementedError

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        raise NotImplementedError


class NumpyVectorIndex(BaseVectorIndex):
    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None

    def build(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        self._chunks = chunks
        self._vectors = np.asarray(vectors, dtype="float32")

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        if self._vectors is None or not len(self._chunks):
            return []

        query = np.asarray(query_vector, dtype="float32")
        scores = self._vectors @ query
        ranked_indices = np.argsort(scores)[::-1]

        hits: list[SearchHit] = []
        for rank, index in enumerate(ranked_indices, start=1):
            chunk = self._chunks[int(index)]
            if filters and not _match_filters(chunk, filters):
                continue
            hits.append(
                SearchHit(
                    chunk=chunk,
                    score=float(scores[index]),
                    rank=rank,
                    vector_score=float(scores[index]),
                )
            )
            if len(hits) >= top_k:
                break
        return hits


class FaissVectorIndex(BaseVectorIndex):
    def __init__(self) -> None:
        import faiss

        self.faiss = faiss
        self.index = None
        self._chunks: list[Chunk] = []

    def build(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        matrix = np.asarray(vectors, dtype="float32")
        dimension = matrix.shape[1]
        self.index = self.faiss.IndexFlatIP(dimension)
        self.index.add(matrix)
        self._chunks = chunks

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        if self.index is None:
            return []

        scores, indices = self.index.search(np.asarray([query_vector], dtype="float32"), top_k * 5)
        hits: list[SearchHit] = []
        for rank, (score, index) in enumerate(zip(scores[0], indices[0]), start=1):
            if index < 0:
                continue
            chunk = self._chunks[int(index)]
            if filters and not _match_filters(chunk, filters):
                continue
            hits.append(
                SearchHit(
                    chunk=chunk,
                    score=float(score),
                    rank=rank,
                    vector_score=float(score),
                )
            )
            if len(hits) >= top_k:
                break
        return hits


class QdrantVectorIndex(BaseVectorIndex):
    def __init__(self, storage_path: str, collection_name: str) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client import models as qmodels

        self.client = QdrantClient(path=storage_path)
        self.collection_name = collection_name
        self.qmodels = qmodels
        self._chunks: dict[int, Chunk] = {}

    def build(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not vectors:
            return

        dimension = len(vectors[0])
        self.client.recreate_collection(
            collection_name=self.collection_name,
            vectors_config=self.qmodels.VectorParams(
                size=dimension,
                distance=self.qmodels.Distance.COSINE,
            ),
        )
        points = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            self._chunks[index] = chunk
            payload = {
                "doc_id": chunk.doc_id,
                "chunk_id": chunk.chunk_id,
                "file_type": chunk.metadata.get("file_type", ""),
                "sheet_name": chunk.metadata.get("sheet_name", ""),
                "block_type": chunk.metadata.get("block_type", ""),
                "text": chunk.text,
                "metadata": chunk.metadata,
            }
            points.append(
                self.qmodels.PointStruct(
                    id=index,
                    vector=vector,
                    payload=payload,
                )
            )
        self.client.upsert(collection_name=self.collection_name, points=points)

    def search(
        self,
        query_vector: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        query_filter = None
        if filters:
            query_filter = self.qmodels.Filter(
                must=[
                    self.qmodels.FieldCondition(
                        key=key,
                        match=self.qmodels.MatchValue(value=value),
                    )
                    for key, value in filters.items()
                ]
            )

        results = self._query_points(
            query_vector=query_vector,
            top_k=top_k,
            query_filter=query_filter,
        )

        hits: list[SearchHit] = []
        for rank, result in enumerate(results, start=1):
            chunk = self._chunks.get(int(result.id))
            if chunk is None:
                payload = result.payload or {}
                chunk = Chunk(
                    chunk_id=str(payload.get("chunk_id", result.id)),
                    doc_id=str(payload.get("doc_id", "")),
                    text=str(payload.get("text", "")),
                    metadata=payload.get("metadata", {}) or {},
                )
            hits.append(
                SearchHit(
                    chunk=chunk,
                    score=float(result.score),
                    rank=rank,
                    vector_score=float(result.score),
                )
            )
        return hits

    def _query_points(self, query_vector: list[float], top_k: int, query_filter):
        if hasattr(self.client, "query_points"):
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                limit=top_k,
                query_filter=query_filter,
                with_payload=True,
            )
            return getattr(response, "points", response)

        return self.client.search(
            collection_name=self.collection_name,
            query_vector=query_vector,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        )


def _match_filters(chunk: Chunk, filters: dict[str, Any]) -> bool:
    for key, expected in filters.items():
        actual = chunk.metadata.get(key)
        if actual != expected:
            return False
    return True


def build_vector_index(settings: Any) -> BaseVectorIndex:
    backend = settings.get("retrieval.backend", "qdrant")
    cache_dir = settings.get("cache.dir", "cache")
    collection_name = settings.get("retrieval.collection", "doc_chunks")

    if backend == "faiss":
        try:
            return FaissVectorIndex()
        except Exception:
            return NumpyVectorIndex()
    if backend == "qdrant":
        try:
            return QdrantVectorIndex(storage_path=cache_dir, collection_name=collection_name)
        except Exception:
            return NumpyVectorIndex()
    return NumpyVectorIndex()
