"""配置驱动的评分策略。

维度、因子、权重、打分规则、预警规则全部来自 ``backend/scoring_config.yaml``。
默认配置为 7 维度加权算法（满分 100），
改配置即可调整算法，无需改代码。
"""

from __future__ import annotations

import datetime
import json
import math

from database import utcnow
from models import Customer
from schemas import AlertItem, AssessmentResponse, DimensionScore, FactorScoreItem

from .base import ScoringStrategy
from .config_loader import KEY_PERSON_COUNT, ScoringConfig, load_scoring_config, sub_dimension_of
from .rules import (
    DimensionResult,
    evaluate_condition,
    evaluate_dimension,
    first_condition_field,
    render_detail,
    resolve_field,
    resolve_value,
)


def _valid_alert_value(factor, value) -> bool:
    """缺失及非法填报不能凭默认计分确认为业务风险。兼容历史数值填报。"""
    if value is None or isinstance(value, str) and not value.strip():
        return False
    spec = factor.input
    if spec.type == "key_person_levels":
        try:
            items = json.loads(value) if isinstance(value, str) else value
        except (ValueError, TypeError):
            return False
        return (isinstance(items, (list, tuple)) and len(items) == KEY_PERSON_COUNT
                and all(not isinstance(item, bool) and str(item) in spec.options for item in items))
    if spec.options and value in spec.options and not isinstance(value, bool):
        return True
    if spec.type in ("number", "slider") or factor.rule.type == "threshold":
        # 旧数据允许原始数字；不从任意非法字符串中截取数字来制造已确认预警。
        try:
            number = float(value)
        except (ValueError, TypeError):
            return False
        return (not isinstance(value, bool) and math.isfinite(number)
                and (spec.min is None or number >= spec.min)
                and (spec.max is None or number <= spec.max)
                and (not spec.step or math.isclose((number - (spec.min or 0)) / spec.step,
                     round((number - (spec.min or 0)) / spec.step), abs_tol=1e-8)))
    if spec.type == "bool":
        return isinstance(value, bool)
    if spec.type == "date":
        try:
            datetime.date.fromisoformat(str(value))
            return True
        except ValueError:
            return False
    return not spec.options and isinstance(value, str) and bool(value.strip())


def _condition_fields(condition):
    """遍历全部条件叶子，避免复合条件绕过因子资料合法性检查。"""
    if condition.field:
        yield condition.field, condition.source
    for child in condition.children:
        yield from _condition_fields(child)


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
        alerts, suggestions = self._analyze(c, config, today, results, total)
        sub_map = {}
        for dim in config.enabled_dimensions:
            for f in dim.factors:
                sub_map[f.field] = sub_dimension_of(f.description)

        return AssessmentResponse(
            customer_id=c.id,
            customer_name=c.customer_name,
            total_score=round(total, 1),
            max_score=config.total_max_score,
            level=level,
            level_color=color,
            dimensions=[self._to_schema(r, sub_map) for r in results],
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
        results: list[DimensionResult],
        total: float,
    ) -> tuple[list[AlertItem], list[str]]:
        """按配置生成风险预警与建议（顺序即配置顺序）。"""
        alerts: list[AlertItem] = []
        suggestions: list[str] = []
        alert_suggestions: dict[str, str] = {}
        related_fields: dict[str, str] = {}
        definitions = {f.field: f for dim in config.enabled_dimensions for f in dim.enabled_factors}
        gaps = {field for field, factor in definitions.items()
                if not _valid_alert_value(factor, resolve_value(c, factor))}

        for rule in config.alerts:
            if any(field in gaps and definitions[field].source == source
                   for field, source in _condition_fields(rule.when)):
                continue
            if not evaluate_condition(rule.when, c, today):
                continue
            # 复合条件（all/any）顶层无 field，向下取第一个叶子字段供 {value} 渲染
            field_name, source = first_condition_field(rule.when)
            value = resolve_field(c, field_name, source) if field_name else None
            related_fields[rule.id] = field_name
            alerts.append(
                AlertItem(
                    id=rule.id,
                    level=rule.level,
                    message=render_detail(rule.message, value=value),
                )
            )
            if rule.suggestion:
                alert_suggestions[rule.id] = rule.suggestion

        dimensions = {result.key: result for result in results}
        factors = {
            factor.field: factor
            for result in results
            for factor in result.factor_scores
        }
        comparisons = {
            "lt": lambda left, right: left < right,
            "lte": lambda left, right: left <= right,
            "gt": lambda left, right: left > right,
            "gte": lambda left, right: left >= right,
            "eq": lambda left, right: left == right,
        }
        for rule in config.score_alerts:
            if rule.target == "total":
                value = float(total)
            elif rule.target == "dimension":
                result = dimensions.get(rule.key)
                if result is None or not result.max_score:
                    continue
                value = result.score / result.max_score * 100.0
            else:
                factor = factors.get(rule.key)
                if factor is None:
                    continue
                if rule.key in gaps:
                    continue
                value = factor.score

            if not comparisons[rule.op](value, rule.value):
                continue
            message = render_detail(rule.message, value=round(value, 1))
            level = rule.level
            if rule.target == "dimension":
                dim = next(dim for dim in config.enabled_dimensions if dim.key == rule.key)
                if any(f.field in gaps for f in dim.enabled_factors):
                    level = "low"
                    message = (f"{dim.name}得分率 {round(value, 1)}%，包含缺失或非法资料的默认计分；"
                               "需先核实资料，不能据此确认业务风险")
            if rule.target == "factor":
                related_fields[rule.id] = rule.key
            alerts.append(
                AlertItem(
                    id=rule.id,
                    level=level,
                    message=message,
                )
            )
            if rule.suggestion:
                alert_suggestions[rule.id] = rule.suggestion

        for opportunity in config.opportunities:
            if evaluate_condition(opportunity.when, c, today):
                suggestions.append(opportunity.suggestion)

        # 同因子专项严重预警覆盖通用低分预警，维度/趋势预警保持独立。
        priorities = {"low": 0, "medium": 1, "high": 2}
        best = {}
        for alert in alerts:
            field = related_fields.get(alert.id)
            if field and (field not in best or priorities[alert.level] > priorities[best[field].level]):
                best[field] = alert
        alerts = [a for a in alerts if not related_fields.get(a.id)
                  or best[related_fields[a.id]] is a]
        suggestions = [alert_suggestions[a.id] for a in alerts if a.id in alert_suggestions] + suggestions
        for field in sorted(gaps):
            factor = definitions[field]
            alerts.append(AlertItem(id=f"{field}_data_gap", level="low",
                message=f"{factor.label}资料缺失或取值非法，当前计分仅作默认参考，需核实后判断"))

        return alerts, suggestions

    # ── 工具 ──────────────────────────────────────────────────────────

    def _to_schema(
        self,
        result: DimensionResult,
        sub_map: dict[str, str] | None = None,
    ) -> DimensionScore:
        if sub_map is None:
            # 兜底：按当前实例的配置路径解析，避免依赖全局默认配置
            sub_map = {
                f.field: sub_dimension_of(f.description)
                for dim in self.config.enabled_dimensions
                for f in dim.factors
            }
        factors = [
            FactorScoreItem(
                field=fs.field,
                label=fs.label,
                sub_dimension=sub_map.get(fs.field, ""),
                detail="\n".join(fs.details) if fs.details else "",
                score=fs.score,
            )
            for fs in result.factor_scores
        ]
        return DimensionScore(
            key=result.key,
            name=result.name,
            score=result.score,
            max_score=result.max_score,
            details=result.details,
            factors=factors,
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
