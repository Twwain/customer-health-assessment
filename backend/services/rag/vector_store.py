"""向量存储抽象。

两种后端：
- ``ChromaVectorStore``：生产后端，持久化到磁盘，需 ``chromadb``（Docker 环境安装）。
- ``InMemoryVectorStore``：纯 Python + 余弦相似度，**零依赖**，用于开发自测与单元测试，
  保证整条 RAG 管道在没装 chromadb 的环境下也能跑通、可验证。

两个后端都只接收**外部算好的向量**（由 ``embeddings.make_embedding_func`` 提供），
因此换 embedding 供应商不影响向量库代码。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from typing import Any, Protocol, Sequence

from config import (
    CHROMA_PERSIST_DIR,
    EMBEDDING_DIM,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    KNOWLEDGE_COLLECTION,
)
import config

logger = logging.getLogger(__name__)


def _embedding_fingerprint() -> str:
    """不含凭据的 Embedding 语义空间标识；同维换模型也必须重建。"""
    raw = json.dumps(
        {
            "provider": EMBEDDING_PROVIDER,
            "base_url": EMBEDDING_BASE_URL.rstrip("/"),
            "model": EMBEDDING_MODEL,
            "dimension": EMBEDDING_DIM,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _embedding_collection_name(base: str, fingerprint: str) -> str:
    return f"{base}__emb_{fingerprint}"


def _actual_dimension(collection: Any) -> int | None:
    """从真实向量读取维度，不能信任历史版本写入的配置 metadata。"""
    if collection.count() <= 0:
        return None
    payload = collection.get(limit=1, include=["embeddings"])
    embeddings = payload.get("embeddings")
    if embeddings is None or len(embeddings) == 0:
        return None
    first = embeddings[0]
    return len(first) if first is not None else None


def _collection_metadata(collection: Any) -> dict[str, Any]:
    return dict(getattr(collection, "metadata", None) or {})


def _get_or_create_collection(client: Any, name: str, metadata: dict[str, Any]):
    """读取已有 collection 时不传 metadata，避免部分 Chroma 版本覆盖旧维度标记。"""
    try:
        return client.get_collection(name=name)
    except Exception:
        return client.get_or_create_collection(name=name, metadata=metadata)


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

    def list_ids(self) -> list[str]: ...

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

    def list_ids(self) -> list[str]:  # type: ignore[override]
        return list(self._records)

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
        self._base_collection = collection
        self.retired_cleanup_pending = False
        self.embedding_fingerprint = _embedding_fingerprint()
        active_name = _embedding_collection_name(collection, self.embedding_fingerprint)
        collection_metadata = {
            "hnsw:space": "cosine",
            "dimension": EMBEDDING_DIM,
            "embedding_fingerprint": self.embedding_fingerprint,
            "base_collection": collection,
            "active": False,
        }

        existing_collections: list[Any] = []
        for entry in self._client.list_collections():
            name = entry if isinstance(entry, str) else entry.name
            try:
                existing_collections.append(self._client.get_collection(name=name))
            except Exception:
                continue
        try:
            legacy = self._client.get_collection(name=collection)
        except Exception:
            legacy = None
        if legacy is not None and all(c.name != legacy.name for c in existing_collections):
            existing_collections.append(legacy)

        legacy_has_vectors = bool(legacy is not None and legacy.count() > 0)
        legacy_actual_dimension = _actual_dimension(legacy) if legacy_has_vectors else None

        self._collection = _get_or_create_collection(
            self._client, active_name, collection_metadata
        )
        actual_dimension = _actual_dimension(self._collection)
        if actual_dimension not in (None, EMBEDDING_DIM):
            raise RuntimeError(
                f"Chroma collection {active_name!r} 实际维度为 {actual_dimension}，"
                f"与配置 {EMBEDDING_DIM} 不一致"
            )
        target_meta = _collection_metadata(self._collection)
        target_active = (
            target_meta.get("embedding_fingerprint") == self.embedding_fingerprint
            and bool(target_meta.get("active"))
        )
        # 兼容上一版 ``base__dim_N`` 集合：它没有 active/fingerprint
        # metadata，但只要同一 base 的其他版本存在真实向量，
        # 就不得把新指纹 collection 当作全新库直接激活。
        has_previous_index = legacy_has_vectors or any(
            c.name != active_name
            and (c.name == collection or c.name.startswith(f"{collection}__"))
            and c.count() > 0
            for c in existing_collections
        )
        if not target_active and not has_previous_index and self._collection.count() == 0:
            # 全新库可直接激活；存在任何旧向量时必须先完整重索引。
            self._set_collection_active(self._collection, True)
            target_active = True

        self.migration_required = not target_active
        self.dimension_migration_required = bool(
            legacy_has_vectors and legacy_actual_dimension != EMBEDDING_DIM
        )
        if self.migration_required:
            logger.warning(
                "Embedding 语义空间已变更，目标 collection=%s fingerprint=%s；"
                "旧索引保留，完成全量蓝绿 reindex 前禁止检索",
                active_name,
                self.embedding_fingerprint,
            )
        self.collection_name = self._collection.name

    @staticmethod
    def _set_collection_active(collection: Any, active: bool) -> None:
        metadata = _collection_metadata(collection)
        # Chroma 禁止 modify 请求再次携带 hnsw:space，即使值未改也会拒绝。
        metadata.pop("hnsw:space", None)
        metadata["active"] = active
        collection.modify(metadata=metadata)

    def _ensure_query_ready(self) -> None:
        if self.migration_required:
            raise RuntimeError("Embedding 配置已变更，请先执行全量重索引")

    def add(self, ids, documents, embeddings, metadatas) -> None:  # type: ignore[override]
        self._ensure_query_ready()
        self._add_to_collection(ids, documents, embeddings, metadatas)

    def _add_to_collection(self, ids, documents, embeddings, metadatas) -> None:
        self._collection.add(
            ids=list(ids),
            documents=list(documents),
            embeddings=[list(e) for e in embeddings],
            metadatas=[dict(m) for m in metadatas],
        )

    def query(self, query_embedding, top_k, where=None) -> list[dict]:  # type: ignore[override]
        self._ensure_query_ready()
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

    def _related_collections(self):
        """只枚举同一逻辑知识库，兼容旧版无 base_collection 的命名。"""
        legacy_pattern = re.compile(
            re.escape(self._base_collection) + r"__(?:dim_[0-9]+|emb_[0-9a-f]{16})"
        )
        for entry in self._client.list_collections():
            name = entry if isinstance(entry, str) else entry.name
            candidate = self._client.get_collection(name=name)
            owner = _collection_metadata(candidate).get("base_collection")
            if owner == self._base_collection or (
                owner is None and (name == self._base_collection or legacy_pattern.fullmatch(name))
            ):
                yield candidate

    def delete_document(self, document_id: int) -> None:
        # 迁移期间或历史集合回收失败时，删除意图也必须覆盖所有旧版本 ID。
        for candidate in self._related_collections():
            candidate.delete(where={"document_id": document_id})

    def cleanup_retired_collections(self) -> int:
        """仅在调用方确认当前索引与 SQL 一致后回收历史副本。"""
        if self.migration_required:
            return 0
        self.retired_cleanup_pending = True
        removed = 0
        for candidate in self._related_collections():
            if candidate.name == self._collection.name:
                continue
            if _collection_metadata(candidate).get("active"):
                raise RuntimeError("检测到其他激活索引，拒绝清理，请先完成全量重索引")
            self._client.delete_collection(name=candidate.name)
            removed += 1
        self.retired_cleanup_pending = False
        return removed

    def update_metadatas(self, ids, metadatas) -> None:  # type: ignore[override]
        if ids:
            self._collection.update(ids=list(ids), metadatas=[dict(m) for m in metadatas])

    def count(self) -> int:  # type: ignore[override]
        return self._collection.count()

    def list_ids(self) -> list[str]:  # type: ignore[override]
        return list(self._collection.get(include=[]).get("ids") or [])

    def reset(self) -> None:  # type: ignore[override]
        try:
            self._client.delete_collection(self._collection.name)
        except Exception:  # pragma: no cover - 集合不存在时忽略
            pass
        self._collection = self._client.get_or_create_collection(
            name=self._collection.name,
            metadata={
                "hnsw:space": "cosine",
                "dimension": EMBEDDING_DIM,
                "embedding_fingerprint": self.embedding_fingerprint,
                "base_collection": self._base_collection,
                "active": not self.migration_required,
            },
        )

    def prepare_rebuild(self) -> None:
        """清空未激活的目标 collection，用于整库蓝绿构建。"""
        if not self.migration_required:
            raise RuntimeError("当前 Embedding 索引已激活，无需迁移")
        self.reset()

    def add_rebuild(self, ids, documents, embeddings, metadatas) -> None:
        if not self.migration_required:
            raise RuntimeError("当前不在 Embedding 迁移状态")
        self._add_to_collection(ids, documents, embeddings, metadatas)

    def activate_rebuild(self) -> None:
        """完整构建且 SQL 权威切换后激活目标 collection。"""
        if not self.migration_required:
            return
        actual_dimension = _actual_dimension(self._collection)
        if self._collection.count() > 0 and actual_dimension != EMBEDDING_DIM:
            raise RuntimeError(
                f"影子 collection 实际维度 {actual_dimension} 与配置 {EMBEDDING_DIM} 不一致"
            )
        for entry in self._client.list_collections():
            name = entry if isinstance(entry, str) else entry.name
            if name == self._collection.name:
                continue
            try:
                candidate = self._client.get_collection(name=name)
            except Exception:
                continue
            metadata = _collection_metadata(candidate)
            if metadata.get("base_collection") == self._base_collection and metadata.get("active"):
                self._set_collection_active(candidate, False)
        self._set_collection_active(self._collection, True)
        self.migration_required = False
        self.dimension_migration_required = False


_MEMORY_STORE: InMemoryVectorStore | None = None
_CHROMA_STORE: ChromaVectorStore | None = None


def get_vector_store() -> VectorStore:
    """按配置选择后端；持久化后端异常时 fail closed，禁止伪成功。"""
    global _MEMORY_STORE, _CHROMA_STORE
    name = (config.KNOWLEDGE_VECTOR_STORE or "chroma").lower()
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
        logger.error("Chroma 不可用，已拒绝切换临时内存库：%s", exc)
        raise RuntimeError(f"持久化向量库不可用：{exc}") from exc
