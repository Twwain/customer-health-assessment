"""评分配置加载与校验（SOW §3.0 M0）。

把维度 / 因子 / 权重 / 规则从代码里搬到 ``backend/scoring_config.yaml``：
修改配置文件后重启服务即生效，无需改代码。

加载结果按「文件路径 + mtime」缓存，开发期改动配置文件会自动重新加载。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field as dc_field
from typing import Any

try:  # pragma: no cover - 环境缺依赖时给出可执行的修复指引
    import yaml
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "缺少 PyYAML 依赖，评分配置无法加载。请执行：pip install -r backend/requirements.txt"
    ) from exc


DEFAULT_CONFIG_FILENAME = "scoring_config.yaml"

VALID_RULE_TYPES = {"threshold", "mapping", "days_since", "linear", "penalty", "constant"}
VALID_CONDITION_OPS = {
    "truthy", "falsy", "is_true", "is_false", "empty",
    "eq", "ne", "lt", "lte", "gt", "gte",
    "in", "not_in", "contains",
    "days_since_gt", "days_since_gt_or_empty",
}
VALID_ALERT_LEVELS = {"high", "medium", "low"}
VALID_SOURCES = {"model", "custom_fields"}


class ScoringConfigError(ValueError):
    """评分配置文件格式错误。"""


# ──────────────────────────── 配置数据结构 ────────────────────────────


@dataclass(frozen=True)
class InputSpec:
    """因子录入控件描述，供前端 `GET /api/customers/factor-config` 动态渲染表单。"""

    type: str = "text"
    options: list[str] = dc_field(default_factory=list)
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    placeholder: str = ""

    @property
    def editable(self) -> bool:
        return self.type != "readonly"


@dataclass(frozen=True)
class RuleConfig:
    type: str
    params: dict[str, Any] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class FactorConfig:
    field: str
    label: str
    weight: float = 0.0
    source: str = "model"           # model / custom_fields
    source_role: str = ""           # 因子来源角色：销售 / 研发 / 市场 / HR ...
    description: str = ""
    enabled: bool = True
    input: InputSpec = dc_field(default_factory=InputSpec)
    rule: RuleConfig = dc_field(default_factory=lambda: RuleConfig("constant", {"score": 0}))


@dataclass(frozen=True)
class DimensionConfig:
    key: str
    name: str
    max_score: float
    enabled: bool = True
    description: str = ""
    base_score: float = 0.0
    base_detail: str = ""
    clamp_min: float | None = None
    clamp_max: float | None = None
    factors: list[FactorConfig] = dc_field(default_factory=list)

    @property
    def enabled_factors(self) -> list[FactorConfig]:
        return [f for f in self.factors if f.enabled]


@dataclass(frozen=True)
class LevelConfig:
    name: str
    min_score: float
    color: str


@dataclass(frozen=True)
class Condition:
    """预警 / 机会点判定条件，支持 all / any 嵌套。"""

    op: str = ""
    field: str = ""
    value: Any = None
    source: str = "model"
    logic: str = ""                                  # all / any
    children: list["Condition"] = dc_field(default_factory=list)


@dataclass(frozen=True)
class AlertConfig:
    id: str
    level: str
    when: Condition
    message: str
    suggestion: str = ""


@dataclass(frozen=True)
class OpportunityConfig:
    id: str
    when: Condition
    suggestion: str


@dataclass(frozen=True)
class ScoringConfig:
    version: str
    updated_at: str
    description: str
    levels: list[LevelConfig]
    dimensions: list[DimensionConfig]
    alerts: list[AlertConfig]
    opportunities: list[OpportunityConfig]
    source_path: str = ""

    @property
    def enabled_dimensions(self) -> list[DimensionConfig]:
        return [d for d in self.dimensions if d.enabled]

    @property
    def total_max_score(self) -> float:
        return sum(d.max_score for d in self.enabled_dimensions)

    def find_factor(self, field_name: str) -> FactorConfig | None:
        """按字段名查找可编辑的因子定义（同一字段被多维度引用时取第一个可编辑的）。"""
        fallback: FactorConfig | None = None
        for dim in self.dimensions:
            for factor in dim.factors:
                if factor.field != field_name:
                    continue
                if factor.input.editable:
                    return factor
                fallback = fallback or factor
        return fallback


# ──────────────────────────── 解析 ────────────────────────────


def _require(node: Any, key: str, ctx: str) -> Any:
    if not isinstance(node, dict) or key not in node:
        raise ScoringConfigError(f"{ctx} 缺少必填字段 `{key}`")
    return node[key]


def _parse_input(raw: Any, ctx: str) -> InputSpec:
    if raw is None:
        return InputSpec()
    if not isinstance(raw, dict):
        raise ScoringConfigError(f"{ctx} 的 `input` 必须是对象")
    options = raw.get("options") or []
    if not isinstance(options, list):
        raise ScoringConfigError(f"{ctx} 的 `input.options` 必须是数组")
    return InputSpec(
        type=str(raw.get("type", "text")),
        options=[str(o) for o in options],
        min=raw.get("min"),
        max=raw.get("max"),
        step=raw.get("step"),
        unit=str(raw.get("unit", "")),
        placeholder=str(raw.get("placeholder", "")),
    )


_COMPARATOR_KEYS = ("gte", "gt", "lte", "lt", "eq")


def _sort_brackets(brackets: list[dict]) -> list[dict]:
    """规范化 threshold / days_since 的分档顺序，消除「命中即止」对书写顺序的依赖。

    规则求值按数组顺序匹配、命中即止，书写顺序错误会让低阈值档遮蔽高阈值档。
    加载期统一排序：
    - 全部为 gte/gt 单一比较符 → 按阈值降序（高阈值先命中）
    - 全部为 lte/lt 单一比较符 → 按阈值升序（低阈值先命中）
    - 其他情况（混合比较符 / 区间档 / eq）保持书写顺序，由配置作者保证顺序
    """
    def single_key(b: dict) -> str | None:
        keys = [k for k in _COMPARATOR_KEYS if k in b]
        return keys[0] if len(keys) == 1 else None

    keys = [single_key(b) for b in brackets]
    if not all(keys):
        return brackets

    def threshold_of(b: dict, key: str) -> float:
        try:
            return float(b[key])
        except (TypeError, ValueError):
            return 0.0

    if all(k in ("gte", "gt") for k in keys):
        return sorted(brackets, key=lambda b: threshold_of(b, single_key(b)), reverse=True)
    if all(k in ("lte", "lt") for k in keys):
        return sorted(brackets, key=lambda b: threshold_of(b, single_key(b)))
    return brackets


def _parse_rule(raw: Any, ctx: str) -> RuleConfig:
    if raw is None:
        return RuleConfig("constant", {"score": 0})
    if not isinstance(raw, dict):
        raise ScoringConfigError(f"{ctx} 的 `rule` 必须是对象")

    rule_type = str(_require(raw, "type", f"{ctx} 的 rule"))
    if rule_type not in VALID_RULE_TYPES:
        raise ScoringConfigError(
            f"{ctx} 的 rule.type=`{rule_type}` 不受支持，可用：{sorted(VALID_RULE_TYPES)}"
        )

    params = {k: v for k, v in raw.items() if k != "type"}

    if rule_type in ("threshold", "days_since"):
        brackets = params.get("brackets")
        if not isinstance(brackets, list) or not brackets:
            raise ScoringConfigError(f"{ctx} 的 rule.brackets 必须是非空数组")
        for b in brackets:
            if not isinstance(b, dict) or "score" not in b:
                raise ScoringConfigError(f"{ctx} 的 rule.brackets 每一项都需要 `score`")
        params["brackets"] = _sort_brackets(brackets)
    elif rule_type == "mapping":
        if not isinstance(params.get("map"), dict):
            raise ScoringConfigError(f"{ctx} 的 rule.map 必须是对象")
    elif rule_type == "penalty":
        when = params.get("when", "truthy")
        if when not in ("truthy", "falsy", "is_true", "is_false"):
            raise ScoringConfigError(
                f"{ctx} 的 rule.when=`{when}` 不受支持，可用：truthy / falsy / is_true / is_false"
            )

    return RuleConfig(rule_type, params)


def _parse_factor(raw: Any, ctx: str) -> FactorConfig:
    if not isinstance(raw, dict):
        raise ScoringConfigError(f"{ctx} 的因子必须是对象")

    field_name = str(_require(raw, "field", ctx))
    source = str(raw.get("source", "model"))
    if source not in VALID_SOURCES:
        raise ScoringConfigError(
            f"{ctx}.{field_name} 的 source=`{source}` 不受支持，可用：{sorted(VALID_SOURCES)}"
        )

    return FactorConfig(
        field=field_name,
        label=str(raw.get("label", field_name)),
        weight=float(raw.get("weight", 0) or 0),
        source=source,
        source_role=str(raw.get("source_role", "")),
        description=str(raw.get("description", "")),
        enabled=bool(raw.get("enabled", True)),
        input=_parse_input(raw.get("input"), f"{ctx}.{field_name}"),
        rule=_parse_rule(raw.get("rule"), f"{ctx}.{field_name}"),
    )


def _parse_dimension(raw: Any, index: int) -> DimensionConfig:
    if not isinstance(raw, dict):
        raise ScoringConfigError(f"dimensions[{index}] 必须是对象")

    key = str(raw.get("key") or f"dim_{index}")
    ctx = f"dimensions[{key}]"
    name = str(_require(raw, "name", ctx))

    clamp = raw.get("clamp") or {}
    if clamp and not isinstance(clamp, dict):
        raise ScoringConfigError(f"{ctx} 的 `clamp` 必须是对象")

    factors_raw = raw.get("factors") or []
    if not isinstance(factors_raw, list):
        raise ScoringConfigError(f"{ctx} 的 `factors` 必须是数组")

    return DimensionConfig(
        key=key,
        name=name,
        max_score=float(_require(raw, "max_score", ctx)),
        enabled=bool(raw.get("enabled", True)),
        description=str(raw.get("description", "")),
        base_score=float(raw.get("base_score", 0) or 0),
        base_detail=str(raw.get("base_detail", "")),
        clamp_min=clamp.get("min"),
        clamp_max=clamp.get("max"),
        factors=[_parse_factor(f, ctx) for f in factors_raw],
    )


def _parse_condition(raw: Any, ctx: str) -> Condition:
    if not isinstance(raw, dict):
        raise ScoringConfigError(f"{ctx} 的 `when` 必须是对象")

    for logic in ("all", "any"):
        if logic in raw:
            children_raw = raw[logic]
            if not isinstance(children_raw, list) or not children_raw:
                raise ScoringConfigError(f"{ctx} 的 `when.{logic}` 必须是非空数组")
            return Condition(
                logic=logic,
                children=[_parse_condition(c, f"{ctx}.{logic}") for c in children_raw],
            )

    op = str(_require(raw, "op", ctx))
    if op not in VALID_CONDITION_OPS:
        raise ScoringConfigError(
            f"{ctx} 的 op=`{op}` 不受支持，可用：{sorted(VALID_CONDITION_OPS)}"
        )
    return Condition(
        op=op,
        field=str(_require(raw, "field", ctx)),
        value=raw.get("value"),
        source=str(raw.get("source", "model")),
    )


def _parse_alert(raw: Any, index: int) -> AlertConfig:
    if not isinstance(raw, dict):
        raise ScoringConfigError(f"alerts[{index}] 必须是对象")

    alert_id = str(raw.get("id") or f"alert_{index}")
    ctx = f"alerts[{alert_id}]"
    level = str(raw.get("level", "medium"))
    if level not in VALID_ALERT_LEVELS:
        raise ScoringConfigError(
            f"{ctx} 的 level=`{level}` 不受支持，可用：{sorted(VALID_ALERT_LEVELS)}"
        )

    return AlertConfig(
        id=alert_id,
        level=level,
        when=_parse_condition(_require(raw, "when", ctx), ctx),
        message=str(_require(raw, "message", ctx)),
        suggestion=str(raw.get("suggestion", "")),
    )


def _parse_opportunity(raw: Any, index: int) -> OpportunityConfig:
    if not isinstance(raw, dict):
        raise ScoringConfigError(f"opportunities[{index}] 必须是对象")

    opp_id = str(raw.get("id") or f"opportunity_{index}")
    ctx = f"opportunities[{opp_id}]"
    return OpportunityConfig(
        id=opp_id,
        when=_parse_condition(_require(raw, "when", ctx), ctx),
        suggestion=str(_require(raw, "suggestion", ctx)),
    )


def parse_scoring_config(raw: Any, source_path: str = "") -> ScoringConfig:
    """把已解析的 YAML 字典转换为强类型配置对象，并做完整性校验。"""
    if not isinstance(raw, dict):
        raise ScoringConfigError("评分配置根节点必须是对象")

    dimensions_raw = raw.get("dimensions")
    if not isinstance(dimensions_raw, list) or not dimensions_raw:
        raise ScoringConfigError("评分配置缺少 `dimensions`（至少一个维度）")
    dimensions = [_parse_dimension(d, i) for i, d in enumerate(dimensions_raw)]

    seen_keys: set[str] = set()
    for dim in dimensions:
        if dim.key in seen_keys:
            raise ScoringConfigError(f"维度 key 重复：`{dim.key}`")
        seen_keys.add(dim.key)

    levels_raw = raw.get("levels")
    if not isinstance(levels_raw, list) or not levels_raw:
        raise ScoringConfigError("评分配置缺少 `levels`（至少一个等级）")
    levels = [
        LevelConfig(
            name=str(_require(lv, "name", f"levels[{i}]")),
            min_score=float(_require(lv, "min_score", f"levels[{i}]")),
            color=str(lv.get("color", "#64748b")),
        )
        for i, lv in enumerate(levels_raw)
    ]
    levels.sort(key=lambda lv: lv.min_score, reverse=True)

    alerts_raw = raw.get("alerts") or []
    if not isinstance(alerts_raw, list):
        raise ScoringConfigError("`alerts` 必须是数组")

    opportunities_raw = raw.get("opportunities") or []
    if not isinstance(opportunities_raw, list):
        raise ScoringConfigError("`opportunities` 必须是数组")

    return ScoringConfig(
        version=str(raw.get("version", "")),
        updated_at=str(raw.get("updated_at", "")),
        description=str(raw.get("description", "")),
        levels=levels,
        dimensions=dimensions,
        alerts=[_parse_alert(a, i) for i, a in enumerate(alerts_raw)],
        opportunities=[_parse_opportunity(o, i) for i, o in enumerate(opportunities_raw)],
        source_path=source_path,
    )


# ──────────────────────────── 加载与缓存 ────────────────────────────

_cache: dict[str, tuple[float, ScoringConfig]] = {}
_lock = threading.Lock()


def default_config_path() -> str:
    """默认配置路径：优先 config.SCORING_CONFIG_PATH，其次 backend/scoring_config.yaml。"""
    try:
        from config import SCORING_CONFIG_PATH  # 延迟导入，避免循环依赖

        if SCORING_CONFIG_PATH:
            return os.path.abspath(SCORING_CONFIG_PATH)
    except ImportError:  # pragma: no cover
        pass

    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(backend_dir, DEFAULT_CONFIG_FILENAME)


def load_scoring_config(path: str | None = None, *, force_reload: bool = False) -> ScoringConfig:
    """加载评分配置。

    按「路径 + mtime」缓存：配置文件被修改后自动重新加载，无需重启进程
    （SOW §11.1 要求「重启生效」，此处更进一步支持热更新）。
    """
    resolved = os.path.abspath(path or default_config_path())

    if not os.path.isfile(resolved):
        raise ScoringConfigError(
            f"评分配置文件不存在：{resolved}\n"
            "请确认 backend/scoring_config.yaml 未被删除，或通过环境变量 SCORING_CONFIG_PATH 指定路径。"
        )

    mtime = os.path.getmtime(resolved)
    cached = _cache.get(resolved)
    if cached and not force_reload and cached[0] == mtime:
        return cached[1]

    with _lock:
        cached = _cache.get(resolved)
        if cached and not force_reload and cached[0] == mtime:
            return cached[1]

        with open(resolved, "r", encoding="utf-8") as fp:
            try:
                raw = yaml.safe_load(fp)
            except yaml.YAMLError as exc:
                raise ScoringConfigError(f"评分配置 YAML 解析失败：{resolved}\n{exc}") from exc

        config = parse_scoring_config(raw, source_path=resolved)
        _cache[resolved] = (mtime, config)
        return config


def clear_config_cache() -> None:
    """清空缓存（测试用）。"""
    with _lock:
        _cache.clear()
