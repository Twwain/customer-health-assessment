"""知识库接口。

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

import os
import threading
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, sessionmaker

import config
from config import KNOWLEDGE_DATA_DIR, RERANKER
from database import get_db
from models import Customer, KnowledgeItem
from schemas import (
    KnowledgeBatchStatusRequest,
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
from services.rag.parser import ParseError, validate_upload
from services.rag.vector_store import get_vector_store

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

_reindex_jobs: dict[str, dict] = {}
_reindex_jobs_lock = threading.Lock()
_reindex_operation_lock = threading.Lock()
_REINDEX_JOB_TTL = 3600


def _sweep_reindex_jobs(now: float | None = None) -> None:
    """标记超时任务并限制进程内历史状态数量（须在锁内调用）。"""
    now = now or time.time()
    for job in _reindex_jobs.values():
        if job["status"] == "running" and now - job["created"] > _REINDEX_JOB_TTL:
            job.update(status="error", error="重建索引超时", finished=now)

    done = sorted(
        (job_id for job_id, job in _reindex_jobs.items() if job["status"] != "running"),
        key=lambda job_id: _reindex_jobs[job_id]["created"],
    )
    for old_id in done[:-19]:
        _reindex_jobs.pop(old_id, None)


def _run_reindex_job(job_id: str, category: str | None, bind) -> None:
    session_factory = sessionmaker(bind=bind, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        count = _svc(db).reindex(category=category)
        with _reindex_jobs_lock:
            job = _reindex_jobs.get(job_id)
            if job is not None and job["status"] == "running":
                job.update(status="ready", reindexed=count, finished=time.time())
    except Exception as exc:  # noqa: BLE001 - 后台任务通过状态接口报告错误
        with _reindex_jobs_lock:
            job = _reindex_jobs.get(job_id)
            if job is not None and job["status"] == "running":
                job.update(status="error", error=str(exc), finished=time.time())
    finally:
        db.close()
        _reindex_operation_lock.release()


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


@router.get("/items/{item_id}/download")
def download_item(item_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """下载知识条目对应的原始文档全文（用于人工核验知识有效性）。"""
    item = _svc(db).get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    path = item.document.file_path
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="知识文档文件不存在")
    # 只允许下载知识库目录内的文件，防止 DB 路径被污染时任意读盘
    data_root = os.path.realpath(KNOWLEDGE_DATA_DIR)
    real_path = os.path.realpath(path)
    try:
        inside = os.path.commonpath([data_root, real_path]) == data_root
    except ValueError:
        inside = False
    if not inside:
        raise HTTPException(status_code=400, detail="文档路径不在知识库目录内")
    return FileResponse(
        real_path,
        media_type="application/octet-stream",
        filename=os.path.basename(path),
    )


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
_UPLOAD_READ_CHUNK_BYTES = 64 * 1024
_UPLOAD_PIPELINE_SEM = threading.BoundedSemaphore(config.UPLOAD_GLOBAL_CONCURRENCY)


def _read_upload_limited(file: UploadFile) -> bytes:
    """流式读取上传内容，一旦越过上限立即停止，不把超大文件整体读入内存。"""
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > config.UPLOAD_MAX_BYTES:
            limit_mb = config.UPLOAD_MAX_BYTES // (1024 * 1024)
            raise HTTPException(status_code=413, detail=f"文件超过 {limit_mb}MB 上限")
        parts.append(chunk)
    return b"".join(parts)


@router.post("/upload", response_model=KnowledgeUploadResponse)
def upload(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    category: str = Form("内部规范"),
    industry: str = Form(""),
    db: Session = Depends(get_db),
) -> KnowledgeUploadResponse:
    from services.rag.knowledge_base import normalize_category

    ext = os.path.splitext(file.filename or "")[1].lower()
    if not ext:
        raise HTTPException(status_code=400, detail="文件必须包含扩展名")
    if ext not in _UPLOAD_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext}，支持：{' / '.join(sorted(_UPLOAD_ALLOWED_EXT))}",
        )
    try:
        category = normalize_category(category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not _UPLOAD_PIPELINE_SEM.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="已有知识文档正在上传并索引，请稍后重试",
            headers={"Retry-After": "5"},
        )
    try:
        raw = _read_upload_limited(file)
        if not raw:
            raise HTTPException(status_code=400, detail="空文件")
        try:
            validate_upload(file.filename or "", raw, file.content_type)
        except ParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        doc = _svc(db).create_from_upload(
            title=title or (file.filename or "未命名文档"),
            category=category,
            filename=file.filename or "upload.bin",
            raw=raw,
            industry=industry,
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
    finally:
        _UPLOAD_PIPELINE_SEM.release()


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
            industry=payload.industry,
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
    """审核通过：proposed → canonical。"""
    item = _svc(db).approve_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return KnowledgeItemResponse.model_validate(item)


@router.post("/items/{item_id}/revoke", response_model=KnowledgeItemResponse)
def revoke(item_id: int, db: Session = Depends(get_db)) -> KnowledgeItemResponse:
    """撤销审核（下线）：canonical → proposed。"""
    item = _svc(db).revoke_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return KnowledgeItemResponse.model_validate(item)


@router.post("/batch-status")
def batch_status(
    payload: KnowledgeBatchStatusRequest, db: Session = Depends(get_db)
) -> dict:
    """批量上线 / 下线：设置条目与文档状态，并同步向量库 metadata。"""
    try:
        updated, warnings = _svc(db).set_items_status(payload.ids, payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"updated": updated, "warnings": warnings}


@router.post("/reindex", response_model=KnowledgeReindexResponse)
def reindex(
    payload: KnowledgeReindexRequest | None = None,
    db: Session = Depends(get_db),
) -> KnowledgeReindexResponse:
    category = payload.category if payload else None
    if not _reindex_operation_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="索引正在重建，请等待当前任务完成")
    try:
        count = _svc(db).reindex(category=category)
        return KnowledgeReindexResponse(reindexed=count)
    finally:
        _reindex_operation_lock.release()


@router.post("/reindex/jobs")
def create_reindex_job(
    background_tasks: BackgroundTasks,
    payload: KnowledgeReindexRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """异步重建索引；前端通过 job 状态轮询展示生成进度。"""
    category = payload.category if payload else None
    with _reindex_jobs_lock:
        _sweep_reindex_jobs()
        if not _reindex_operation_lock.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="索引正在重建，请等待当前任务完成")
        job_id = uuid.uuid4().hex
        _reindex_jobs[job_id] = {
            "status": "running",
            "category": category,
            "reindexed": 0,
            "error": None,
            "created": time.time(),
        }
    try:
        background_tasks.add_task(_run_reindex_job, job_id, category, db.get_bind())
    except Exception:
        _reindex_operation_lock.release()
        with _reindex_jobs_lock:
            _reindex_jobs.pop(job_id, None)
        raise
    return {"job_id": job_id, "status": "running"}


@router.get("/reindex/jobs/{job_id}")
def get_reindex_job(job_id: str) -> dict:
    with _reindex_jobs_lock:
        _sweep_reindex_jobs()
        job = _reindex_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="重建索引任务不存在")
        return {
            "job_id": job_id,
            "status": job["status"],
            "reindexed": job["reindexed"],
            "error": job["error"],
        }


# ── 结构化知识指标─────────────


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
