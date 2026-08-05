"""知识库接口（SOW §6.4）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | /api/knowledge/items              | 知识条目列表（category / status / 关键词过滤） |
| GET    | /api/knowledge/items/{id}         | 知识条目详情 |
| POST   | /api/knowledge/search             | 语义检索（RAG 召回 + Rerank，Top-K=5） |
| POST   | /api/knowledge/upload             | 上传文档（触发解析→切片→向量化 pipeline） |
| PUT    | /api/knowledge/items/{id}         | 编辑元数据（标题 / 分类 / 标签；正文不可编辑） |
| DELETE | /api/knowledge/items/{id}         | 删除条目（含向量清理，级联删除切片） |
| POST   | /api/knowledge/reindex            | 全量 / 按分类重建索引 |
| GET    | /api/knowledge/status             | 知识库健康状态 |

注：检索（search）默认只取 ``canonical``（已审核）知识用于 grounding；
``status=all`` 可检索含 ``proposed`` 的全部条目（知识库管理视图用）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from config import RERANKER
from database import get_db
from models import Customer, KnowledgeItem
from schemas import (
    KnowledgeItemListResponse,
    KnowledgeItemResponse,
    KnowledgeMetricCreate,
    KnowledgeMetricListResponse,
    KnowledgeMetricResponse,
    KnowledgeReindexRequest,
    KnowledgeReindexResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    KnowledgeStatusResponse,
    KnowledgeUpdateRequest,
    KnowledgeUploadResponse,
)
from services.ai.llm_adapter import get_embedding_adapter
from services.rag import metrics as metrics_svc
from services.rag.knowledge_base import KnowledgeBaseService
from services.rag.retriever import RetrievedChunk
from services.rag.vector_store import get_vector_store

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _svc(db: Session) -> KnowledgeBaseService:
    return KnowledgeBaseService(db)


def _to_result(chunk: RetrievedChunk) -> KnowledgeSearchResult:
    return KnowledgeSearchResult(
        document_id=chunk.document_id,
        chunk_index=chunk.chunk_index,
        item_id=chunk.item_id,
        item_title=chunk.item_title,
        category=chunk.category,
        content=chunk.content,
        score=round(chunk.score, 4),
    )


@router.get("/items", response_model=KnowledgeItemListResponse)
def list_items(
    category: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> KnowledgeItemListResponse:
    svc = _svc(db)
    items = svc.list_items(category=category, status=status, q=q, limit=limit)
    return KnowledgeItemListResponse(
        items=[KnowledgeItemResponse.model_validate(i) for i in items],
        total=len(items),
    )


@router.get("/items/{item_id}", response_model=KnowledgeItemResponse)
def get_item(item_id: int, db: Session = Depends(get_db)) -> KnowledgeItemResponse:
    item = _svc(db).get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return KnowledgeItemResponse.model_validate(item)


@router.post("/search", response_model=KnowledgeSearchResponse)
def search(
    payload: KnowledgeSearchRequest,
    db: Session = Depends(get_db),
) -> KnowledgeSearchResponse:
    customer = None
    if payload.customer_id:
        customer = db.query(Customer).filter(Customer.id == payload.customer_id).first()
    # status=None 表示不过滤（检索含 proposed 的全部条目）；"canonical"/"proposed" 按值过滤
    search_status = None if payload.status == "all" else payload.status
    chunks = _svc(db).search(
        payload.query,
        customer=customer,
        category=payload.category,
        top_k=payload.top_k,
        status=search_status,
    )
    db.commit()  # 持久化检索命中计数（retriever 只 flush 不 commit）
    return KnowledgeSearchResponse(
        query=payload.query,
        results=[_to_result(c) for c in chunks],
    )


# 与 services.rag.parser 支持的解析器保持一致
_UPLOAD_ALLOWED_EXT = {".md", ".markdown", ".txt", ".csv", ".pdf", ".xlsx", ".xlsm", ".docx"}
# 单文件大小上限（与前端提示的 50MB 一致）
_UPLOAD_MAX_BYTES = 50 * 1024 * 1024


@router.post("/upload", response_model=KnowledgeUploadResponse)
def upload(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    category: str = Form("内部规范"),
    db: Session = Depends(get_db),
) -> KnowledgeUploadResponse:
    import os

    from services.rag.knowledge_base import normalize_category

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext and ext not in _UPLOAD_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext}，支持：{' / '.join(sorted(_UPLOAD_ALLOWED_EXT))}",
        )
    try:
        category = normalize_category(category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > _UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 50MB 上限")
    doc = _svc(db).create_from_upload(
        title=title or (file.filename or "未命名文档"),
        category=category,
        filename=file.filename or "upload.bin",
        raw=raw,
        created_by="user",
    )
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).first()
    return KnowledgeUploadResponse(
        document_id=doc.id,
        item_id=item.id if item else 0,
        title=doc.title,
        category=doc.category,
        index_status=doc.index_status,
        chunk_count=doc.chunk_count,
        index_error=doc.index_error,
    )


@router.put("/items/{item_id}", response_model=KnowledgeItemResponse)
def update_metadata(
    item_id: int,
    payload: KnowledgeUpdateRequest,
    db: Session = Depends(get_db),
) -> KnowledgeItemResponse:
    try:
        item = _svc(db).update_item_metadata(
            item_id,
            title=payload.title,
            category=payload.category,
            tags=payload.tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return KnowledgeItemResponse.model_validate(item)


@router.delete("/items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db)) -> dict:
    ok = _svc(db).delete_item(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return {"deleted": True}


@router.post("/items/{item_id}/approve", response_model=KnowledgeItemResponse)
def approve(item_id: int, db: Session = Depends(get_db)) -> KnowledgeItemResponse:
    """审核通过：proposed → canonical（SOW §3.3.1，Q7 无权限校验）。"""
    item = _svc(db).approve_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return KnowledgeItemResponse.model_validate(item)


@router.post("/reindex", response_model=KnowledgeReindexResponse)
def reindex(
    payload: KnowledgeReindexRequest | None = None,
    db: Session = Depends(get_db),
) -> KnowledgeReindexResponse:
    category = payload.category if payload else None
    count = _svc(db).reindex(category=category)
    return KnowledgeReindexResponse(reindexed=count)


# ── 结构化知识指标（SOW §3.3.1：精确数值按行业/规模/地域精确查询）─────────────


@router.get("/metrics", response_model=KnowledgeMetricListResponse)
def list_metrics(
    industry: str | None = Query(None),
    metric_key: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> KnowledgeMetricListResponse:
    rows = metrics_svc.query_metrics(
        db, industry=industry or "", metric_key=metric_key or "", limit=limit
    )
    return KnowledgeMetricListResponse(
        items=[KnowledgeMetricResponse.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.post("/metrics", response_model=KnowledgeMetricResponse)
def create_metric(
    payload: KnowledgeMetricCreate, db: Session = Depends(get_db)
) -> KnowledgeMetricResponse:
    """写入指标（按 metric_key + 行业/地域/规模/周期幂等更新）。"""
    try:
        row = metrics_svc.upsert_metric(db, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KnowledgeMetricResponse.model_validate(row)


@router.delete("/metrics/{metric_id}")
def delete_metric(metric_id: int, db: Session = Depends(get_db)) -> dict:
    if not metrics_svc.delete_metric(db, metric_id):
        raise HTTPException(status_code=404, detail="指标不存在")
    return {"deleted": True}


@router.get("/status", response_model=KnowledgeStatusResponse)
def status(db: Session = Depends(get_db)) -> KnowledgeStatusResponse:
    store = get_vector_store()
    emb = get_embedding_adapter()
    categories = [
        c for (c,) in db.query(KnowledgeItem.category)
        .distinct()
        .all()
    ]
    return KnowledgeStatusResponse(
        vector_store=type(store).__name__,
        count=store.count(),
        embedding_available=emb.available,
        reranker=RERANKER,
        categories=categories,
    )
