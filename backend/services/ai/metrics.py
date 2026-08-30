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

    session_id: int
    scenario: str
    started: float = 0.0
    model: str = ""
    status: str = "running"
    tokens_used: int = 0
    timings: dict[str, Any] | None = None
    warning_count: int = 0
    error_code: str = ""
    persisted: bool = False

    def __post_init__(self) -> None:
        self.started = time.monotonic()

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
        elif event.type == "error":
            self.error_code = _error_code(str(data.get("message") or ""))
            self.status = "timeout" if self.error_code == "turn_timeout" else "error"
            self.timings = data.get("timings") or self.timings
            self.tokens_used = int(data.get("tokens_used") or self.tokens_used)
        elif event.type == "done":
            self.tokens_used = int(data.get("tokens_used") or 0)
            self.timings = data.get("timings") or {}
            self.model = str(data.get("model") or self.model)
            if data.get("degraded"):
                self.status = "degraded"
            elif self.error_code:
                self.status = "error"
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
        budget = int(config.CHAT_TURN_TOKEN_BUDGET)
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
            db.add(metric)
            db.commit()
        except Exception as exc:  # 指标故障绝不能影响主对话
            db.rollback()
            logger.warning("chat_turn_metric 写入失败：%s", exc)


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
        "configured_timeout_seconds": config.CHAT_TURN_TIMEOUT_SECONDS,
        "configured_chat_concurrency": config.CHAT_LLM_GLOBAL_CONCURRENCY,
        "overall": summarize_rows(rows),
        "by_scenario": {
            scenario: summarize_rows([row for row in rows if row.scenario == scenario])
            for scenario in scenarios
        },
    }
