"""知识库服务。

编排整条管道：上传文档 → 解析 → 中文切片 → 向量化 → 写入向量库 + 切片表；
并提供条目浏览 / 检索 / 元数据编辑 / 删除 / 重索引，以及"对话沉淀"采纳入库。

设计要点：
- 一次上传（或一次策略采纳）= 一个 ``KnowledgeDocument``，对应一个聚合 ``KnowledgeItem``。
- 向量库只存标量 metadata（document_id / chunk_index / category / title / status / item_id），
  用于检索过滤与溯源；正文不进结构化的 ``KnowledgeMetric``（精确数值走 SQLite 精确查询）。
- Embedding 不可用时索引标记 failed 但不崩溃，满足  可用性。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from functools import wraps
from typing import Any, Sequence

from config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    KNOWLEDGE_DATA_DIR,
    KNOWLEDGE_MAINTENANCE_INTERVAL_SECONDS,
    KNOWLEDGE_MAINTENANCE_MAX_BACKOFF_SECONDS,
    KNOWLEDGE_RECONCILE_INTERVAL_SECONDS,
    UPLOAD_MAX_CHUNKS,
    UPLOAD_MAX_EMBEDDING_TOKENS,
)
from sqlalchemy.orm import joinedload
from models import (
    KNOWLEDGE_CATEGORIES,
    KNOWLEDGE_STATUSES,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeItem,
    KnowledgeVectorMetadataOutbox,
)
from services.rag.chunker import chunk_text
from services.rag.embeddings import EmbeddingUnavailableError, make_embedding_func
from services.rag.parser import ParseError, parse_document
from services.rag.retriever import retrieve_knowledge
from services.rag.vector_store import VectorStore, get_vector_store

logger = logging.getLogger(__name__)
_VECTOR_MUTATION_LOCK = threading.RLock()
_METADATA_OUTBOX_BATCH_SIZE = 200
_METADATA_OUTBOX_MAX_BATCHES = 50
_INTEGRITY_CACHE_LOCK = threading.Lock()
_INTEGRITY_CACHE: dict[str, Any] = {}
_MAINTENANCE_LOCK = threading.Lock()
_MAINTENANCE_STOP: threading.Event | None = None
_MAINTENANCE_THREAD: threading.Thread | None = None


def _store_cache_key(store: VectorStore) -> str:
    return f"{type(store).__name__}:{getattr(store, 'collection_name', id(store))}"


def _cache_integrity(store: VectorStore, value: dict[str, Any]) -> None:
    with _INTEGRITY_CACHE_LOCK:
        _INTEGRITY_CACHE.clear()
        _INTEGRITY_CACHE.update({
            "store_key": _store_cache_key(store),
            "checked_at": time.time(),
            "value": dict(value),
        })


def _cached_integrity(store: VectorStore) -> dict[str, Any] | None:
    with _INTEGRITY_CACHE_LOCK:
        if _INTEGRITY_CACHE.get("store_key") != _store_cache_key(store):
            return None
        value = _INTEGRITY_CACHE.get("value")
        return dict(value) if isinstance(value, dict) else None


def _serialized_vector_mutation(func):
    """串行化向量写入与对账，避免影子向量被并发孤儿清理误删。"""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _VECTOR_MUTATION_LOCK:
            return func(*args, **kwargs)
    return wrapped


def _batched(values: Sequence[str], size: int = _METADATA_OUTBOX_BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _enqueue_metadata_updates(db: Any, chunks: Sequence[KnowledgeChunk]) -> list[str]:
    """在调用方 SQL 事务内写入最新 metadata；同一向量只保留最后一次状态。"""
    payloads = {
        str(chunk.vector_id): dict(chunk.chunk_metadata or {})
        for chunk in chunks
        if chunk.vector_id
    }
    if not payloads:
        return []
    existing: dict[str, KnowledgeVectorMetadataOutbox] = {}
    vector_ids = list(payloads)
    for batch in _batched(vector_ids):
        rows = (
            db.query(KnowledgeVectorMetadataOutbox)
            .filter(KnowledgeVectorMetadataOutbox.vector_id.in_(batch))
            .all()
        )
        existing.update({row.vector_id: row for row in rows})
    for vector_id, payload in payloads.items():
        row = existing.get(vector_id)
        if row is None:
            db.add(KnowledgeVectorMetadataOutbox(vector_id=vector_id, payload=payload))
        else:
            row.payload = payload
            row.last_error = ""
    return vector_ids


def _delete_metadata_updates(db: Any, vector_ids: Sequence[str]) -> None:
    for batch in _batched(list(dict.fromkeys(vector_ids))):
        db.query(KnowledgeVectorMetadataOutbox).filter(
            KnowledgeVectorMetadataOutbox.vector_id.in_(batch)
        ).delete(synchronize_session=False)


class KnowledgeIndexError(Exception):
    """切片 / 向量化失败。"""

    def __init__(self, message: str, *, succeeded: int = 0, failed: list[dict] | None = None):
        super().__init__(message)
        self.succeeded = succeeded
        self.failed = failed or []


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
    vector_ids = _enqueue_metadata_updates(db, dirty)
    if fixed or dirty:
        db.commit()
        logger.info("知识分类迁移完成：校正 %d 行记录、%d 个切片", fixed, len(dirty))
    if vector_ids and store is not None:
        result = KnowledgeBaseService(db, store=store)._flush_metadata_outbox(vector_ids)
        if result["failures"]:
            logger.warning("分类迁移更新向量 metadata 待重试：%s", result["failures"])
    return fixed


def migrate_seed_knowledge_status(db: Any, store: VectorStore | None = None) -> int:
    """把 seed 预置知识从 proposed 提升为 canonical，幂等。

    初期 seed 走 ``create_from_upload`` 默认 proposed，而检索默认只查
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
        for c in chunk_rows:
            c.chunk_metadata = {**(c.chunk_metadata or {}), "status": "canonical"}
        _enqueue_metadata_updates(db, chunk_rows)
    if docs:
        db.commit()
        logger.info("seed 预置知识状态迁移完成：%d 个文档提升为 canonical", len(docs))
    if docs and store is not None:
        result = KnowledgeBaseService(db, store=store)._flush_metadata_outbox()
        if result["failures"]:
            logger.warning("seed 知识状态迁移更新向量 metadata 待重试：%s", result["failures"])
    return len(docs)


def migrate_add_industry_column(db: Any, store: VectorStore | None = None) -> int:
    """给存量 knowledge_items 表补 industry 列（SQLite ALTER TABLE）。

    SQLite 没有 ``ADD COLUMN IF NOT EXISTS``，先查 PRAGMA 判断列是否存在。
    存量条目行业未知，保持空串（通用）；用户编辑行业后由 update_item_metadata 自动同步向量库。
    返回 1 表示本次执行了迁移，0 表示列已存在。
    """
    from sqlalchemy import text

    cols = {row[1] for row in db.execute(text("PRAGMA table_info(knowledge_items)"))}
    if "industry" in cols:
        return 0
    db.execute(text("ALTER TABLE knowledge_items ADD COLUMN industry VARCHAR(50) DEFAULT ''"))
    db.commit()
    return 1


def _safe_filename(name: str, ext: str) -> str:
    base = re.sub(r"[^\w一-鿿.-]", "_", name).strip("_") or "doc"
    return f"{base}{ext}"


def _estimate_tokens(text: str) -> int:
    """保守估算 Embedding token：中文 1/字、ASCII 4 字符/token、其余按 UTF-8 字节。"""
    if not text:
        return 0
    cjk = 0
    ascii_chars = 0
    other_unicode_bytes = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
        elif ord(ch) < 128:
            ascii_chars += 1
        else:
            # 日文假名、韩文、emoji 等不能套用英文 4 字符/token；按 UTF-8
            # 字节数计数会适度高估，但能避免匿名上传绕过费用护栏。
            other_unicode_bytes += len(ch.encode("utf-8"))
    return max(1, cjk + (ascii_chars + 3) // 4 + other_unicode_bytes)


class KnowledgeBaseService:
    def __init__(
        self,
        db: Any,
        store: VectorStore | None = None,
        embed_func=None,
    ) -> None:
        self._db = db
        # 向量依赖延迟初始化：Chroma 故障时列表、详情和原文下载仍可只读使用；
        # 只有检索、索引和向量元数据写操作才 fail closed。
        self.__store = store
        self.__embed = embed_func

    @property
    def _store(self) -> VectorStore:
        if self.__store is None:
            self.__store = get_vector_store()
        return self.__store

    @_store.setter
    def _store(self, value: VectorStore) -> None:
        self.__store = value

    @property
    def _embed(self):
        if self.__embed is None:
            self.__embed = make_embedding_func()
        return self.__embed

    @_embed.setter
    def _embed(self, value) -> None:
        self.__embed = value

    def ensure_vector_available(self, *, allow_migration: bool = False) -> None:
        """供路由在产生 SQL 写入前执行 fail-closed 检查。"""
        try:
            self._store.count()
        except Exception as exc:
            raise KnowledgeIndexError(f"持久化向量库不可用：{exc}") from exc
        if bool(getattr(self._store, "migration_required", False)) and not allow_migration:
            raise KnowledgeIndexError(
                "Embedding 配置已变更，完成全量重索引前向量读写已关闭"
            )

    def _flush_metadata_outbox(
        self, vector_ids: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """把持久化 metadata 变更幂等发送到向量库，失败记录留库重试。"""
        requested = list(dict.fromkeys(str(value) for value in (vector_ids or []) if value))
        requested_batches = list(_batched(requested)) if requested else None
        delivered = 0
        failures: list[str] = []
        batch_index = 0
        while batch_index < _METADATA_OUTBOX_MAX_BATCHES:
            query = self._db.query(KnowledgeVectorMetadataOutbox).order_by(
                KnowledgeVectorMetadataOutbox.id
            )
            if requested_batches is not None:
                if batch_index >= len(requested_batches):
                    break
                query = query.filter(
                    KnowledgeVectorMetadataOutbox.vector_id.in_(
                        requested_batches[batch_index]
                    )
                )
            rows = query.limit(_METADATA_OUTBOX_BATCH_SIZE).all()
            if not rows:
                if requested_batches is None:
                    break
                batch_index += 1
                continue
            ids = [row.vector_id for row in rows]
            try:
                self._store.update_metadatas(ids, [dict(row.payload or {}) for row in rows])
            except Exception as exc:
                error = str(exc)[:1000]
                for row in rows:
                    row.attempts += 1
                    row.last_error = error
                self._db.commit()
                failures.append(error)
                break
            for row in rows:
                self._db.delete(row)
            self._db.commit()
            delivered += len(rows)
            if requested_batches is not None:
                batch_index += 1
            else:
                batch_index += 1
        pending = self._db.query(KnowledgeVectorMetadataOutbox).count()
        return {
            "metadata_delivered": delivered,
            "metadata_pending": pending,
            "failures": failures,
        }

    # ── 文档文本来源 ────────────────────────────────────────────────────────
    def _document_text(self, doc: KnowledgeDocument) -> str:
        if doc.file_path and os.path.exists(doc.file_path):
            with open(doc.file_path, "rb") as fh:
                raw = fh.read()
            parsed = parse_document(os.path.basename(doc.file_path), raw=raw)
            return parsed.text
        return ""

    def _build_shadow_payload(
        self, doc: KnowledgeDocument
    ) -> tuple[list[str], list[list[float]], list[dict]]:
        """构建影子索引数据；失败时绝不触碰当前可用索引。"""
        item = (
            self._db.query(KnowledgeItem)
            .filter(KnowledgeItem.document_id == doc.id)
            .first()
        )
        try:
            text = self._document_text(doc)
        except ParseError as exc:
            raise KnowledgeIndexError(f"文档解析失败：{exc}") from exc
        if not text.strip():
            raise KnowledgeIndexError("文档无可用文本")
        chunks = chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        if len(chunks) > UPLOAD_MAX_CHUNKS:
            raise KnowledgeIndexError(f"文档切片超过 {UPLOAD_MAX_CHUNKS} 个上限")
        estimated_tokens = sum(_estimate_tokens(chunk) for chunk in chunks)
        if estimated_tokens > UPLOAD_MAX_EMBEDDING_TOKENS:
            raise KnowledgeIndexError(
                f"文档预计 Embedding token 超过 {UPLOAD_MAX_EMBEDDING_TOKENS} 上限"
            )
        try:
            vectors = self._embed(chunks)
        except EmbeddingUnavailableError as exc:
            raise KnowledgeIndexError(f"Embedding 不可用：{exc}") from exc
        except Exception as exc:
            raise KnowledgeIndexError(str(exc)) from exc
        metadatas = [{
            "document_id": doc.id,
            "chunk_index": i,
            "category": doc.category,
            "industry": item.industry if item else "",
            "title": item.title if item else doc.title,
            "status": doc.status,
            "item_id": item.id if item else 0,
            "source_type": doc.source_type,
        } for i in range(len(chunks))]
        return chunks, vectors, metadatas

    # ── 索引（解析→切片→向量化→入库）────────────────────────────────────────
    @_serialized_vector_mutation
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
        if len(chunks) > UPLOAD_MAX_CHUNKS:
            doc.index_status = "failed"
            doc.index_error = f"文档切片超过 {UPLOAD_MAX_CHUNKS} 个上限"
            doc.chunk_count = 0
            self._db.commit()
            return doc
        estimated_tokens = sum(_estimate_tokens(chunk) for chunk in chunks)
        if estimated_tokens > UPLOAD_MAX_EMBEDDING_TOKENS:
            doc.index_status = "failed"
            doc.index_error = (
                f"文档预计 Embedding token 超过 {UPLOAD_MAX_EMBEDDING_TOKENS} 上限"
            )
            doc.chunk_count = 0
            self._db.commit()
            return doc
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
                "industry": item.industry if item else "",
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
        industry: str = "",
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
                industry=(industry or "").strip(),
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

    # ── 对话沉淀：采纳策略入库─────────────────────────────────
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
        query = self._db.query(KnowledgeItem).options(joinedload(KnowledgeItem.document))
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
        return (
            self._db.query(KnowledgeItem)
            .options(joinedload(KnowledgeItem.document))
            .filter(KnowledgeItem.id == item_id)
            .first()
        )

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

    # ── 元数据编辑（正文不可编辑，）────────────────────────────────
    @_serialized_vector_mutation
    def update_item_metadata(
        self,
        item_id: int,
        *,
        title: str | None = None,
        category: str | None = None,
        industry: str | None = None,
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
        if industry is not None:
            item.industry = (industry or "").strip()
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
        # 同步向量库 metadata（category / status / title）
        chunk_rows = (
            self._db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.document_id == item.document_id)
            .all()
        )
        if chunk_rows:
            # 回写切片表中的 metadata 副本，保持与向量库一致
            for c in chunk_rows:
                c.chunk_metadata = {
                    **(c.chunk_metadata or {}),
                    "category": item.category,
                    "industry": item.industry,
                    "status": item.status,
                    "title": item.title,
                }
        vector_ids = _enqueue_metadata_updates(self._db, chunk_rows)
        # 业务数据与 outbox 同一事务落库；Chroma 失败时返回 SQL 最新值，后台可重试。
        self._db.commit()
        if vector_ids:
            result = self._flush_metadata_outbox(vector_ids)
            if result["failures"]:
                logger.warning("更新向量 metadata 已进入待重试队列：%s", result["failures"])
        return item

    # ── 审核：proposed → canonical──────────
    @_serialized_vector_mutation
    def set_items_status(self, item_ids: Sequence[int], status: str) -> tuple[int, list[str]]:
        """批量设置条目状态（上线 / 下线），返回 ``(更新条数, 警告列表)``。

        单事务更新 items / documents / chunks，再批量同步向量库 metadata；
        不存在的 id 直接报错，避免静默部分成功。
        """
        if status not in KNOWLEDGE_STATUSES:
            raise ValueError(f"无效的知识状态：{status!r}，可选值：{' / '.join(KNOWLEDGE_STATUSES)}")
        ids = list(dict.fromkeys(int(i) for i in item_ids))
        items = (
            self._db.query(KnowledgeItem)
            .options(joinedload(KnowledgeItem.document))
            .filter(KnowledgeItem.id.in_(ids))
            .all()
        )
        found_ids = {it.id for it in items}
        missing = [i for i in ids if i not in found_ids]
        if missing:
            raise ValueError(f"知识条目不存在：{missing}")

        doc_ids = {it.document_id for it in items}
        docs: dict[int, KnowledgeDocument] = {}
        if doc_ids:
            docs = {
                d.id: d
                for d in self._db.query(KnowledgeDocument)
                .filter(KnowledgeDocument.id.in_(doc_ids))
                .all()
            }
        for it in items:
            it.status = status
            doc = docs.get(it.document_id)
            if doc is not None:
                doc.status = status

        # 切片表 metadata 副本同步（检索过滤读向量库 metadata，这里保持一致）
        chunk_rows: list[KnowledgeChunk] = []
        if doc_ids:
            chunk_rows = (
                self._db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.document_id.in_(doc_ids))
                .all()
            )
        for c in chunk_rows:
            c.chunk_metadata = {**(c.chunk_metadata or {}), "status": status}
        vector_ids = _enqueue_metadata_updates(self._db, chunk_rows)
        self._db.commit()

        warnings: list[str] = []
        if vector_ids:
            result = self._flush_metadata_outbox(vector_ids)
            if result["failures"]:
                msg = (
                    "批量更新向量 metadata 失败，已进入持久化重试队列："
                    f"{result['failures'][0]}"
                )
                logger.warning(msg)
                warnings.append(msg)
        return len(items), warnings

    def approve_item(self, item_id: int) -> KnowledgeItem | None:
        """审核通过：proposed → canonical。"""
        item = self.get_item(item_id)
        if item is None:
            return None
        _, warnings = self.set_items_status([item_id], "canonical")
        item = self.get_item(item_id)
        if item is not None:
            item.sync_pending = bool(warnings)
            item.sync_warnings = warnings
        return item

    def revoke_item(self, item_id: int) -> KnowledgeItem | None:
        """撤销审核（下线）：canonical → proposed。"""
        item = self.get_item(item_id)
        if item is None:
            return None
        _, warnings = self.set_items_status([item_id], "proposed")
        item = self.get_item(item_id)
        if item is not None:
            item.sync_pending = bool(warnings)
            item.sync_warnings = warnings
        return item

    # ── 删除（含向量清理）───────────────────────────────────────────────────
    def _purge_deleting_document(
        self, doc_id: int, vector_ids: Sequence[str], file_path: str | None
    ) -> None:
        """完成已持久化 tombstone 的物理清理；失败时保留 SQL 记录供重试。"""
        if vector_ids:
            self._store.delete(list(vector_ids))
            _delete_metadata_updates(self._db, vector_ids)
        delete_document = getattr(self._store, "delete_document", None)
        if callable(delete_document):
            delete_document(doc_id)
        self._db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).delete()
        self._db.commit()
        if file_path:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError as exc:  # pragma: no cover - 文件清理失败不影响数据下线
                logger.warning("删除知识库文件失败 %s：%s", file_path, exc)

    @_serialized_vector_mutation
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
        # 先提交 tombstone。向量库异常时 SQL 权威检索会立即阻止该知识再次进入 Prompt，
        # 同时保留记录，便于用户或后台稍后重试物理清理。
        item.status = "proposed"
        if doc is not None:
            doc.status = "proposed"
            doc.index_status = "deleting"
        self._db.commit()
        try:
            self._purge_deleting_document(doc_id, vector_ids, file_path)
        except Exception as exc:
            self._db.rollback()
            logger.error("删除向量失败，已保留 tombstone：%s", exc)
            raise KnowledgeIndexError(
                f"知识已下线，但向量清理失败，系统将在对账时重试：{exc}"
            ) from exc
        return True

    # ── 重索引（故障恢复 / embedding 模型切换）─────────────────────────────
    @_serialized_vector_mutation
    def reindex(self, category: str | None = None) -> int:
        # deleting 是持久化删除意图，任何索引操作都不能把它改回 indexed。
        # 先尽力完成物理清理；仍失败的墓碑从重索引候选中排除，等待下次对账。
        self.retry_pending_deletes()
        query = self._db.query(KnowledgeDocument)
        query = query.filter(KnowledgeDocument.index_status != "deleting")
        if category:
            query = query.filter(KnowledgeDocument.category == category)
        docs = query.all()

        # Embedding provider/model/dimension/fingerprint 变更时，必须整库构建
        # 独立 collection。所有文档成功后才用一个 SQL 事务切换权威
        # chunk 版本，任一文档失败都保留完整旧索引。
        migration_required = bool(getattr(self._store, "migration_required", False))
        if migration_required:
            if category:
                raise KnowledgeIndexError(
                    "Embedding 配置已变更，必须不带 category 执行全量重索引"
                )
            prepare = getattr(self._store, "prepare_rebuild", None)
            add_rebuild = getattr(self._store, "add_rebuild", None)
            activate = getattr(self._store, "activate_rebuild", None)
            if not all(callable(value) for value in (prepare, add_rebuild, activate)):
                raise KnowledgeIndexError("向量库不支持 Embedding 蓝绿迁移")

            prepare()
            version = uuid.uuid4().hex[:12]
            staged: list[tuple[Any, list[str], list[dict], list[str]]] = []
            failures: list[dict] = []
            try:
                for doc in docs:
                    try:
                        chunks, vectors, metadatas = self._build_shadow_payload(doc)
                        ids = [f"{doc.id}:v{version}:{i}" for i in range(len(chunks))]
                        add_rebuild(ids, chunks, vectors, metadatas)
                        staged.append((doc, chunks, metadatas, ids))
                    except Exception as exc:
                        failures.append({
                            "document_id": doc.id,
                            "title": doc.title,
                            "error": str(exc),
                        })
                        break
                if failures:
                    prepare()
                    raise KnowledgeIndexError(
                        f"蓝绿重索引构建失败：成功 0，失败 {len(failures)}",
                        succeeded=0,
                        failed=failures,
                    )

                expected_vectors = sum(len(row[3]) for row in staged)
                if self._store.count() != expected_vectors:
                    raise KnowledgeIndexError(
                        "影子 collection 数量校验失败，已保留旧索引"
                    )

                old_ids = [
                    row.vector_id
                    for row in self._db.query(KnowledgeChunk.vector_id).all()
                    if row.vector_id
                ]
                self._db.query(KnowledgeChunk).delete()
                _delete_metadata_updates(self._db, old_ids)
                for doc, chunks, metadatas, ids in staged:
                    for i, chunk in enumerate(chunks):
                        self._db.add(KnowledgeChunk(
                            document_id=doc.id,
                            content=chunk,
                            vector_id=ids[i],
                            chunk_metadata=metadatas[i],
                            chunk_index=i,
                            token_count=_estimate_tokens(chunk),
                        ))
                    doc.chunk_count = len(chunks)
                    doc.index_status = "indexed"
                    doc.index_error = ""
                self._db.commit()
            except KnowledgeIndexError:
                self._db.rollback()
                raise
            except Exception as exc:
                self._db.rollback()
                try:
                    prepare()
                except Exception:
                    logger.exception("清理失败的整库影子索引失败")
                raise KnowledgeIndexError(f"蓝绿重索引切换失败：{exc}") from exc

            try:
                activate()
            except Exception as exc:
                # SQL 已指向影子 ID，但 collection 激活失败时保持
                # fail-closed；readiness 会拒绝流量，重试全量 reindex 可恢复。
                raise KnowledgeIndexError(
                    f"影子索引已构建但激活失败：{exc}",
                    succeeded=0,
                ) from exc
            self._cleanup_retired_indexes()
            return len(staged)

        count = 0
        failures: list[dict] = []
        for doc in docs:
            old_ids = [
                c.vector_id
                for c in self._db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.document_id == doc.id)
                .all()
                if c.vector_id
            ]
            staged_ids: list[str] = []
            try:
                chunks, vectors, metadatas = self._build_shadow_payload(doc)
                version = uuid.uuid4().hex[:12]
                staged_ids = [f"{doc.id}:v{version}:{i}" for i in range(len(chunks))]
                self._store.add(staged_ids, chunks, vectors, metadatas)

                # 影子向量完整写入后，单个 SQL 事务切换权威版本。
                self._db.query(KnowledgeChunk).filter(
                    KnowledgeChunk.document_id == doc.id
                ).delete()
                _delete_metadata_updates(self._db, old_ids)
                for i, chunk in enumerate(chunks):
                    self._db.add(KnowledgeChunk(
                        document_id=doc.id,
                        content=chunk,
                        vector_id=staged_ids[i],
                        chunk_metadata=metadatas[i],
                        chunk_index=i,
                        token_count=_estimate_tokens(chunk),
                    ))
                doc.chunk_count = len(chunks)
                doc.index_status = "indexed"
                doc.index_error = ""
                self._db.commit()
                count += 1
            except Exception as exc:
                self._db.rollback()
                if staged_ids:
                    try:
                        self._store.delete(staged_ids)
                    except Exception:
                        logger.exception("清理失败的影子索引失败: doc=%s", doc.id)
                failures.append({"document_id": doc.id, "title": doc.title, "error": str(exc)})
                continue

            # SQL 已切换后再清旧向量；即使失败，SQL 权威校验也会过滤这些孤儿。
            if old_ids:
                try:
                    self._store.delete(old_ids)
                except Exception:
                    logger.exception("清理旧索引失败: doc=%s", doc.id)
        if failures:
            raise KnowledgeIndexError(
                f"重索引部分失败：成功 {count}，失败 {len(failures)}",
                succeeded=count,
                failed=failures,
            )
        self._cleanup_retired_indexes()
        return count

    def _cleanup_retired_indexes(self) -> int:
        integrity = self.inspect_vector_integrity()
        cleanup = getattr(self._store, "cleanup_retired_collections", None)
        if (
            not callable(cleanup) or integrity["migration_required"]
            or integrity["missing_ids"] or integrity["orphan_ids"]
            or integrity["metadata_pending"]
        ):
            return 0
        try:
            return cleanup()
        except Exception as exc:
            raise KnowledgeIndexError(f"当前索引已切换，历史索引清理失败，后台将重试：{exc}") from exc
        finally:
            self.inspect_vector_integrity()

    @_serialized_vector_mutation
    def retry_pending_deletes(self) -> dict[str, Any]:
        """重试数据库中持久化的 deleting tombstone。"""
        pending = (
            self._db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.index_status == "deleting")
            .all()
        )
        deleted: list[int] = []
        failures: list[dict[str, Any]] = []
        for doc in pending:
            vector_ids = [
                row.vector_id
                for row in self._db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.document_id == doc.id)
                .all()
                if row.vector_id
            ]
            try:
                self._purge_deleting_document(doc.id, vector_ids, doc.file_path)
                deleted.append(doc.id)
            except Exception as exc:
                self._db.rollback()
                failures.append({"document_id": doc.id, "error": str(exc)})
        return {"pending_delete_retried": len(deleted), "pending_delete_failures": failures}

    def inspect_vector_integrity(self) -> dict[str, Any]:
        """执行完整 ID 对账并缓存结果；不应在每次 readiness 调用。"""
        sql_rows = self._db.query(
            KnowledgeChunk.vector_id, KnowledgeChunk.document_id
        ).all()
        sql_ids = {row.vector_id for row in sql_rows if row.vector_id}
        vector_ids = set(self._store.list_ids())
        missing_ids = sorted(sql_ids - vector_ids)
        orphan_ids = sorted(vector_ids - sql_ids)
        missing_set = set(missing_ids)
        affected_documents = sorted({
            row.document_id for row in sql_rows if row.vector_id in missing_set
        })
        metadata_pending = self._db.query(KnowledgeVectorMetadataOutbox).count()
        result = {
            "sql_vectors": len(sql_ids),
            "store_vectors": len(vector_ids),
            "missing_ids": missing_ids,
            "orphan_ids": orphan_ids,
            "affected_documents": affected_documents,
            "metadata_pending": metadata_pending,
            "retired_cleanup_pending": bool(getattr(self._store, "retired_cleanup_pending", False)),
            "migration_required": bool(getattr(self._store, "migration_required", False)),
            "consistent": (
                not missing_ids
                and not orphan_ids
                and metadata_pending == 0
                and not bool(getattr(self._store, "retired_cleanup_pending", False))
                and not bool(getattr(self._store, "migration_required", False))
            ),
        }
        _cache_integrity(self._store, result)
        return result

    def inspect_vector_health(self) -> dict[str, Any]:
        """readiness 轻量快照：聚合计数 + 最近一次后台全量对账。"""
        sql_vectors = (
            self._db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.vector_id.isnot(None))
            .count()
        )
        store_vectors = self._store.count()
        metadata_pending = self._db.query(KnowledgeVectorMetadataOutbox).count()
        deleting_pending = (
            self._db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.index_status == "deleting")
            .count()
        )
        cached = _cached_integrity(self._store)
        # 进程刚启动或计数已明确变化时做一次深度检查；
        # 稳态健康探针不再全量拉取 Chroma ID。
        if cached is None or sql_vectors != store_vectors:
            cached = self.inspect_vector_integrity()
        result = dict(cached)
        result.update({
            "sql_vectors": sql_vectors,
            "store_vectors": store_vectors,
            "metadata_pending": metadata_pending,
            "deleting_pending": deleting_pending,
            "retired_cleanup_pending": bool(getattr(self._store, "retired_cleanup_pending", False)),
            "migration_required": bool(getattr(self._store, "migration_required", False)),
        })
        result["consistent"] = bool(
            cached.get("consistent")
            and sql_vectors == store_vectors
            and metadata_pending == 0
            and deleting_pending == 0
            and not result["retired_cleanup_pending"]
            and not result["migration_required"]
        )
        return result

    @_serialized_vector_mutation
    def reconcile_vectors(self, *, cleanup_orphans: bool = False) -> dict[str, Any]:
        """双向对账 SQL/向量 ID；可安全清理数据库不存在的孤儿向量。"""
        retry_result = self.retry_pending_deletes()
        metadata_result = self._flush_metadata_outbox()
        integrity = self.inspect_vector_integrity()
        orphan_ids = integrity["orphan_ids"]
        missing_ids = integrity["missing_ids"]
        if cleanup_orphans and orphan_ids:
            self._store.delete(orphan_ids)
            _delete_metadata_updates(self._db, orphan_ids)
            self._db.commit()
        if missing_ids:
            affected_docs = set(integrity["affected_documents"])
            if affected_docs:
                self._db.query(KnowledgeDocument).filter(
                    KnowledgeDocument.id.in_(affected_docs),
                    # 缺失向量不能覆盖持久化删除意图，后台仍需继续物理清理。
                    KnowledgeDocument.index_status != "deleting",
                ).update(
                    {KnowledgeDocument.index_status: "failed",
                     KnowledgeDocument.index_error: "向量对账发现缺失，请重索引"},
                    synchronize_session=False,
                )
                self._db.commit()
        # 返回操作后的权威状态，避免已清理孤儿后仍误报不一致。
        retired_cleaned = self._cleanup_retired_indexes() if cleanup_orphans else 0
        final_integrity = self.inspect_vector_integrity()
        return {
            **retry_result,
            **metadata_result,
            **final_integrity,
            "orphans_cleaned": len(orphan_ids) if cleanup_orphans else 0,
            "retired_collections_cleaned": retired_cleaned,
        }

    # ── 预置知识──────────────────────────────────
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
            # 预置知识随项目交付即可用：直接 canonical，无需人工审核；
            # 检索默认只查 canonical，若为 proposed 将导致预置知识实际不可检索
            status="canonical",
        )


def run_vector_maintenance_once(
    db: Any, store: VectorStore, *, force_reconcile: bool = False
) -> bool:
    """执行一次恢复；返回是否进行了完整对账，便于调度器和测试观察。"""
    pending_metadata = db.query(KnowledgeVectorMetadataOutbox).count()
    pending_deletes = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.index_status == "deleting")
        .count()
    )
    if pending_metadata or pending_deletes or force_reconcile or getattr(store, "retired_cleanup_pending", False):
        result = KnowledgeBaseService(db, store=store).reconcile_vectors(
            cleanup_orphans=True
        )
        failures = list(result.get("failures") or []) + list(
            result.get("pending_delete_failures") or []
        )
        if failures:
            raise KnowledgeIndexError(f"向量后台恢复仍有失败：{failures[0]}")
        return True
    store.count()
    return False


def _vector_maintenance_loop(stop_event: threading.Event) -> None:
    """有界后台恢复：短周期重试 outbox/tombstone，低频执行完整双向对账。"""
    next_reconcile = time.monotonic() + KNOWLEDGE_RECONCILE_INTERVAL_SECONDS
    retry_not_before = 0.0
    consecutive_failures = 0
    while not stop_event.wait(KNOWLEDGE_MAINTENANCE_INTERVAL_SECONDS):
        if time.monotonic() < retry_not_before:
            continue
        db = None
        try:
            from database import SessionLocal

            store = get_vector_store()
            db = SessionLocal()
            due = time.monotonic() >= next_reconcile
            reconciled = run_vector_maintenance_once(
                db, store, force_reconcile=due
            )
            if reconciled:
                next_reconcile = time.monotonic() + KNOWLEDGE_RECONCILE_INTERVAL_SECONDS
            consecutive_failures = 0
            retry_not_before = 0.0
        except Exception:
            consecutive_failures += 1
            backoff = min(
                KNOWLEDGE_MAINTENANCE_MAX_BACKOFF_SECONDS,
                KNOWLEDGE_MAINTENANCE_INTERVAL_SECONDS
                * (2 ** min(consecutive_failures - 1, 10)),
            )
            retry_not_before = time.monotonic() + backoff
            logger.exception(
                "知识向量后台恢复失败（连续 %d 次），%.1fs 后重试",
                consecutive_failures,
                backoff,
            )
            if db is not None:
                db.rollback()
        finally:
            if db is not None:
                db.close()


def start_vector_maintenance() -> None:
    """随应用 lifespan 启动唯一 daemon worker；重复调用幂等。"""
    global _MAINTENANCE_STOP, _MAINTENANCE_THREAD
    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_THREAD is not None and _MAINTENANCE_THREAD.is_alive():
            return
        _MAINTENANCE_STOP = threading.Event()
        _MAINTENANCE_THREAD = threading.Thread(
            target=_vector_maintenance_loop,
            args=(_MAINTENANCE_STOP,),
            name="knowledge-vector-maintenance",
            daemon=True,
        )
        _MAINTENANCE_THREAD.start()


def stop_vector_maintenance() -> None:
    """请求后台 worker 停止；外部依赖卡住时不阻塞应用无限退出。"""
    global _MAINTENANCE_STOP, _MAINTENANCE_THREAD
    with _MAINTENANCE_LOCK:
        stop_event = _MAINTENANCE_STOP
        thread = _MAINTENANCE_THREAD
        _MAINTENANCE_STOP = None
        _MAINTENANCE_THREAD = None
    if stop_event is not None:
        stop_event.set()
    if thread is not None:
        thread.join(timeout=2.0)
        if thread.is_alive():
            logger.error("知识向量后台恢复线程未能在退出 grace 内停止")
