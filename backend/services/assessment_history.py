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
    AlertItem,
    AssessmentHistoryItem,
    AssessmentResponse,
    AssessmentTrendResponse,
    LevelConfigItem,
    TrendPoint,
)
from services.scoring import get_scoring_strategy, load_scoring_config

# 趋势判定阈值：分差小于该值视为持平
TREND_EPSILON = 0.05


def _quarter_index(value: datetime.datetime) -> int:
    """把时间转换为单调递增的季度编号，便于判断季度是否连续。"""
    return value.year * 4 + (value.month - 1) // 3


def apply_trend_alerts(
    db: Session,
    customer: Customer,
    assessment: AssessmentResponse,
    *,
    as_of: datetime.datetime | None = None,
) -> AssessmentResponse:
    """把依赖历史快照的最新版预警合并进本次评估结果。

    同一季度只取最后一次评估；“连续两个季度下降 > 10”要求最近三个连续季度
    的两次环比均下降，且首尾累计降幅严格大于 10 分。
    """
    config = load_scoring_config()
    if not config.trend_alerts:
        return assessment

    as_of = as_of or utcnow()
    records = (
        db.query(AssessmentHistory)
        .filter(
            AssessmentHistory.customer_id == customer.id,
            AssessmentHistory.config_version == assessment.config_version,
            AssessmentHistory.assessed_at <= as_of,
        )
        .order_by(AssessmentHistory.assessed_at.asc(), AssessmentHistory.id.asc())
        .all()
    )
    quarter_scores: dict[int, float] = {
        _quarter_index(record.assessed_at): float(record.total_score)
        for record in records
    }
    quarter_scores[_quarter_index(as_of)] = float(assessment.total_score)

    existing_ids = {alert.id for alert in assessment.alerts}
    for rule in config.trend_alerts:
        required_points = rule.consecutive_quarters + 1
        ordered = sorted(quarter_scores.items())
        if len(ordered) < required_points:
            continue
        window = ordered[-required_points:]
        quarter_ids = [quarter for quarter, _ in window]
        scores = [score for _, score in window]
        if any(
            right - left != 1
            for left, right in zip(quarter_ids, quarter_ids[1:])
        ):
            continue
        if not all(newer < older for older, newer in zip(scores, scores[1:])):
            continue
        drop = scores[0] - scores[-1]
        if drop <= rule.drop_gt or rule.id in existing_ids:
            continue

        score_text = " → ".join(f"{score:g}" for score in scores)
        message = rule.message.format(drop=f"{drop:g}", scores=score_text)
        assessment.alerts.append(AlertItem(id=rule.id, level=rule.level, message=message))
        assessment.risk_alerts.append(message)
        if rule.suggestion and rule.suggestion not in assessment.suggestions:
            assessment.suggestions.append(rule.suggestion)
        existing_ids.add(rule.id)

    return assessment


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
        and last.config_version == assessment.config_version
        and (last.risk_alerts or []) == list(assessment.risk_alerts)
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
    apply_trend_alerts(db, customer, assessment)

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


def backfill_current_config_snapshots(db: Session) -> int:
    """为缺少当前评分配置快照的客户补齐一次评估结果。

    配置版本升级后，总览与等级筛选只读取当前版本快照。这里在启动迁移阶段
    一次性回填缺失客户，避免每次请求都退化为全量实时评分。已有当前版本
    快照的客户会被跳过，因此重复启动不会产生重复记录。
    """
    scoring = get_scoring_strategy()
    config_version = scoring.config.version
    current_snapshot_customers = (
        db.query(AssessmentHistory.customer_id)
        .filter(AssessmentHistory.config_version == config_version)
        .distinct()
        .subquery()
    )
    customers = (
        db.query(Customer)
        .outerjoin(
            current_snapshot_customers,
            Customer.id == current_snapshot_customers.c.customer_id,
        )
        .filter(current_snapshot_customers.c.customer_id.is_(None))
        .all()
    )

    created = 0
    for customer in customers:
        assessment = scoring.evaluate(customer)
        record_assessment(
            db,
            customer,
            assessment,
            assessed_by="system",
            trigger="config_upgrade",
            skip_if_unchanged=False,
            commit=False,
        )
        created += 1

    if created:
        db.commit()
    return created


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
