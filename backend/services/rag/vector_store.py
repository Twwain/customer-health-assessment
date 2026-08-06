"""向量存储抽象。

两种后端：
- ``ChromaVectorStore``：生产后端，持久化到磁盘，需 ``chromadb``（Docker 环境安装）。
- ``InMemoryVectorStore``：纯 Python + 余弦相似度，**零依赖**，用于开发自测与单元测试，
  保证整条 RAG 管道在没装 chromadb 的环境下也能跑通、可验证。

两个后端都只接收**外部算好的向量**（由 ``embeddings.make_embedding_func`` 提供），
因此换 embedding 供应商不影响向量库代码。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Protocol, Sequence

from config import (
    CHROMA_PERSIST_DIR,
    EMBEDDING_DIM,
    KNOWLEDGE_COLLECTION,
    KNOWLEDGE_VECTOR_STORE,
)

logger = logging.getLogger(__name__)


class VectorStore(Protocol):
    """向量库接口；返回结果统一为 dict 列表：``{id, content, metadata, score}``。"""

    def add(
        self,
        ids: Sequence[str],
        documents: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict],
    ) -> None: ...

    def query(
        self,
        query_embedding: Sequence[float],
        top_k: int,
        where: dict | None = None,
    ) -> list[dict]: ...

    def delete(self, ids: Sequence[str]) -> None: ...

    def update_metadatas(self, ids: Sequence[str], metadatas: Sequence[dict]) -> None: ...

    def count(self) -> int: ...

    def reset(self) -> None: ...


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _match_where(metadata: dict, where: dict | None) -> bool:
    """Chroma 风格的简单相等过滤；值为 list 时按 ``$in`` 处理；AND 语义。"""
    if not where:
        return True
    for key, value in where.items():
        actual = metadata.get(key)
        if isinstance(value, list):
            if actual not in value:
                return False
        elif actual != value:
            return False
    return True


class InMemoryVectorStore:
    """零依赖内存向量库（余弦相似度）。"""

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}

    def add(self, ids, documents, embeddings, metadatas) -> None:  # type: ignore[override]
        for i, doc_id in enumerate(ids):
            self._records[doc_id] = {
                "id": doc_id,
                "content": documents[i],
                "embedding": list(embeddings[i]),
                "metadata": dict(metadatas[i]),
            }

    def query(self, query_embedding, top_k, where=None) -> list[dict]:  # type: ignore[override]
        scored = []
        for rec in self._records.values():
            if not _match_where(rec["metadata"], where):
                continue
            scored.append({**rec, "score": _cosine(query_embedding, rec["embedding"])})
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]

    def delete(self, ids) -> None:  # type: ignore[override]
        for doc_id in ids:
            self._records.pop(doc_id, None)

    def update_metadatas(self, ids, metadatas) -> None:  # type: ignore[override]
        for doc_id, meta in zip(ids, metadatas):
            if doc_id in self._records:
                self._records[doc_id]["metadata"] = dict(meta)

    def count(self) -> int:  # type: ignore[override]
        return len(self._records)

    def reset(self) -> None:  # type: ignore[override]
        self._records.clear()


def _to_chroma_where(where: dict | None) -> dict | None:
    """把简单 AND 语义的多条件 dict 转成 Chroma 官方语法。

    Chroma 要求：单条件直接 ``{"k": v}``；多条件必须 ``{"$and": [{"k1": v1}, {"k2": v2}]}``；
    值为 list 时用 ``$in``。直接传多键 dict 新版 chromadb 会抛 ValueError。
    """
    if not where:
        return None
    clauses = []
    for key, value in where.items():
        if isinstance(value, list):
            clauses.append({key: {"$in": value}})
        else:
            clauses.append({key: value})
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


class ChromaVectorStore:
    """Chroma 持久化向量库（lazy import chromadb）。"""

    def __init__(self, persist_dir: str = CHROMA_PERSIST_DIR, collection: str = KNOWLEDGE_COLLECTION) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - 依赖可选
            raise RuntimeError("使用 Chroma 后端需安装 chromadb：pip install chromadb") from exc

        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine", "dimension": EMBEDDING_DIM},
        )

    def add(self, ids, documents, embeddings, metadatas) -> None:  # type: ignore[override]
        self._collection.add(
            ids=list(ids),
            documents=list(documents),
            embeddings=[list(e) for e in embeddings],
            metadatas=[dict(m) for m in metadatas],
        )

    def query(self, query_embedding, top_k, where=None) -> list[dict]:  # type: ignore[override]
        res = self._collection.query(
            query_embeddings=[list(query_embedding)],
            n_results=top_k,
            where=_to_chroma_where(where),
            include=["documents", "metadatas", "distances"],
        )
        out: list[dict] = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for i, doc_id in enumerate(ids):
            # Chroma cosine 距离为 1 - cos，转回相似度
            out.append(
                {
                    "id": doc_id,
                    "content": docs[i] if docs else "",
                    "metadata": metas[i] if metas else {},
                    "score": 1 - (dists[i] if dists else 1.0),
                }
            )
        return out

    def delete(self, ids) -> None:  # type: ignore[override]
        if ids:
            self._collection.delete(ids=list(ids))

    def update_metadatas(self, ids, metadatas) -> None:  # type: ignore[override]
        if ids:
            self._collection.update(ids=list(ids), metadatas=[dict(m) for m in metadatas])

    def count(self) -> int:  # type: ignore[override]
        return self._collection.count()

    def reset(self) -> None:  # type: ignore[override]
        try:
            self._client.delete_collection(self._collection.name)
        except Exception:  # pragma: no cover - 集合不存在时忽略
            pass
        self._collection = self._client.get_or_create_collection(
            name=self._collection.name,
            metadata={"hnsw:space": "cosine", "dimension": EMBEDDING_DIM},
        )


_MEMORY_STORE: InMemoryVectorStore | None = None
_CHROMA_STORE: ChromaVectorStore | None = None


def get_vector_store() -> VectorStore:
    """按配置选择后端；Chroma 不可用时自动回退内存并打日志。"""
    global _MEMORY_STORE, _CHROMA_STORE
    name = (KNOWLEDGE_VECTOR_STORE or "chroma").lower()
    if name == "memory":
        if _MEMORY_STORE is None:
            _MEMORY_STORE = InMemoryVectorStore()
        return _MEMORY_STORE
    # chroma
    try:
        if _CHROMA_STORE is None:
            _CHROMA_STORE = ChromaVectorStore()
        return _CHROMA_STORE
    except Exception as exc:  # pragma: no cover - 环境无 chromadb
        logger.warning("Chroma 不可用，回退内存向量库：%s", exc)
        if _MEMORY_STORE is None:
            _MEMORY_STORE = InMemoryVectorStore()
        return _MEMORY_STORE
