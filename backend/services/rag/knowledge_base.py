"""知识库服务（SOW §3.3 / §5 / §6.4）。

编排整条管道：上传文档 → 解析 → 中文切片 → 向量化 → 写入向量库 + 切片表；
并提供条目浏览 / 检索 / 元数据编辑 / 删除 / 重索引，以及"对话沉淀"采纳入库。

设计要点：
- 一次上传（或一次策略采纳）= 一个 ``KnowledgeDocument``，对应一个聚合 ``KnowledgeItem``。
- 向量库只存标量 metadata（document_id / chunk_index / category / title / status / item_id），
  用于检索过滤与溯源；正文不进结构化的 ``KnowledgeMetric``（精确数值走 SQLite 精确查询）。
- Embedding 不可用时索引标记 failed 但不崩溃，满足 SOW §7 可用性。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Sequence

from config import CHUNK_OVERLAP, CHUNK_SIZE, KNOWLEDGE_DATA_DIR
from models import (
    KNOWLEDGE_CATEGORIES,
    KNOWLEDGE_STATUSES,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeItem,
)
from services.rag.chunker import chunk_text
from services.rag.embeddings import EmbeddingUnavailableError, make_embedding_func
from services.rag.parser import ParseError, parse_document
from services.rag.retriever import retrieve_knowledge
from services.rag.vector_store import VectorStore, get_vector_store

logger = logging.getLogger(__name__)


class KnowledgeIndexError(Exception):
    """切片 / 向量化失败。"""


# 分类白名单以 models.KNOWLEDGE_CATEGORIES 为唯一权威来源
_CATEGORY_CHOICES = KNOWLEDGE_CATEGORIES

# 历史/别名校正：旧版前端上传的分类名 → 规范名（保证既有数据迁移平滑）
_CATEGORY_ALIASES = {
    "公司内部规范": "内部规范",
    "内部数据指标": "内部指标",
    "外部数据指标": "外部指标",
}


def normalize_category(category: str) -> str:
    """校正分类名：别名映射到规范名；不在白名单内则抛 ValueError。"""
    name = _CATEGORY_ALIASES.get((category or "").strip(), (category or "").strip())
    if name not in _CATEGORY_CHOICES:
        raise ValueError(f"无效的知识分类：{category!r}，可选值：{' / '.join(_CATEGORY_CHOICES)}")
    return name


def migrate_legacy_categories(db: Any, store: VectorStore | None = None) -> int:
    """把历史数据中的旧分类名（如"公司内部规范"）一次性校正为规范名，幂等。

    别名映射只对「新写入」生效，已入库的旧数据分类名不会自动变化，
    会导致分类 tab 分裂、检索权重失效——本函数在应用启动时执行一次完成迁移。
    同步校正：documents / items 表、切片表 metadata 副本、向量库 metadata。
    返回校正的条目行数。
    """
    fixed = 0
    for old, new in _CATEGORY_ALIASES.items():
        for model in (KnowledgeDocument, KnowledgeItem):
            updated = (
                db.query(model)
                .filter(model.category == old)
                .update({"category": new}, synchronize_session=False)
            )
            fixed += updated or 0

    # 切片表 metadata 副本 + 向量库 metadata（检索的 category 过滤读向量 metadata）
    dirty = [
        c
        for c in db.query(KnowledgeChunk).all()
        if (c.chunk_metadata or {}).get("category") in _CATEGORY_ALIASES
    ]
    for c in dirty:
        meta = dict(c.chunk_metadata or {})
        meta["category"] = _CATEGORY_ALIASES[meta["category"]]
        c.chunk_metadata = meta
    if dirty and store is not None:
        ids = [c.vector_id for c in dirty if c.vector_id]
        metas = [c.chunk_metadata for c in dirty if c.vector_id]
        if ids:
            try:
                store.update_metadatas(ids, metas)
            except Exception as exc:  # pragma: no cover - 向量库不可用时下次 reindex 同步
                logger.warning("分类迁移更新向量 metadata 失败（可 reindex 修复）：%s", exc)
    if fixed or dirty:
        db.commit()
        logger.info("知识分类迁移完成：校正 %d 行记录、%d 个切片", fixed, len(dirty))
    return fixed


def migrate_seed_knowledge_status(db: Any, store: VectorStore | None = None) -> int:
    """把 seed 预置知识从 proposed 提升为 canonical，幂等。

    v3.0 初期 seed 走 ``create_from_upload`` 默认 proposed，而检索默认只查
    canonical，导致预置知识实际不可检索。本迁移在应用启动时执行一次，同步：
    documents / items 表、切片表 metadata 副本、向量库 metadata。
    返回提升的文档行数。
    """
    docs = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.created_by == "seed", KnowledgeDocument.status == "proposed")
        .all()
    )
    for doc in docs:
        doc.status = "canonical"
        for item in db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).all():
            item.status = "canonical"
        chunk_rows = (
            db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == doc.id).all()
        )
        ids = [c.vector_id for c in chunk_rows if c.vector_id]
        metas = []
        for c in chunk_rows:
            c.chunk_metadata = {**(c.chunk_metadata or {}), "status": "canonical"}
            if c.vector_id:
                metas.append(c.chunk_metadata)
        if ids and store is not None:
            try:
                store.update_metadatas(ids, metas)
            except Exception as exc:  # pragma: no cover - 向量库不可用时下次 reindex 同步
                logger.warning("seed 知识状态迁移更新向量 metadata 失败（可 reindex 修复）：%s", exc)
    if docs:
        db.commit()
        logger.info("seed 预置知识状态迁移完成：%d 个文档提升为 canonical", len(docs))
    return len(docs)


def _safe_filename(name: str, ext: str) -> str:
    base = re.sub(r"[^\w一-鿿.-]", "_", name).strip("_") or "doc"
    return f"{base}{ext}"


def _estimate_tokens(text: str) -> int:
    # 中文约 2 字符/token 的粗略估算
    return max(1, len(text) // 2)


class KnowledgeBaseService:
    def __init__(
        self,
        db: Any,
        store: VectorStore | None = None,
        embed_func=None,
    ) -> None:
        self._db = db
        self._store = store or get_vector_store()
        self._embed = embed_func or make_embedding_func()

    # ── 文档文本来源 ────────────────────────────────────────────────────────
    def _document_text(self, doc: KnowledgeDocument) -> str:
        if doc.file_path and os.path.exists(doc.file_path):
            with open(doc.file_path, "rb") as fh:
                raw = fh.read()
            parsed = parse_document(os.path.basename(doc.file_path), raw=raw)
            return parsed.text
        return ""

    # ── 索引（解析→切片→向量化→入库）────────────────────────────────────────
    def index_document(self, doc: KnowledgeDocument) -> KnowledgeDocument:
        item = (
            self._db.query(KnowledgeItem)
            .filter(KnowledgeItem.document_id == doc.id)
            .first()
        )
        try:
            text = self._document_text(doc)
        except ParseError as exc:
            # 不支持的文件类型 / 解析失败：标记 failed，不向上抛 500
            doc.index_status = "failed"
            doc.index_error = f"文档解析失败：{exc}"
            self._db.commit()
            return doc
        if not text.strip():
            doc.index_status = "empty"
            doc.index_error = "文档无可用文本"
            self._db.commit()
            return doc

        chunks = chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        try:
            vectors = self._embed(chunks)
        except EmbeddingUnavailableError as exc:
            doc.index_status = "failed"
            doc.index_error = f"Embedding 不可用：{exc}"
            self._db.commit()
            return doc
        except Exception as exc:  # pragma: no cover - 网络等异常
            doc.index_status = "failed"
            doc.index_error = str(exc)
            self._db.commit()
            return doc

        ids = [f"{doc.id}:{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "document_id": doc.id,
                "chunk_index": i,
                "category": doc.category,
                "title": item.title if item else doc.title,
                "status": doc.status,
                "item_id": item.id if item else 0,
                "source_type": doc.source_type,
            }
            for i in range(len(chunks))
        ]
        try:
            self._store.add(ids, chunks, vectors, metadatas)
        except Exception as exc:  # pragma: no cover - 向量库写入失败
            doc.index_status = "failed"
            doc.index_error = f"向量库写入失败：{exc}"
            self._db.commit()
            return doc

        # 切片表
        for i, chunk in enumerate(chunks):
            self._db.add(
                KnowledgeChunk(
                    document_id=doc.id,
                    content=chunk,
                    vector_id=ids[i],
                    chunk_metadata=metadatas[i],
                    chunk_index=i,
                    token_count=_estimate_tokens(chunk),
                )
            )
        doc.chunk_count = len(chunks)
        doc.index_status = "indexed"
        doc.index_error = ""
        self._db.commit()
        return doc

    # ── 上传 ────────────────────────────────────────────────────────────────
    def create_from_upload(
        self,
        title: str,
        category: str,
        filename: str,
        raw: bytes,
        created_by: str = "system",
        status: str = "proposed",
    ) -> KnowledgeDocument:
        category = normalize_category(category)
        if status not in KNOWLEDGE_STATUSES:
            raise ValueError(
                f"无效的知识状态：{status!r}，可选值：{' / '.join(KNOWLEDGE_STATUSES)}"
            )
        os.makedirs(KNOWLEDGE_DATA_DIR, exist_ok=True)
        ext = os.path.splitext(filename)[1].lower() or ".txt"
        doc = KnowledgeDocument(
            title=title or os.path.splitext(filename)[0],
            category=category,
            source_type="文档",
            file_size=len(raw),
            status=status,
            index_status="pending",
            created_by=created_by,
        )
        self._db.add(doc)
        self._db.flush()  # 拿到 doc.id 用于命名文件
        safe = _safe_filename(f"{doc.id}_{doc.title}", ext)
        path = os.path.join(KNOWLEDGE_DATA_DIR, safe)
        with open(path, "wb") as fh:
            fh.write(raw)
        doc.file_path = path
        self._db.add(
            KnowledgeItem(
                document_id=doc.id,
                title=doc.title,
                category=category,
                storage="vector",
                status=status,
                created_by=created_by,
            )
        )
        # 先写盘后 commit：commit 失败时清掉磁盘文件，避免孤儿文件累积
        try:
            self._db.commit()
        except Exception:
            self._db.rollback()
            try:
                os.remove(path)
            except OSError:
                logger.warning("清理孤儿文件失败 %s", path)
            raise
        self.index_document(doc)
        self._db.commit()
        return doc

    # ── 对话沉淀：采纳策略入库（SOW §3.3.1）─────────────────────────────────
    def create_from_strategy(
        self,
        title: str,
        strategy_text: str,
        adopted_by: str = "user",
        customer_name: str = "",
    ) -> KnowledgeDocument:
        category = normalize_category("对话沉淀")
        os.makedirs(KNOWLEDGE_DATA_DIR, exist_ok=True)
        doc = KnowledgeDocument(
            title=title,
            category=category,
            source_type="对话沉淀",
            file_size=len(strategy_text.encode("utf-8")),
            status="canonical",  # 用户已采纳，直接可用
            index_status="pending",
            created_by=adopted_by,
        )
        self._db.add(doc)
        self._db.flush()
        fname = _safe_filename(f"{doc.id}_adopted", ".md")
        path = os.path.join(KNOWLEDGE_DATA_DIR, fname)
        header = f"# {title}\n\n> 来源客户：{customer_name}\n\n" if customer_name else f"# {title}\n\n"
        with open(path, "wb") as fh:
            fh.write((header + strategy_text).encode("utf-8"))
        doc.file_path = path
        self._db.add(
            KnowledgeItem(
                document_id=doc.id,
                title=title,
                category=category,
                storage="vector",
                status="canonical",
                created_by=adopted_by,
            )
        )
        try:
            self._db.commit()
        except Exception:
            self._db.rollback()
            try:
                os.remove(path)
            except OSError:
                logger.warning("清理孤儿文件失败 %s", path)
            raise
        self.index_document(doc)
        self._db.commit()
        return doc

    # ── 浏览 / 检索 ─────────────────────────────────────────────────────────
    def list_items(
        self,
        category: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeItem]:
        query = self._db.query(KnowledgeItem)
        if category:
            query = query.filter(KnowledgeItem.category == category)
        if status:
            query = query.filter(KnowledgeItem.status == status)
        query = query.order_by(KnowledgeItem.created_at.desc())
        if not q:
            return query.limit(limit).all()
        # tags 是 JSON 数组，SQL LIKE 匹配序列化文本可能误中（转义符/键名），
        # 改在 Python 侧按标题与标签逐个精确过滤
        ql = q.strip().lower()
        items = [
            it
            for it in query.all()
            if ql in (it.title or "").lower()
            or any(ql in str(t).lower() for t in (it.tags or []))
        ]
        return items[:limit]

    def get_item(self, item_id: int) -> KnowledgeItem | None:
        return self._db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()

    def search(
        self,
        query: str,
        *,
        customer: Any | None = None,
        category: str | None = None,
        top_k: int = 5,
        status: str = "canonical",
    ) -> list:
        return retrieve_knowledge(
            query,
            customer=customer,
            category=category,
            top_k=top_k,
            status=status,
            db=self._db,
            embed_func=self._embed,
            store=self._store,
        )

    # ── 元数据编辑（正文不可编辑，SOW §2.2）────────────────────────────────
    def update_item_metadata(
        self,
        item_id: int,
        *,
        title: str | None = None,
        category: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> KnowledgeItem | None:
        item = self.get_item(item_id)
        if item is None:
            return None
        if category is not None:
            category = normalize_category(category)
        if title is not None:
            item.title = title
        if category is not None:
            item.category = category
        if tags is not None:
            item.tags = list(tags)
        # 同步文档分类（检索过滤用到 document 上的 category）
        doc = (
            self._db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.id == item.document_id)
            .first()
        )
        if doc is not None and category is not None:
            doc.category = category
        self._db.commit()

        # 同步向量库 metadata（category / status / title）
        chunk_rows = (
            self._db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.document_id == item.document_id)
            .all()
        )
        if chunk_rows:
            ids = [c.vector_id for c in chunk_rows if c.vector_id]
            metadatas = [
                {
                    **(c.chunk_metadata or {}),
                    "category": item.category,
                    "status": item.status,
                    "title": item.title,
                }
                for c in chunk_rows
                if c.vector_id
            ]
            # 回写切片表中的 metadata 副本，保持与向量库一致
            for c in chunk_rows:
                c.chunk_metadata = {
                    **(c.chunk_metadata or {}),
                    "category": item.category,
                    "status": item.status,
                    "title": item.title,
                }
            self._db.commit()
            if ids:
                try:
                    self._store.update_metadatas(ids, metadatas)
                except Exception as exc:  # pragma: no cover - 向量库不可用时忽略
                    logger.warning("更新向量 metadata 失败：%s", exc)
        return item

    # ── 审核：proposed → canonical（SOW §3.3.1 审核态，Q7 无权限校验）──────────
    def approve_item(self, item_id: int) -> KnowledgeItem | None:
        item = self.get_item(item_id)
        if item is None:
            return None
        item.status = "canonical"
        doc = (
            self._db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.id == item.document_id)
            .first()
        )
        if doc is not None:
            doc.status = "canonical"
        self._db.commit()
        # 同步向量库 metadata 的 status
        chunk_rows = (
            self._db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.document_id == item.document_id)
            .all()
        )
        ids = [c.vector_id for c in chunk_rows if c.vector_id]
        if ids:
            metadatas = [
                {**(c.chunk_metadata or {}), "status": "canonical"} for c in chunk_rows if c.vector_id
            ]
            # 回写切片表 metadata 副本
            for c in chunk_rows:
                c.chunk_metadata = {**(c.chunk_metadata or {}), "status": "canonical"}
            self._db.commit()
            try:
                self._store.update_metadatas(ids, metadatas)
            except Exception as exc:  # pragma: no cover
                logger.warning("更新向量 status 失败：%s", exc)
        return item

    # ── 删除（含向量清理）───────────────────────────────────────────────────
    def delete_item(self, item_id: int) -> bool:
        item = self.get_item(item_id)
        if item is None:
            return False
        doc_id = item.document_id
        vector_ids = [
            c.vector_id
            for c in self._db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.document_id == doc_id)
            .all()
            if c.vector_id
        ]
        # 磁盘上的原始文件一并清理，避免孤儿文件累积
        doc = (
            self._db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.id == doc_id)
            .first()
        )
        file_path = doc.file_path if doc else None
        if vector_ids:
            try:
                self._store.delete(vector_ids)
            except Exception as exc:  # pragma: no cover
                logger.warning("删除向量失败：%s", exc)
        self._db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).delete()
        self._db.commit()
        if file_path:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError as exc:  # pragma: no cover - 文件清理失败不影响删除
                logger.warning("删除知识库文件失败 %s：%s", file_path, exc)
        return True

    # ── 重索引（故障恢复 / embedding 模型切换）─────────────────────────────
    def reindex(self, category: str | None = None) -> int:
        query = self._db.query(KnowledgeDocument)
        if category:
            query = query.filter(KnowledgeDocument.category == category)
        docs = query.all()
        count = 0
        for doc in docs:
            old_ids = [
                c.vector_id
                for c in self._db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.document_id == doc.id)
                .all()
                if c.vector_id
            ]
            if old_ids:
                try:
                    self._store.delete(old_ids)
                except Exception:  # pragma: no cover
                    pass
            self._db.query(KnowledgeChunk).filter(
                KnowledgeChunk.document_id == doc.id
            ).delete()
            self._db.commit()
            self.index_document(doc)
            self._db.commit()
            count += 1
        return count

    # ── 预置知识（SOW §3.3.3 评估方法论）──────────────────────────────────
    def seed_default_knowledge(self, source_path: str | None = None) -> KnowledgeDocument | None:
        existing = (
            self._db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.category == "内部规范", KnowledgeDocument.title.like("%评估方法论%"))
            .first()
        )
        if existing:
            return existing
        path = source_path or os.path.join(KNOWLEDGE_DATA_DIR, "customer_health_methodology.md")
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            raw = fh.read()
        return self.create_from_upload(
            title="客户健康度评估方法论",
            category="内部规范",
            filename="customer_health_methodology.md",
            raw=raw,
            created_by="seed",
            # 预置知识随项目交付即可用（SOW §3.3.3）：直接 canonical，无需人工审核；
            # 检索默认只查 canonical，若为 proposed 将导致预置知识实际不可检索
            status="canonical",
        )
