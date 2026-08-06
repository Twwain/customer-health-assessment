"""RAG 检索。

流程：向量召回（dense）→ 重排（metadata / BGE）→ Top-K。
支持 ``where`` 过滤（默认只取 canonical，即已审核知识），并按客户行业提升同行业权重。

Embedding 不可用时（缺 Key / 网络）**静默降级为空结果**，调用方据此走规则引擎兜底，
不抛异常、不影响基础功能（对应  可用性）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from config import RAG_RECALL_K, RAG_TOP_K, RAG_WINDOW
from services.rag.embeddings import EmbeddingUnavailableError, make_embedding_func
from services.rag.reranker import MetadataReranker, RerankCandidate, get_reranker
from services.rag.vector_store import VectorStore, get_vector_store


@dataclass
class RetrievedChunk:
    document_id: int
    chunk_index: int
    item_id: int
    item_title: str
    category: str
    content: str
    score: float
    metadata: dict


class KnowledgeRetriever:
    def __init__(
        self,
        store: VectorStore | None = None,
        embed_func=None,
        reranker=None,
    ) -> None:
        self._store = store or get_vector_store()
        self._embed = embed_func or make_embedding_func()
        self._reranker = reranker or get_reranker()

    def retrieve(
        self,
        query: str,
        *,
        customer: Any | None = None,
        category: str | None = None,
        top_k: int = RAG_TOP_K,
        recall_k: int = RAG_RECALL_K,
        window: int | None = None,
        status: str = "canonical",
        db: Any | None = None,
    ) -> list[RetrievedChunk]:
        where: dict = {}
        if status:
            where["status"] = status
        if category:
            where["category"] = category

        try:
            qvec = self._embed([query])[0]
        except EmbeddingUnavailableError:
            return []

        raw = self._store.query(qvec, top_k=recall_k, where=where)
        candidates = [
            RerankCandidate(
                id=r["id"], content=r["content"], metadata=r["metadata"], base_score=r["score"]
            )
            for r in raw
        ]
        boost_industry = getattr(customer, "industry", None) or None
        ranked = self._reranker.rerank(query, candidates, boost_industry=boost_industry)

        window = RAG_WINDOW if window is None else window
        out: list[RetrievedChunk] = []
        seen_items: set[int] = set()
        for r in ranked[:top_k]:
            m = r.metadata
            item_id = int(m.get("item_id", 0) or 0)
            out.append(
                RetrievedChunk(
                    document_id=int(m.get("document_id", 0) or 0),
                    chunk_index=int(m.get("chunk_index", 0) or 0),
                    item_id=item_id,
                    item_title=m.get("title", "") or "",
                    category=m.get("category", "") or "",
                    content=r.content,
                    score=r.score,
                    metadata=m,
                )
            )
            seen_items.add(item_id)

        if db is not None and seen_items:
            self._bump_hits(db, seen_items)
        if window > 0 and db is not None:
            return [self._expand_window(c, window, db) for c in out]
        return out

    @staticmethod
    def _expand_window(chunk: RetrievedChunk, window: int, db: Any) -> RetrievedChunk:
        """把命中切片的前后相邻切片一起拼进正文，缓解跨切片信息截断。"""
        if window <= 0 or db is None or chunk.document_id is None:
            return chunk
        try:
            from models import KnowledgeChunk

            rows = (
                db.query(KnowledgeChunk)
                .filter(
                    KnowledgeChunk.document_id == chunk.document_id,
                    KnowledgeChunk.chunk_index >= chunk.chunk_index - window,
                    KnowledgeChunk.chunk_index <= chunk.chunk_index + window,
                )
                .order_by(KnowledgeChunk.chunk_index)
                .all()
            )
            if len(rows) <= 1:
                return chunk
            expanded = "\n\n".join(r.content for r in rows)
            return RetrievedChunk(
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                item_id=chunk.item_id,
                item_title=chunk.item_title,
                category=chunk.category,
                content=expanded,
                score=chunk.score,
                metadata=chunk.metadata,
            )
        except Exception:  # pragma: no cover - 窗口扩展失败不影响检索
            return chunk

    @staticmethod
    def _bump_hits(db: Any, item_ids: set[int]) -> None:
        """命中计数 +1。只 flush 不 commit：提交时机由调用方的事务边界决定，
        避免在检索中途意外提交调用方未完成的更改。"""
        try:
            from models import KnowledgeItem

            for iid in item_ids:
                if iid:
                    db.query(KnowledgeItem).filter(KnowledgeItem.id == iid).update(
                        {KnowledgeItem.hit_count: KnowledgeItem.hit_count + 1}
                    )
            db.flush()
        except Exception:  # pragma: no cover - 命中计数失败不影响检索
            try:
                db.rollback()
            except Exception:
                pass


def retrieve_knowledge(
    query: str,
    *,
    customer: Any | None = None,
    category: str | None = None,
    top_k: int = RAG_TOP_K,
    window: int | None = None,
    status: str = "canonical",
    db: Any | None = None,
    embed_func=None,
    reranker: MetadataReranker | None = None,
    store: VectorStore | None = None,
) -> list[RetrievedChunk]:
    """便捷函数：一次性检索。"""
    return KnowledgeRetriever(store=store, embed_func=embed_func, reranker=reranker).retrieve(
        query,
        customer=customer,
        category=category,
        top_k=top_k,
        window=window,
        status=status,
        db=db,
    )
