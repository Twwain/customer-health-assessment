import io
import datetime
import logging
import threading
import time
import uuid
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
import config
from database import get_db
from models import AssessmentHistory, Customer
from schemas import (
    AssessmentHistoryItem,
    AssessmentHistoryResponse,
    AssessmentResponse,
    AssessmentTrendResponse,
    CustomerHealthSummary,
    OverviewResponse,
)
from services import assessment_history
from services.scoring import get_scoring_strategy
from services.pdf_report import PdfReportGenerator

router = APIRouter(prefix="/assessment", tags=["健康度评估"])

# 评估历史挂在 /api/customers/{id}/... 下，与客户资源保持一致
history_router = APIRouter(prefix="/customers", tags=["评估历史"])

logger = logging.getLogger(__name__)

pdf_gen = PdfReportGenerator()

# 异步 PDF 导出任务（内存态：生成结果直接缓存在进程内，重启即失效）
_pdf_jobs: dict[str, dict] = {}
_pdf_jobs_lock = threading.Lock()
_PDF_JOBS_MAX = config.PDF_JOB_MAX
# 并发信号量：防止请求方用大量任务打满线程与内存（429 拒绝而非无限排队）
_PDF_JOB_SEM = threading.BoundedSemaphore(config.PDF_JOB_MAX_CONCURRENT)


def _sweep_pdf_jobs(now: float | None = None) -> None:
    """任务清理（须在 ``_pdf_jobs_lock`` 内调用）。

    - 超过 ``PDF_JOB_TTL`` 的 running 视为疑似挂死：置为 error 并保留条目，
      避免用户轮询到 404；后台线程结束时不再覆盖该状态；
    - 已完成（ready/error）条目无论是否过期都保留到上限清理，避免用户
      在 TTL 边界二次查询/下载时突然 404（内存由 ``PDF_JOB_MAX`` 兜底）；
    - 超过 ``PDF_JOB_MAX`` 存量上限时，只清最早完成的 ready/error，绝不误删 running。
    """
    now = now or time.time()
    expired = [
        (jid, v) for jid, v in _pdf_jobs.items() if now - v["created"] > config.PDF_JOB_TTL
    ]
    for jid, v in expired:
        if v["status"] == "running":
            logger.info("PDF 导出任务超时标记失败: job=%s", jid)
            v["status"] = "error"
            v["error"] = f"生成超时（超过 {config.PDF_JOB_TTL}s）"
    if len(_pdf_jobs) > _PDF_JOBS_MAX:
        done = sorted(
            (k for k, v in _pdf_jobs.items() if v["status"] in ("ready", "error")),
            key=lambda k: _pdf_jobs[k]["created"],
        )
        for old_id in done[: len(_pdf_jobs) - _PDF_JOBS_MAX]:
            logger.info("PDF 导出任务超上限清理: job=%s", old_id)
            _pdf_jobs.pop(old_id, None)


def _run_pdf_job(job_id: str, customer_id: int, include_ai: bool, bind) -> None:
    """后台线程生成 PDF：与请求共用同一数据库引擎，避免依赖请求级 Session。"""
    from sqlalchemy.orm import sessionmaker
    from services.report_builder import build_report_data

    Session = sessionmaker(bind=bind, autoflush=False, autocommit=False)
    db = Session()
    try:
        customer = db.query(Customer).filter(Customer.id == customer_id).first()
        if customer is None:
            raise HTTPException(status_code=404, detail="客户不存在")
        report = build_report_data(db, customer, include_ai=include_ai)
        db.commit()  # 持久化知识检索命中计数（retriever 只 flush 不 commit）
        pdf_bytes = pdf_gen.generate(
            report.assessment,
            strategy_items=report.strategy_items,
            references=report.references,
            trend=report.trend,
            industry=customer.industry or "",
        )
        with _pdf_jobs_lock:
            job = _pdf_jobs.get(job_id)
            if job is not None and job["status"] == "running":
                # 先写 bytes 再写 status，避免读方观察到 ready 但 bytes 缺失的中间态
                job["bytes"] = pdf_bytes
                job["status"] = "ready"
                logger.info("PDF 导出任务完成: job=%s customer_id=%s", job_id, customer_id)
            elif job is not None:
                # 任务已被 TTL 标记为失败（超时），丢弃本次生成结果，避免覆盖错误状态
                logger.info("PDF 导出任务已超时，丢弃生成结果: job=%s", job_id)
            else:
                # 任务已被上限清理（进程内存态），生成结果无处存放
                logger.info("PDF 导出任务已不存在（超上限清理），丢弃生成结果: job=%s", job_id)
    except Exception as exc:  # noqa: BLE001 - 任务失败仅记录状态，不阻断请求
        logger.warning("PDF 导出任务失败: job=%s customer_id=%s err=%s", job_id, customer_id, exc)
        with _pdf_jobs_lock:
            job = _pdf_jobs.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = str(exc)
    finally:
        db.close()
        _PDF_JOB_SEM.release()


def _get_customer(customer_id: int, db: Session) -> Customer:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return customer


@router.get("/{customer_id}", response_model=AssessmentResponse)
def get_assessment(customer_id: int, db: Session = Depends(get_db)):
    return get_scoring_strategy().evaluate(_get_customer(customer_id, db))


@router.post("/{customer_id}/snapshot", response_model=AssessmentHistoryItem)
def create_assessment_snapshot(
    customer_id: int,
    assessed_by: str = Query(default="manual", description="评估触发方，记录谁触发了评估"),
    force: bool = Query(default=False, description="因子未变化时是否仍然记录"),
    db: Session = Depends(get_db),
):
    """执行一次评估并写入历史快照（趋势曲线的数据来源）。"""
    customer = _get_customer(customer_id, db)
    assessment = get_scoring_strategy().evaluate(customer)
    record = assessment_history.record_assessment(
        db,
        customer,
        assessment,
        assessed_by=assessed_by,
        trigger="manual",
        skip_if_unchanged=not force,
    )
    if record is None:
        records = assessment_history.list_history(db, customer_id, limit=1)
        if records:
            return AssessmentHistoryItem.model_validate(records[0])
        record = assessment_history.record_assessment(
            db, customer, assessment, assessed_by=assessed_by, skip_if_unchanged=False
        )
    return AssessmentHistoryItem.model_validate(record)


@history_router.get("/{customer_id}/assessment-history", response_model=AssessmentHistoryResponse)
def get_assessment_history(
    customer_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """客户评估历史列表（含分数、维度分、因子快照、时间戳）。"""
    customer = _get_customer(customer_id, db)
    records = assessment_history.list_history(db, customer_id, limit=limit)
    return AssessmentHistoryResponse(
        customer_id=customer.id,
        customer_name=customer.customer_name,
        total=len(records),
        items=assessment_history.to_history_items(records),
    )


@history_router.get("/{customer_id}/assessment-trend", response_model=AssessmentTrendResponse)
def get_assessment_trend(
    customer_id: int,
    limit: int = Query(default=12, ge=2, le=100, description="最近 N 次评估"),
    db: Session = Depends(get_db),
):
    """趋势图数据：点位序列 + 趋势箭头（↑↓→）+ 等级参考线。"""
    customer = _get_customer(customer_id, db)
    return assessment_history.build_trend(db, customer, limit=limit)


@router.get("/{customer_id}/pdf")
def download_pdf(
    customer_id: int,
    include_ai: bool = Query(default=True, description="是否在报告中整合 AI 策略建议与知识溯源"),
    db: Session = Depends(get_db),
):
    """兼容旧接口：同步生成 PDF（LLM 慢时会阻塞请求）。新前端请用异步任务接口。"""
    customer = _get_customer(customer_id, db)
    from services.report_builder import build_report_data

    report = build_report_data(db, customer, include_ai=include_ai)
    db.commit()  # 持久化知识检索命中计数（retriever 只 flush 不 commit）
    pdf_bytes = pdf_gen.generate(
        report.assessment,
        strategy_items=report.strategy_items,
        references=report.references,
        trend=report.trend,
        industry=customer.industry or "",
    )

    # RFC 5987 filename* for modern browsers + plain filename fallback for mobile
    encoded = quote(f"{customer.customer_name}_客情评估报告.pdf", safe="")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=report.pdf; filename*=UTF-8''{encoded}"
        },
    )


@router.post("/{customer_id}/pdf/jobs")
def create_pdf_job(
    customer_id: int,
    include_ai: bool = Query(default=True, description="是否在报告中整合 AI 策略建议与知识溯源"),
    db: Session = Depends(get_db),
):
    """创建后台 PDF 导出任务：立即返回 job_id，生成完成后经 GET /pdf/jobs/{id} 轮询状态。"""
    customer = _get_customer(customer_id, db)
    if not _PDF_JOB_SEM.acquire(blocking=False):
        # 并发上限：直接 429，避免线程/内存被打满
        raise HTTPException(status_code=429, detail="导出任务繁忙，请稍后重试")
    job_id = uuid.uuid4().hex
    bind = db.get_bind()
    try:
        with _pdf_jobs_lock:
            _sweep_pdf_jobs()
            _pdf_jobs[job_id] = {
                "status": "running",
                "customer_id": customer_id,
                "customer_name": customer.customer_name,
                "include_ai": include_ai,
                "bytes": None,
                "error": None,
                "created": time.time(),
            }
        threading.Thread(
            target=_run_pdf_job,
            args=(job_id, customer_id, include_ai, bind),
            daemon=True,
        ).start()
    except Exception:
        _PDF_JOB_SEM.release()
        with _pdf_jobs_lock:
            _pdf_jobs.pop(job_id, None)
        raise
    logger.info(
        "PDF 导出任务创建: job=%s customer_id=%s include_ai=%s", job_id, customer_id, include_ai
    )
    return {"job_id": job_id, "status": "running"}


@router.get("/{customer_id}/pdf/jobs/{job_id}")
def get_pdf_job(customer_id: int, job_id: str):
    """查询导出任务状态：running / ready / error。"""
    with _pdf_jobs_lock:
        _sweep_pdf_jobs()
        job = _pdf_jobs.get(job_id)
        if job is None or job["customer_id"] != customer_id:
            raise HTTPException(status_code=404, detail="导出任务不存在")
        return {"job_id": job_id, "status": job["status"], "error": job.get("error")}


@router.get("/{customer_id}/pdf/jobs/{job_id}/download")
def download_pdf_job(customer_id: int, job_id: str):
    """下载已生成的报告（status=ready 时可用）。"""
    with _pdf_jobs_lock:
        job = _pdf_jobs.get(job_id)
        if job is None or job["customer_id"] != customer_id:
            raise HTTPException(status_code=404, detail="导出任务不存在")
        if job["status"] == "error":
            raise HTTPException(status_code=409, detail=f"报告生成失败：{job.get('error') or '未知错误'}")
        if job["status"] != "ready" or not job.get("bytes"):
            raise HTTPException(status_code=409, detail="报告尚未生成完成")
        payload = job["bytes"]
    logger.info("PDF 导出任务下载: job=%s customer_id=%s", job_id, customer_id)
    encoded = quote(f"{job['customer_name']}_客情评估报告.pdf", safe="")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=report.pdf; filename*=UTF-8''{encoded}"
        },
    )


@router.get("/all/overview", response_model=OverviewResponse)
def get_overview(db: Session = Depends(get_db)):
    # 等级分布按 scoring_config.yaml 的 levels 动态构建（最低档视为"风险档"），
    # 不写死等级名，配置改名后接口仍然正确。
    scoring = get_scoring_strategy()
    levels_cfg = scoring.config.levels  # 按 min_score 降序
    level_names = [lv.name for lv in levels_cfg]
    risk_level = levels_cfg[-1].name if levels_cfg else ""

    total_customers = db.query(Customer).count()
    if not total_customers:
        return OverviewResponse(
            total_customers=0,
            avg_score=0,
            risk_count=0,
            level_distribution={name: 0 for name in level_names},
            recent_customers=[],
            risk_customers=[],
        )

    # 正常路径只读取每位客户的最新评分快照，避免每次打开总览都全量实时评分。
    # 无快照仅是历史数据兼容场景，回退评分后会与快照结果一起参与汇总。
    latest = (
        db.query(
            AssessmentHistory.customer_id,
            func.max(AssessmentHistory.id).label("max_id"),
        )
        .filter(AssessmentHistory.config_version == scoring.config.version)
        .group_by(AssessmentHistory.customer_id)
        .subquery()
    )
    snapshot_stats = (
        db.query(func.count(AssessmentHistory.id), func.sum(AssessmentHistory.total_score))
        .join(latest, AssessmentHistory.id == latest.c.max_id)
        .one()
    )
    snapshot_count, snapshot_score_sum = snapshot_stats
    distribution = {name: 0 for name in level_names}
    for level, count in (
        db.query(AssessmentHistory.level, func.count(AssessmentHistory.id))
        .join(latest, AssessmentHistory.id == latest.c.max_id)
        .group_by(AssessmentHistory.level)
        .all()
    ):
        distribution[level] = count

    snapshot_base = (
        db.query(AssessmentHistory, Customer)
        .join(latest, AssessmentHistory.id == latest.c.max_id)
        .join(Customer, Customer.id == AssessmentHistory.customer_id)
    )
    recent_rows = snapshot_base.order_by(Customer.updated_at.desc()).limit(5).all()
    risk_rows = snapshot_base.order_by(AssessmentHistory.total_score.asc()).limit(5).all()
    missing_customers = (
        db.query(Customer)
        .outerjoin(latest, Customer.id == latest.c.customer_id)
        .filter(latest.c.customer_id.is_(None))
        .all()
    )

    def snapshot_summary(history: AssessmentHistory, customer: Customer) -> CustomerHealthSummary:
        return CustomerHealthSummary(
            customer_id=history.customer_id,
            customer_name=customer.customer_name,
            industry=customer.industry,
            total_score=history.total_score,
            level=history.level,
            level_color=history.level_color,
        )

    missing_pairs = [
        (
            CustomerHealthSummary(
                customer_id=result.customer_id,
                customer_name=result.customer_name,
                industry=customer.industry,
                total_score=result.total_score,
                level=result.level,
                level_color=result.level_color,
            ),
            customer,
        )
        for customer in missing_customers
        for result in [scoring.evaluate(customer)]
    ]
    for summary, _ in missing_pairs:
        distribution[summary.level] = distribution.get(summary.level, 0) + 1
    missing_score_sum = sum(summary.total_score for summary, _ in missing_pairs)
    evaluated_count = snapshot_count + len(missing_pairs)
    avg_score = round(((snapshot_score_sum or 0) + missing_score_sum) / evaluated_count, 1)
    risk_count = distribution.get(risk_level, 0)

    # Top 5 候选由 SQL 截断；仅将极少数无快照兼容数据合入后排序。
    recent_pairs = [(snapshot_summary(history, customer), customer) for history, customer in recent_rows]
    recent_pairs.extend(missing_pairs)
    recent = sorted(
        recent_pairs,
        key=lambda pair: pair[1].updated_at or datetime.datetime.min,
        reverse=True,
    )[:5]
    recent = [summary for summary, _ in recent]

    risk = [snapshot_summary(history, customer) for history, customer in risk_rows]
    risk.extend(summary for summary, _ in missing_pairs)
    risk = sorted(risk, key=lambda summary: summary.total_score)[:5]

    return OverviewResponse(
        total_customers=total_customers,
        avg_score=avg_score,
        risk_count=risk_count,
        level_distribution=distribution,
        recent_customers=recent,
        risk_customers=risk,
    )
