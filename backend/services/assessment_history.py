"""评估历史记录服务。

每次评估落一条 `AssessmentHistory` 快照，用于：
- 预警趋势箭头（最近两次总分差值）
- 客情评分历史趋势曲线
- AI 解读预警时的上下文（"近 3 次评估持续下滑"）
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy.orm import Session

from database import utcnow
from models import AssessmentHistory, Customer
from schemas import (
    AssessmentHistoryItem,
    AssessmentResponse,
    AssessmentTrendResponse,
    LevelConfigItem,
    TrendPoint,
)
from services.scoring import get_scoring_strategy, load_scoring_config

# 趋势判定阈值：分差小于该值视为持平
TREND_EPSILON = 0.05


def build_factor_snapshot(customer: Customer) -> dict[str, Any]:
    """记录评估时所有已注册因子的取值，便于回溯"当时为什么是这个分"。"""
    from services.scoring.rules import resolve_value

    snapshot: dict[str, Any] = {}
    for dimension in load_scoring_config().dimensions:
        for factor in dimension.factors:
            if factor.field in snapshot:
                continue
            value = resolve_value(customer, factor)
            if isinstance(value, (datetime.date, datetime.datetime)):
                value = value.isoformat()
            snapshot[factor.field] = value
    return snapshot


def _is_duplicate(last: AssessmentHistory, assessment: AssessmentResponse, snapshot: dict) -> bool:
    return (
        abs(last.total_score - assessment.total_score) < TREND_EPSILON
        and (last.factor_snapshot or {}) == snapshot
    )


def record_assessment(
    db: Session,
    customer: Customer,
    assessment: AssessmentResponse | None = None,
    *,
    assessed_by: str = "system",
    trigger: str = "manual",
    strategy_snapshot: list | None = None,
    skip_if_unchanged: bool = True,
    commit: bool = True,
) -> AssessmentHistory | None:
    """写入一条评估快照。

    `skip_if_unchanged=True` 时，若分数与因子取值与上一条完全一致则不重复记录，
    避免页面每次打开都往历史表灌数据。
    """
    if assessment is None:
        assessment = get_scoring_strategy().evaluate(customer)

    snapshot = build_factor_snapshot(customer)

    if skip_if_unchanged:
        last = (
            db.query(AssessmentHistory)
            .filter(AssessmentHistory.customer_id == customer.id)
            .order_by(AssessmentHistory.assessed_at.desc(), AssessmentHistory.id.desc())
            .first()
        )
        if last and _is_duplicate(last, assessment, snapshot):
            return None

    record = AssessmentHistory(
        customer_id=customer.id,
        assessed_by=assessed_by,
        trigger=trigger,
        total_score=assessment.total_score,
        max_score=assessment.max_score,
        level=assessment.level,
        level_color=assessment.level_color,
        dimensions=[d.model_dump() for d in assessment.dimensions],
        risk_alerts=list(assessment.risk_alerts),
        factor_snapshot=snapshot,
        strategy_snapshot=strategy_snapshot or [],
        config_version=assessment.config_version,
        assessed_at=utcnow(),
    )
    db.add(record)
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record


def attach_strategy_snapshot(db: Session, customer_id: int, items: list, commit: bool = True) -> bool:
    """把 AI 生成的策略回写到最近一条评估快照。

    PDF 报告与"上次给过什么建议"的追溯都依赖这份快照；
    没有历史记录时静默跳过，不影响对话主流程。
    """
    if not items:
        return False
    last = (
        db.query(AssessmentHistory)
        .filter(AssessmentHistory.customer_id == customer_id)
        .order_by(AssessmentHistory.assessed_at.desc(), AssessmentHistory.id.desc())
        .first()
    )
    if last is None:
        return False
    last.strategy_snapshot = list(items)
    if commit:
        db.commit()
    return True


def list_history(db: Session, customer_id: int, limit: int = 50) -> list[AssessmentHistory]:
    """按时间倒序返回评估历史。"""
    return (
        db.query(AssessmentHistory)
        .filter(AssessmentHistory.customer_id == customer_id)
        .order_by(AssessmentHistory.assessed_at.desc(), AssessmentHistory.id.desc())
        .limit(limit)
        .all()
    )


def to_history_items(records: list[AssessmentHistory]) -> list[AssessmentHistoryItem]:
    return [AssessmentHistoryItem.model_validate(r) for r in records]


def build_trend(
    db: Session,
    customer: Customer,
    limit: int = 12,
    fallback_assessment: AssessmentResponse | None = None,
) -> AssessmentTrendResponse:
    """构造趋势图数据：按时间正序的点位 + 趋势箭头 + 等级参考线。"""
    config = load_scoring_config()
    records = list(reversed(list_history(db, customer.id, limit=limit)))

    points = [
        TrendPoint(
            assessed_at=r.assessed_at,
            label=r.assessed_at.strftime("%m-%d"),
            total_score=r.total_score,
            level=r.level,
            dimensions={
                str(d.get("name") or d.get("key") or ""): float(d.get("score") or 0)
                for d in (r.dimensions or [])
            },
        )
        for r in records
    ]

    if records:
        latest = records[-1]
        latest_score, level, level_color = latest.total_score, latest.level, latest.level_color
        max_score = latest.max_score or config.total_max_score
        previous_score = records[-2].total_score if len(records) >= 2 else None
    else:
        current = fallback_assessment or get_scoring_strategy().evaluate(customer)
        latest_score, level, level_color = current.total_score, current.level, current.level_color
        max_score = current.max_score
        previous_score = None

    delta = round(latest_score - previous_score, 1) if previous_score is not None else 0.0
    if delta > TREND_EPSILON:
        trend = "up"
    elif delta < -TREND_EPSILON:
        trend = "down"
    else:
        trend = "flat"

    return AssessmentTrendResponse(
        customer_id=customer.id,
        customer_name=customer.customer_name,
        max_score=max_score,
        points=points,
        latest_score=latest_score,
        previous_score=previous_score,
        delta=delta,
        trend=trend,
        level=level,
        level_color=level_color,
        level_lines=[
            LevelConfigItem(name=lv.name, min_score=lv.min_score, color=lv.color)
            for lv in config.levels
            if lv.min_score > 0
        ],
    )
