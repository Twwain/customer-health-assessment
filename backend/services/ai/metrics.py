"""AI 对话运行指标采集与聚合。

只记录运行元数据，不记录问题、回复、客户 ID、Prompt 或工具结果。
"""

from __future__ import annotations

import datetime
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import config
from database import utcnow
from models import ChatTurnMetric

logger = logging.getLogger(__name__)


def _percentile(values: list[int], percentile: float) -> int:
    """返回 nearest-rank 分位数；空样本返回 0。"""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return int(ordered[rank - 1])


def _distribution(values: list[int]) -> dict[str, int | float]:
    if not values:
        return {"count": 0, "avg": 0, "p50": 0, "p90": 0, "p95": 0, "p99": 0, "max": 0}
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 2),
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values),
    }


def _error_code(message: str) -> str:
    text = message or ""
    if "token 预算" in text:
        return "token_budget_exceeded"
    if "超过总时限" in text:
        return "turn_timeout"
    if "未能及时取消" in text:
        return "cancel_late"
    if "正在生成中" in text:
        return "session_busy"
    return "generation_error" if text else ""


@dataclass
class TurnMetricObserver:
    """从现有 SSE/非流式事件中提取一轮指标。"""

    session_id: int | None
    scenario: str
    started: float = 0.0
    model: str = ""
    status: str = "running"
    tokens_used: int = 0
    timings: dict[str, Any] | None = None
    warning_count: int = 0
    error_code: str = ""
    persisted: bool = False
    token_budget: int | None = None

    def __post_init__(self) -> None:
        self.started = time.monotonic()

    def consume_usage(self, data: dict[str, Any]) -> None:
        """接收不依赖客户端消费进度的成本快照，断线时仍能持久化。"""
        self.tokens_used = max(self.tokens_used, int(data.get("tokens_used") or 0))
        call = data.get("llm_call") or {}
        if call:
            timings = dict(self.timings or {})
            timings["llm_call_count"] = int(timings.get("llm_call_count") or 0) + 1
            self.timings = timings

    def consume(self, event: Any) -> None:
        data = event.data or {}
        if event.type == "start":
            self.scenario = str(data.get("scenario") or self.scenario)
            self.model = str(data.get("model") or "")
        elif event.type == "warning":
            self.warning_count += 1
            code = _error_code(str(data.get("message") or ""))
            if code != "generation_error":
                self.error_code = code
        elif event.type == "usage":
            self.tokens_used = max(self.tokens_used, int(data.get("tokens_used") or 0))
        elif event.type == "error":
            self.error_code = _error_code(str(data.get("message") or ""))
            self.status = "timeout" if self.error_code == "turn_timeout" else "error"
            observed = dict(self.timings or {})
            incoming = dict(data.get("timings") or {})
            merged = {**observed, **incoming}
            merged["llm_call_count"] = max(
                int(observed.get("llm_call_count") or 0),
                int(incoming.get("llm_call_count") or 0),
            )
            self.timings = merged
            self.tokens_used = int(data.get("tokens_used") or self.tokens_used)
        elif event.type == "done":
            self.tokens_used = int(data.get("tokens_used") or 0)
            self.timings = data.get("timings") or {}
            self.model = str(data.get("model") or self.model)
            if data.get("degraded"):
                self.status = "degraded"
            elif self.error_code == "turn_timeout":
                self.status = "timeout"
            elif data.get("incomplete"):
                self.status = "partial"
                self.error_code = self.error_code or "generation_interrupted"
            else:
                self.status = "success"

    def persist(self, db: Any) -> None:
        if self.persisted:
            return
        self.persisted = True
        timings = self.timings or {}
        if self.status == "running":
            self.status = "cancelled"
            self.error_code = self.error_code or "client_cancelled"
        latency_ms = int(timings.get("total_ms") or ((time.monotonic() - self.started) * 1000))
        budget = int(config.CHAT_TURN_TOKEN_BUDGET if self.token_budget is None else self.token_budget)
        metric = ChatTurnMetric(
            session_id=self.session_id,
            scenario=self.scenario,
            status=self.status,
            model=self.model,
            tokens_used=self.tokens_used,
            token_budget=budget,
            token_overshoot=max(0, self.tokens_used - budget),
            latency_ms=latency_ms,
            llm_call_count=int(timings.get("llm_call_count") or 0),
            tool_rounds=int(timings.get("tool_rounds") or 0),
            refine_rounds=int(timings.get("refine_rounds") or 0),
            queue_capacity=int(timings.get("event_queue_capacity") or 0),
            queue_high_watermark=int(timings.get("event_queue_high_watermark") or 0),
            warning_count=self.warning_count,
            error_code=self.error_code,
        )
        try:
            cutoff = utcnow() - datetime.timedelta(
                days=config.CHAT_METRICS_RETENTION_DAYS
            )
            db.query(ChatTurnMetric).filter(ChatTurnMetric.created_at < cutoff).delete()
            db.add(metric)
            db.commit()
        except Exception as exc:  # 指标故障绝不能影响主对话
            db.rollback()
            logger.warning("chat_turn_metric 写入失败：%s", exc)


def record_report_metric(db: Any, report: Any, *, elapsed_ms: int) -> None:
    """在报告调用方完成业务提交后记录成本；不保存正文、客户或核对证据。"""
    try:
        if not report.has_ai:
            return
        from services.ai.llm_adapter import get_chat_adapter

        def count(value: Any) -> int:
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError, OverflowError):
                return 0

        timings = report.timings if isinstance(report.timings, dict) else {}
        calls = timings.get("llm_calls")
        calls = calls if isinstance(calls, list) else []
        verification = timings.get("verification")
        verification = verification if isinstance(verification, list) else []
        last_check = verification[-1] if verification and isinstance(verification[-1], dict) else {}
        verified = last_check.get("status") == "passed" and not last_check.get("reason")
        degraded = bool(report.degraded)
        observer = TurnMetricObserver(
            session_id=None,
            scenario="report",
            model=str(getattr(get_chat_adapter(), "model", "") or "")[:100],
            status="degraded" if degraded else "success" if verified else "partial",
            tokens_used=sum(count(call.get("total_tokens")) for call in calls if isinstance(call, dict)),
            timings={
                "total_ms": count(elapsed_ms),
                "llm_call_count": len(calls),
                "tool_rounds": count(timings.get("tool_rounds")),
                "refine_rounds": count(timings.get("refine_rounds")),
            },
            warning_count=len(report.warnings) if isinstance(report.warnings, list) else 0,
            error_code=("report_degraded" if degraded else "" if verified else
                        "fact_check_issues" if last_check.get("status") == "issues" else "fact_check_unverified"),
            token_budget=config.REPORT_TURN_TOKEN_BUDGET,
        )
        observer.persist(db)
    except Exception:  # 元数据格式或指标设施故障不影响报告/PDF
        logger.warning("report_metric 采集失败", exc_info=True)


def summarize(db: Any, *, hours: int = 168) -> dict[str, Any]:
    """聚合最近一段时间的真实运行分布，默认最近 7 天。"""
    since = utcnow() - datetime.timedelta(hours=hours)
    rows = (
        db.query(ChatTurnMetric)
        .filter(ChatTurnMetric.created_at >= since)
        .order_by(ChatTurnMetric.created_at.asc())
        .all()
    )

    def summarize_rows(items: list[ChatTurnMetric]) -> dict[str, Any]:
        statuses = Counter(row.status for row in items)
        overshoots = [row.token_overshoot for row in items if row.token_overshoot > 0]
        queue_rows = [row for row in items if row.queue_capacity > 0]
        return {
            "turns": len(items),
            "status_counts": dict(statuses),
            "success_rate": round(statuses.get("success", 0) / len(items), 4) if items else 0,
            "degraded_rate": round(statuses.get("degraded", 0) / len(items), 4) if items else 0,
            "timeout_rate": round(statuses.get("timeout", 0) / len(items), 4) if items else 0,
            "tokens": _distribution([row.tokens_used for row in items if row.tokens_used > 0]),
            "latency_ms": _distribution([row.latency_ms for row in items if row.latency_ms > 0]),
            "llm_call_count": _distribution([row.llm_call_count for row in items]),
            "tool_rounds": _distribution([row.tool_rounds for row in items]),
            "refine_rounds": _distribution([row.refine_rounds for row in items]),
            "token_budget_exceeded_count": len(overshoots),
            "token_overshoot": _distribution(overshoots),
            "queue_saturation_count": sum(
                row.queue_high_watermark >= row.queue_capacity for row in queue_rows
            ),
            "queue_high_watermark": _distribution(
                [row.queue_high_watermark for row in queue_rows]
            ),
        }

    scenarios = sorted({row.scenario for row in rows})
    return {
        "window_hours": hours,
        "since": since.isoformat() + "Z",
        "generated_at": utcnow().isoformat() + "Z",
        "configured_token_budget": config.CHAT_TURN_TOKEN_BUDGET,
        "configured_report_token_budget": config.REPORT_TURN_TOKEN_BUDGET,
        "configured_timeout_seconds": config.CHAT_TURN_TIMEOUT_SECONDS,
        "configured_chat_concurrency": config.CHAT_LLM_GLOBAL_CONCURRENCY,
        "overall": summarize_rows(rows),
        "by_scenario": {
            scenario: summarize_rows([row for row in rows if row.scenario == scenario])
            for scenario in scenarios
        },
    }
