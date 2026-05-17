import io
import datetime
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from database import get_db
from models import Customer
from schemas import AssessmentResponse, OverviewResponse, CustomerHealthSummary
from services.scoring import get_scoring_strategy
from services.pdf_report import PdfReportGenerator

router = APIRouter(prefix="/assessment", tags=["健康度评估"])

engine = get_scoring_strategy()
pdf_gen = PdfReportGenerator()


@router.get("/{customer_id}", response_model=AssessmentResponse)
def get_assessment(customer_id: int, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return engine.evaluate(customer)


@router.get("/{customer_id}/pdf")
def download_pdf(customer_id: int, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")

    assessment = engine.evaluate(customer)
    pdf_bytes = pdf_gen.generate(assessment)

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

    if not customers:
        return OverviewResponse(
            total_customers=0,
            avg_score=0,
            risk_count=0,
            level_distribution={"优秀": 0, "良好": 0, "一般": 0, "风险": 0},
            recent_customers=[],
            risk_customers=[],
        )

    pairs = [(engine.evaluate(c), c) for c in customers]
    assessments = [a for a, _ in pairs]
    scores = [a.total_score for a in assessments]
    avg_score = round(sum(scores) / len(scores), 1)
    risk_count = sum(1 for a in assessments if a.level == "风险")
    distribution = {"优秀": 0, "良好": 0, "一般": 0, "风险": 0}
    for a in assessments:
        distribution[a.level] += 1

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

    # Recent: top 5 by updated_at desc
    recent = sorted(summaries, key=lambda s: next(
        c.updated_at for _, c in pairs if c.id == s.customer_id
    ), reverse=True)[:5]

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
