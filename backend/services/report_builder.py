"""Build factual reports or the current validated AI report result."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading

import config
from models import Customer
from schemas import AssessmentResponse, AssessmentTrendResponse
from services import assessment_history
from services.scoring import get_scoring_strategy


@dataclass
class ReportData:
    assessment: AssessmentResponse
    trend: AssessmentTrendResponse | None = None
    strategy_items: list[dict] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)
    analysis: dict = field(default_factory=dict)
    hypotheses: list[dict] = field(default_factory=list)
    degraded: bool = False
    has_ai: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    timings: dict = field(default_factory=dict)


def build_report_data(
    db,
    customer: Customer,
    *,
    include_ai: bool = True,
    cancel_event: threading.Event | None = None,
    deadline: float | None = None,
    job_id: str | None = None,
) -> ReportData:
    """汇总单客户报告的全部数据；``include_ai=False`` 时只产出量化评估 + 趋势。"""
    if include_ai:
        from services.ai.report import build_report
        return build_report(db, customer, cancelled=cancel_event, deadline=deadline, job_id=job_id)
    engine = get_scoring_strategy()
    assessment = engine.evaluate(customer)
    assessment_history.apply_trend_alerts(db, customer, assessment)
    trend = assessment_history.build_trend(
        db, customer, limit=config.CHAT_TREND_POINTS, fallback_assessment=assessment
    )
    return ReportData(assessment=assessment, trend=trend)
