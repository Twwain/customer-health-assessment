import io
import datetime
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from database import get_db
from models import Customer
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

# 评估历史挂在 /api/customers/{id}/... 下（SOW §6.3），与客户资源保持一致
history_router = APIRouter(prefix="/customers", tags=["评估历史"])

engine = get_scoring_strategy()
pdf_gen = PdfReportGenerator()


def _get_customer(customer_id: int, db: Session) -> Customer:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return customer


@router.get("/{customer_id}", response_model=AssessmentResponse)
def get_assessment(customer_id: int, db: Session = Depends(get_db)):
    return engine.evaluate(_get_customer(customer_id, db))


@router.post("/{customer_id}/snapshot", response_model=AssessmentHistoryItem)
def create_assessment_snapshot(
    customer_id: int,
    assessed_by: str = Query(default="manual", description="评估触发方，记录谁触发了评估"),
    force: bool = Query(default=False, description="因子未变化时是否仍然记录"),
    db: Session = Depends(get_db),
):
    """执行一次评估并写入历史快照（趋势曲线的数据来源）。"""
    customer = _get_customer(customer_id, db)
    assessment = engine.evaluate(customer)
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
    customer = _get_customer(customer_id, db)
    from services.report_builder import build_report_data

    report = build_report_data(db, customer, include_ai=include_ai)
    db.commit()  # 持久化知识检索命中计数（retriever 只 flush 不 commit）
    pdf_bytes = pdf_gen.generate(
        report.assessment,
        strategy_items=report.strategy_items,
        references=report.references,
        trend=report.trend,
        degraded=report.degraded,
        ai_error=report.error,
    )

    # RFC 5987 filename* for modern browsers + plain filename fallback for mobile
    encoded = quote(f"{customer.customer_name}_健康度评估报告.pdf")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=report.pdf; filename*=UTF-8''{encoded}"
        },
    )


@router.get("/all/overview", response_model=OverviewResponse)
def get_overview(db: Session = Depends(get_db)):
    customers = db.query(Customer).all()

    # 等级分布按 scoring_config.yaml 的 levels 动态构建（最低档视为"风险档"），
    # 不写死等级名，配置改名后接口仍然正确。
    levels_cfg = engine.config.levels  # 按 min_score 降序
    level_names = [lv.name for lv in levels_cfg]
    risk_level = levels_cfg[-1].name if levels_cfg else ""

    if not customers:
        return OverviewResponse(
            total_customers=0,
            avg_score=0,
            risk_count=0,
            level_distribution={name: 0 for name in level_names},
            recent_customers=[],
            risk_customers=[],
        )

    pairs = [(engine.evaluate(c), c) for c in customers]
    assessments = [a for a, _ in pairs]
    scores = [a.total_score for a in assessments]
    avg_score = round(sum(scores) / len(scores), 1)
    risk_count = sum(1 for a in assessments if a.level == risk_level)
    distribution = {name: 0 for name in level_names}
    for a in assessments:
        distribution[a.level] = distribution.get(a.level, 0) + 1

    # Build summaries with industry info
    summaries = [
        CustomerHealthSummary(
            customer_id=a.customer_id,
            customer_name=a.customer_name,
            industry=c.industry,
            total_score=a.total_score,
            level=a.level,
            level_color=a.level_color,
        )
        for a, c in pairs
    ]

    # Recent: top 5 by updated_at desc（先建 id→updated_at 索引，避免 O(N²) 查找）
    updated_at_by_id = {c.id: c.updated_at for _, c in pairs}
    recent = sorted(
        summaries,
        key=lambda s: updated_at_by_id.get(s.customer_id) or datetime.datetime.min,
        reverse=True,
    )[:5]

    # Risk: lowest 5 scores
    risk = sorted(summaries, key=lambda s: s.total_score)[:5]

    return OverviewResponse(
        total_customers=len(customers),
        avg_score=avg_score,
        risk_count=risk_count,
        level_distribution=distribution,
        recent_customers=recent,
        risk_customers=risk,
    )
