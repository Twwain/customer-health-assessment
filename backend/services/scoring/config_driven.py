"""配置驱动的评分策略。

维度、因子、权重、打分规则、预警规则全部来自 ``backend/scoring_config.yaml``。
默认配置为 7 维度加权算法（满分 100），
改配置即可调整算法，无需改代码。
"""

from __future__ import annotations

import datetime

from database import utcnow
from models import Customer
from schemas import AlertItem, AssessmentResponse, DimensionScore

from .base import ScoringStrategy
from .config_loader import ScoringConfig, load_scoring_config
from .rules import (
    DimensionResult,
    evaluate_condition,
    evaluate_dimension,
    render_detail,
    resolve_field,
)

class ConfigDrivenStrategy(ScoringStrategy):
    """读取 scoring_config.yaml 计算基础客情分。"""

    def __init__(self, config_path: str | None = None):
        self._config_path = config_path

    # ── 配置 ──────────────────────────────────────────────────────────

    @property
    def config(self) -> ScoringConfig:
        """每次访问都会校验配置文件 mtime，改动后自动重新加载。"""
        return load_scoring_config(self._config_path)

    # ── 主流程 ────────────────────────────────────────────────────────

    def evaluate(self, c: Customer, today: datetime.date | None = None) -> AssessmentResponse:
        config = self.config
        # 天数类计算（距今 N 天）刻意用本地日期，与用户时区感知一致；
        # assessed_at 用 UTC 时间戳仅用于历史排序，不参与天数运算。
        today = today or datetime.date.today()

        results = [evaluate_dimension(dim, c, today) for dim in config.enabled_dimensions]
        total = sum(r.score for r in results)
        level, color = self._level(total, config)
        alerts, suggestions = self._analyze(c, config, today)

        return AssessmentResponse(
            customer_id=c.id,
            customer_name=c.customer_name,
            total_score=round(total, 1),
            max_score=config.total_max_score,
            level=level,
            level_color=color,
            dimensions=[self._to_schema(r) for r in results],
            risk_alerts=[a.message for a in alerts],
            alerts=alerts,
            suggestions=suggestions,
            config_version=config.version,
            assessed_at=utcnow(),
        )

    def _analyze(
        self,
        c: Customer,
        config: ScoringConfig,
        today: datetime.date,
    ) -> tuple[list[AlertItem], list[str]]:
        """按配置生成风险预警与建议（顺序即配置顺序）。"""
        alerts: list[AlertItem] = []
        suggestions: list[str] = []

        for rule in config.alerts:
            if not evaluate_condition(rule.when, c, today):
                continue
            value = resolve_field(c, rule.when.field, rule.when.source) if rule.when.field else None
            alerts.append(
                AlertItem(
                    id=rule.id,
                    level=rule.level,
                    message=render_detail(rule.message, value=value),
                )
            )
            if rule.suggestion:
                suggestions.append(rule.suggestion)

        for opportunity in config.opportunities:
            if evaluate_condition(opportunity.when, c, today):
                suggestions.append(opportunity.suggestion)

        return alerts, suggestions

    # ── 工具 ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_schema(result: DimensionResult) -> DimensionScore:
        return DimensionScore(
            key=result.key,
            name=result.name,
            score=result.score,
            max_score=result.max_score,
            details=result.details,
        )

    def _level(self, total: float, config: ScoringConfig | None = None) -> tuple[str, str]:
        """按配置的等级阈值判定等级（levels 已按 min_score 降序排序）。"""
        config = config or self.config
        for level in config.levels:
            if total >= level.min_score:
                return level.name, level.color
        last = config.levels[-1]
        return last.name, last.color

    def _dimension_score(
        self,
        key: str,
        c: Customer,
        today: datetime.date | None = None,
    ) -> DimensionScore:
        config = self.config
        for dim in config.dimensions:
            if dim.key == key or dim.name == key:
                return self._to_schema(evaluate_dimension(dim, c, today))
        raise KeyError(f"scoring_config.yaml 中不存在维度 `{key}`")
