"""配置驱动的打分规则求值器。

`config_loader` 负责「读什么」，本模块负责「怎么算」：
把 scoring_config.yaml 里声明的 rule / condition 翻译成分数与明细文案。
所有输出文案与早期硬编码算法逐字一致，保证配置化改造前后评分结果不变。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field as dc_field
from typing import Any

from .config_loader import Condition, DimensionConfig, FactorConfig


@dataclass
class FactorScore:
    """单个因子的计分结果。"""

    field: str
    label: str
    score: float
    details: list[str] = dc_field(default_factory=list)


@dataclass
class DimensionResult:
    """单个维度的计分结果。"""

    key: str
    name: str
    score: float
    max_score: float
    details: list[str] = dc_field(default_factory=list)
    factor_scores: list[FactorScore] = dc_field(default_factory=list)


# ──────────────────────────── 取值与格式化 ────────────────────────────


def resolve_value(customer: Any, factor: FactorConfig) -> Any:
    """按因子声明的来源取值：模型列 或 custom_fields 扩展字段。"""
    if factor.source == "custom_fields":
        extra = getattr(customer, "custom_fields", None) or {}
        if isinstance(extra, dict):
            return extra.get(factor.field)
        return None
    return getattr(customer, factor.field, None)


def resolve_field(customer: Any, field_name: str, source: str = "model") -> Any:
    if source == "custom_fields":
        extra = getattr(customer, "custom_fields", None) or {}
        return extra.get(field_name) if isinstance(extra, dict) else None
    return getattr(customer, field_name, None)


class _SafeFormatDict(dict):
    """占位符缺失时原样保留，便于发现配置里的拼写错误。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        # 展示口径：小数最多保留两位并去尾零（0.233333→0.23，80.0→80）；
        # 不用 :g——有效数字超 6 位会退化为科学计数法（1234567.8→1.23457e+06）
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def first_condition_field(condition: Condition) -> tuple[str, str]:
    """取条件树第一个叶子条件的 (field, source)。

    用于复合条件（all/any）预警文案的 {value} 取值：顶层无 field 时向下找。
    """
    if condition.field:
        return condition.field, condition.source
    for child in condition.children:
        field_name, source = first_condition_field(child)
        if field_name:
            return field_name, source
    return "", "model"


def render_detail(template: str, **kwargs: Any) -> str:
    """渲染明细文案模板，支持 {value} / {score} / {days} 占位符。"""
    if not template:
        return ""
    safe = _SafeFormatDict({k: _stringify(v) for k, v in kwargs.items()})
    try:
        return template.format_map(safe)
    except (ValueError, IndexError):
        return template


def _to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "y", "是", "有")


# 录入时的"占位空值"文案：填写这些值视为「无内容」，truthy 判定不生效。
# 典型场景：风险信号随手填"无"/"暂无"，不应触发 penalty 扣分与风险预警。
EMPTY_LIKE_STRINGS = frozenset({
    "无", "暂无", "没有", "无风险", "暂无风险", "无异常", "暂无异常", "无风险信号", "无。",
    "none", "null", "n/a", "na", "-", "--", "/",
})


def is_effectively_empty(value: Any) -> bool:
    """业务意义上的「空」：None / 空串 / 空容器 / 占位空值文案。"""
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return not text or text.lower() in EMPTY_LIKE_STRINGS
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def effectively_truthy(value: Any) -> bool:
    """truthy 判定的业务口径：Python 真值 且 非占位空值。"""
    return bool(value) and not is_effectively_empty(value)


def _to_date(value: Any) -> datetime.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _bracket_matches(bracket: dict[str, Any], number: float) -> bool:
    """分档条件：同一档内多个比较符取「与」关系。"""
    checks = (
        ("gte", lambda a, b: a >= b),
        ("gt", lambda a, b: a > b),
        ("lte", lambda a, b: a <= b),
        ("lt", lambda a, b: a < b),
        ("eq", lambda a, b: a == b),
    )
    matched_any = False
    for key, op in checks:
        if key not in bracket:
            continue
        threshold = _to_number(bracket[key])
        if threshold is None or not op(number, threshold):
            return False
        matched_any = True
    return matched_any


# ──────────────────────────── 规则求值 ────────────────────────────


def evaluate_factor(
    factor: FactorConfig,
    value: Any,
    today: datetime.date | None = None,
) -> FactorScore:
    """按因子配置计算得分与明细。"""
    today = today or datetime.date.today()
    rule = factor.rule
    params = rule.params
    handler = _RULE_HANDLERS.get(rule.type)
    if handler is None:  # pragma: no cover - 加载期已校验
        raise ValueError(f"未知的打分规则类型：{rule.type}")
    score, details = handler(params, value, today)
    return FactorScore(field=factor.field, label=factor.label, score=score, details=details)


def _rule_threshold(params: dict, value: Any, today: datetime.date) -> tuple[float, list[str]]:
    number = _to_number(value)
    if number is not None:
        for bracket in params.get("brackets", []):
            if _bracket_matches(bracket, number):
                score = bracket.get("score", 0)
                detail = render_detail(bracket.get("detail", ""), value=value, score=score)
                return float(score), [detail] if detail else []

    default = params.get("default") or {}
    score = default.get("score", 0)
    detail = render_detail(default.get("detail", ""), value=value, score=score)
    return float(score), [detail] if detail else []


def _rule_mapping(params: dict, value: Any, today: datetime.date) -> tuple[float, list[str]]:
    table = params.get("map") or {}
    hit = True
    if value in table:
        score = table[value]
    elif _stringify(value) in table:
        score = table[_stringify(value)]
    else:
        score = params.get("default_score", 0)
        hit = False

    if not hit and params.get("omit_detail_on_default"):
        return float(score), []

    detail = render_detail(params.get("detail", ""), value=value, score=score)
    return float(score), [detail] if detail else []


def _rule_days_since(params: dict, value: Any, today: datetime.date) -> tuple[float, list[str]]:
    parsed = _to_date(value)
    if parsed is None:
        empty = params.get("empty") or {}
        score = empty.get("score", 0)
        detail = render_detail(empty.get("detail", ""), value=value, score=score)
        return float(score), [detail] if detail else []

    days = (today - parsed).days
    for bracket in params.get("brackets", []):
        if _bracket_matches(bracket, days):
            score = bracket.get("score", 0)
            detail = render_detail(bracket.get("detail", ""), value=value, score=score, days=days)
            return float(score), [detail] if detail else []

    default = params.get("default") or {}
    score = default.get("score", 0)
    detail = render_detail(default.get("detail", ""), value=value, score=score, days=days)
    return float(score), [detail] if detail else []


def _rule_linear(params: dict, value: Any, today: datetime.date) -> tuple[float, list[str]]:
    number = _to_number(value)
    if number is None:
        number = 0.0
    score = number * float(params.get("multiplier", 1)) + float(params.get("offset", 0))

    clamp = params.get("clamp") or {}
    if clamp.get("min") is not None:
        score = max(float(clamp["min"]), score)
    if clamp.get("max") is not None:
        score = min(float(clamp["max"]), score)

    detail = render_detail(params.get("detail", ""), value=value, score=score)
    return float(score), [detail] if detail else []


def _rule_penalty(params: dict, value: Any, today: datetime.date) -> tuple[float, list[str]]:
    when = params.get("when", "truthy")
    triggered = {
        "truthy": effectively_truthy,
        "falsy": lambda v: not effectively_truthy(v),
        "is_true": _to_bool,
        "is_false": lambda v: not _to_bool(v),
    }[when](value)

    if not triggered:
        return 0.0, []

    score = params.get("score", 0)
    detail = render_detail(params.get("detail", ""), value=value, score=score)
    return float(score), [detail] if detail else []


def _rule_constant(params: dict, value: Any, today: datetime.date) -> tuple[float, list[str]]:
    score = params.get("score", 0)
    detail = render_detail(params.get("detail", ""), value=value, score=score)
    return float(score), [detail] if detail else []


_RULE_HANDLERS = {
    "threshold": _rule_threshold,
    "mapping": _rule_mapping,
    "days_since": _rule_days_since,
    "linear": _rule_linear,
    "penalty": _rule_penalty,
    "constant": _rule_constant,
}


# ──────────────────────────── 维度求值 ────────────────────────────


def evaluate_dimension(
    dimension: DimensionConfig,
    customer: Any,
    today: datetime.date | None = None,
) -> DimensionResult:
    """计算单个维度得分：基础分 + 各因子得分，最后按 clamp 截断。"""
    today = today or datetime.date.today()

    score = float(dimension.base_score)
    details: list[str] = []
    if dimension.base_detail:
        details.append(dimension.base_detail)

    factor_scores: list[FactorScore] = []
    for factor in dimension.enabled_factors:
        result = evaluate_factor(factor, resolve_value(customer, factor), today)
        score += result.score
        details.extend(result.details)
        factor_scores.append(result)

    if dimension.clamp_min is not None:
        score = max(float(dimension.clamp_min), score)
    if dimension.clamp_max is not None:
        score = min(float(dimension.clamp_max), score)

    return DimensionResult(
        key=dimension.key,
        name=dimension.name,
        score=score,
        max_score=dimension.max_score,
        details=details,
        factor_scores=factor_scores,
    )


# ──────────────────────────── 条件求值 ────────────────────────────


def evaluate_condition(
    condition: Condition,
    customer: Any,
    today: datetime.date | None = None,
) -> bool:
    """判定预警 / 机会点条件，支持 all / any 嵌套。"""
    today = today or datetime.date.today()

    if condition.logic == "all":
        return all(evaluate_condition(c, customer, today) for c in condition.children)
    if condition.logic == "any":
        return any(evaluate_condition(c, customer, today) for c in condition.children)

    value = resolve_field(customer, condition.field, condition.source)
    expected = condition.value
    op = condition.op

    if op == "truthy":
        return effectively_truthy(value)
    if op == "falsy":
        return not effectively_truthy(value)
    if op == "is_true":
        return _to_bool(value)
    if op == "is_false":
        return not _to_bool(value)
    if op == "empty":
        return value is None or value == ""
    if op == "eq":
        return value == expected or _stringify(value) == _stringify(expected)
    if op == "ne":
        return not (value == expected or _stringify(value) == _stringify(expected))
    if op in ("lt", "lte", "gt", "gte"):
        left, right = _to_number(value), _to_number(expected)
        if left is None or right is None:
            return False
        return {
            "lt": left < right,
            "lte": left <= right,
            "gt": left > right,
            "gte": left >= right,
        }[op]
    if op in ("in", "not_in"):
        candidates = expected if isinstance(expected, (list, tuple, set)) else [expected]
        hit = value in candidates or _stringify(value) in [_stringify(c) for c in candidates]
        return hit if op == "in" else not hit
    if op == "contains":
        return _stringify(expected) in _stringify(value)
    if op in ("days_since_gt", "days_since_gt_or_empty"):
        parsed = _to_date(value)
        if parsed is None:
            return op == "days_since_gt_or_empty"
        threshold = _to_number(expected)
        return threshold is not None and (today - parsed).days > threshold

    return False  # pragma: no cover - 加载期已校验 op 合法性
