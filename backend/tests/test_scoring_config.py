"""配置驱动评分引擎测试。

覆盖三件事：
1. 默认配置（7 维度 × 28 因子）
2. 改 `scoring_config.yaml` 即改算法 —— 不需要改任何代码
3. 通过 custom_fields 注册新因子即可参与计分
"""

import textwrap

import pytest

from models import Customer
from factories import MAX_FACTORS
from services.health_score import HealthScoreEngine
from services.scoring.config_driven import ConfigDrivenStrategy
from services.scoring.config_loader import (
    ScoringConfigError,
    clear_config_cache,
    load_scoring_config,
    parse_scoring_config,
)


def make_customer(**overrides) -> Customer:
    c = Customer()
    c.id = 1
    c.customer_name = "测试客户"
    c.industry = "政务"
    c.cooperation_years = 3
    c.contact_frequency = "每月"
    c.customer_satisfaction = 8
    c.contract_amount = 100
    c.growth_potential = "中"
    c.custom_fields = {}
    for k, v in overrides.items():
        setattr(c, k, v)
    return c

def write_config(tmp_path, body: str) -> str:
    path = tmp_path / "scoring_config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    clear_config_cache()
    return str(path)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_config_cache()
    yield
    clear_config_cache()


# ── 1. 默认配置结构（7 维度 × 28 因子）───────────────────────────────────────


def test_default_config_loads():
    config = load_scoring_config()
    assert config.version == "2026.08.24-v3"
    assert config.source_path.endswith("scoring_config.yaml")


def test_default_config_has_seven_enabled_dimensions_totaling_100():
    config = load_scoring_config()
    keys = [d.key for d in config.enabled_dimensions]
    assert keys == ["kcr", "er", "or", "ci", "his", "risk", "svc"]
    assert config.total_max_score == 100


def test_default_config_registers_28_equal_weight_factors():
    """V3.0 固定为 28 因子，维度内算术平均。"""
    config = load_scoring_config()
    assert [len(d.factors) for d in config.enabled_dimensions] == [6, 4, 3, 3, 4, 5, 3]
    assert sum(len(d.factors) for d in config.enabled_dimensions) == 28
    for dim in config.enabled_dimensions:
        assert dim.aggregation == "average"
        assert {f.weight for f in dim.factors} == {1}
        assert all(f.enabled for f in dim.factors)


def test_all_v3_factors_render_as_dropdowns():
    config = load_scoring_config()
    factors = [factor for dim in config.enabled_dimensions for factor in dim.factors]
    kcr_02 = config.find_factor("kcr_02")
    assert kcr_02.input.type == "key_person_levels"
    assert kcr_02.input.options == ["3", "2", "1", "0", "-1"]
    assert all(factor.input.type == "select" for factor in factors if factor.field != "kcr_02")
    assert all(factor.input.options for factor in factors)


def test_dropdown_options_match_scoring_bands_without_example_duplicates():
    from services.scoring.rules import evaluate_factor

    config = load_scoring_config()
    allowed_same_score = {"kcr_02", "kcr_07", "kcr_10b"}
    for dimension in config.enabled_dimensions:
        for factor in dimension.factors:
            if factor.input.type == "key_person_levels":
                continue
            by_score: dict[float, list[str]] = {}
            for option in factor.input.options:
                score = evaluate_factor(factor, option).score
                by_score.setdefault(score, []).append(option)
            duplicates = {score: values for score, values in by_score.items() if len(values) > 1}
            if duplicates:
                assert factor.field in allowed_same_score, (factor.field, duplicates)

            declared = {
                float(bracket["score"])
                for bracket in factor.rule.params.get("brackets", [])
                if "score" in bracket
            }
            declared.update(float(value) for value in (factor.rule.params.get("map") or {}).values())
            default = factor.rule.params.get("default", {}).get(
                "score", factor.rule.params.get("default_score")
            )
            if default is not None:
                declared.add(float(default))
            assert declared <= set(by_score), (factor.field, declared - set(by_score))


def test_factors_carry_role_frequency_and_example():
    config = load_scoring_config()
    roles = {f.source_role for d in config.dimensions for f in d.factors}
    assert {"AR", "SR", "FR", "AR+FR", "AR+SR", "生态/行业", "系统"} <= roles
    assert all(
        f.source == "custom_fields" and f.frequency and f.example
        for d in config.dimensions
        for f in d.factors
    )


def test_levels_sorted_desc_by_min_score():
    config = load_scoring_config()
    assert [lv.name for lv in config.levels] == ["健康", "亚健康", "风险", "高危"]
    assert [lv.min_score for lv in config.levels] == [80, 60, 40, 0]


def test_find_factor_prefers_editable_definition():
    config = load_scoring_config()
    factor = config.find_factor("his_09")
    assert factor is not None
    assert factor.input.editable is True
    assert factor.source == "custom_fields"
    assert config.find_factor("not_registered_field") is None


# ── 2. 因子库计分（原始分均值 × 维度权重）───────────────────────────────────


def test_empty_factors_score_minimum():
    """未填报因子按各因子最低档计分（客观事实驱动，无记录即低分）。"""
    result = HealthScoreEngine().evaluate(make_customer())
    scores = {d.key: d.score for d in result.dimensions}
    assert scores["kcr"] == pytest.approx(4.0)
    assert scores["er"] == pytest.approx(2.25)
    assert scores["or"] == pytest.approx(0.466667)
    assert scores["ci"] == 0
    assert scores["his"] == pytest.approx(1.2)
    assert scores["risk"] == pytest.approx(0.72)
    assert scores["svc"] == pytest.approx(1 / 6, abs=1e-3)
    assert result.total_score == pytest.approx(8.8, abs=1e-3)
    assert result.level == "高危"
    assert result.max_score == 100


def test_maxed_factors_score_100():
    """28 个因子全部取最高档 → 总分 100、等级健康。"""
    result = HealthScoreEngine().evaluate(
        make_customer(custom_fields=MAX_FACTORS, customer_satisfaction=10)
    )
    assert result.total_score == pytest.approx(100.0)
    assert result.level == "健康"
    assert result.level_color == "#22c55e"


def test_kcr_01_raw_percent_score():
    engine = HealthScoreEngine()
    cases = [("100%", "10分"), ("87.5%", "8分"), ("65%", "6分"), ("45%", "4分"), ("30%", "1分")]
    for value, tail in cases:
        dim = engine._dimension_score("kcr", make_customer(custom_fields={"kcr_01": value}))
        assert any(d.startswith("决策链识别") and d.endswith(tail) for d in dim.details)


def test_kcr_02_list_average_and_support_distribution():
    engine = HealthScoreEngine()
    customer = make_customer(custom_fields={
        "kcr_02": "[3,2,2,1,1]",
        "kcr_07": "支持62.5%且反对12.5%",
    })
    dim = engine._dimension_score("kcr", customer)
    scores = {factor.field: factor.score for factor in dim.factors}
    assert scores["kcr_02"] == 8
    assert scores["kcr_07"] == 8


def test_kcr_02_requires_exactly_five_valid_person_levels():
    from fastapi import HTTPException
    from routers.customers import _parse_key_person_levels

    factor = load_scoring_config().find_factor("kcr_02")
    assert _parse_key_person_levels(factor, [3, 2, 1, 0, -1]) == "[3,2,1,0,-1]"
    with pytest.raises(HTTPException, match="必须填写 5 位"):
        _parse_key_person_levels(factor, [3, 2, 1, 0])
    with pytest.raises(HTTPException, match="等级只能"):
        _parse_key_person_levels(factor, [3, 2, 1, 0, 4])


def test_risk_factors_worst_case_lower_score():
    """RISK 五因子最差档按算术平均折算为维度分。"""
    c = make_customer(
        custom_fields={
            "risk_05": "≥2人变动",
            "risk_06": "下降≥20%",
            "risk_07": "是且进展顺利",
            "risk_08b": "高(<60%)",
            "risk_08c": "危机",
        }
    )
    dim = next(d for d in HealthScoreEngine().evaluate(c).dimensions if d.key == "risk")
    assert dim.score == pytest.approx(0.72)


def test_v3_alerts_cover_raw_and_score_triggers():
    c = make_customer(
        custom_fields={
            "kcr_02": "[3,2,-1,1]",
            "risk_05": "≥2人变动",
            "risk_06": "下降≥20%",
            "risk_07": "是且进展顺利",
            "risk_08b": "高(<60%)",
            "risk_08c": "危机",
        }
    )
    alerts = {a.id: a.level for a in HealthScoreEngine().evaluate(c).alerts}
    assert alerts["key_person_opposition"] == "high"
    assert alerts["kcr_dimension_low"] == "high"
    assert alerts["risk_dimension_low"] == "high"
    assert alerts["ci_dimension_low"] == "high"
    assert alerts["ces_critical"] == "high"
    assert alerts["customer_business_risk_critical"] == "high"


def test_v3_trend_alert_matches_latest_docx_rule():
    config = load_scoring_config()
    assert len(config.trend_alerts) == 1
    rule = config.trend_alerts[0]
    assert rule.id == "consecutive_two_quarter_decline"
    assert rule.type == "consecutive_quarter_decline"
    assert rule.consecutive_quarters == 2
    assert rule.drop_gt == 10


def test_all_alerts_use_current_factor_config():
    """每条预警的叶子条件都必须引用当前启用因子，并使用其真实数据来源。"""
    config = load_scoring_config()
    enabled_factors = {
        factor.field: factor
        for dimension in config.enabled_dimensions
        for factor in dimension.enabled_factors
    }

    def leaves(condition):
        if condition.children:
            for child in condition.children:
                yield from leaves(child)
        else:
            yield condition

    for alert in config.alerts:
        for condition in leaves(alert.when):
            assert condition.field in enabled_factors, alert.id
            assert condition.source == enabled_factors[condition.field].source, alert.id


def test_config_version_returned_in_assessment():
    assert HealthScoreEngine().evaluate(make_customer()).config_version == "2026.08.24-v3"


# ── 3. 改配置即改算法（§11.1 首条验收）────────────────────────────────────

MINIMAL_CONFIG = """
    version: "test-1"
    levels:
      - {name: 通过, min_score: 60, color: "#22c55e"}
      - {name: 不通过, min_score: 0, color: "#ef4444"}
    dimensions:
      - key: satisfaction
        name: 客户满意度
        max_score: 100
        factors:
          - field: customer_satisfaction
            label: 满意度评分
            weight: 100
            input: {type: slider, min: 1, max: 10}
            rule:
              type: linear
              multiplier: 10
              detail: "满意度 {value} × 10 = {score}分"
    alerts: []
    opportunities: []
"""


def test_algorithm_changes_with_config_only(tmp_path):
    path = write_config(tmp_path, MINIMAL_CONFIG)
    result = ConfigDrivenStrategy(path).evaluate(make_customer(customer_satisfaction=8))
    assert result.total_score == 80
    assert result.max_score == 100
    assert result.level == "通过"
    assert [d.name for d in result.dimensions] == ["客户满意度"]
    assert result.dimensions[0].details == ["满意度 8 × 10 = 80分"]


def test_level_thresholds_come_from_config(tmp_path):
    path = write_config(tmp_path, MINIMAL_CONFIG)
    engine = ConfigDrivenStrategy(path)
    assert engine.evaluate(make_customer(customer_satisfaction=5)).level == "不通过"
    assert engine.evaluate(make_customer(customer_satisfaction=6)).level == "通过"


def test_custom_field_factor_participates_in_scoring(tmp_path):
    """新增因子三步走：custom_fields 存值 → 配置注册 → 引擎自动读取。"""
    path = write_config(
        tmp_path,
        """
        version: "test-2"
        levels:
          - {name: 达标, min_score: 0, color: "#22c55e"}
        dimensions:
          - key: multi_role
            name: 多角色反馈
            max_score: 20
            factors:
              - field: delivery_quality
                label: 交付质量
                weight: 20
                source: custom_fields
                source_role: 研发
                input: {type: select, options: [优, 良, 差]}
                rule:
                  type: mapping
                  map: {优: 20, 良: 10, 差: 0}
                  default_score: 0
                  detail: "交付质量「{value}」：+{score}分"
        alerts: []
        opportunities: []
        """,
    )
    engine = ConfigDrivenStrategy(path)
    assert engine.evaluate(make_customer(custom_fields={"delivery_quality": "优"})).total_score == 20
    assert engine.evaluate(make_customer(custom_fields={"delivery_quality": "良"})).total_score == 10
    assert engine.evaluate(make_customer(custom_fields={})).total_score == 0


def test_disabled_dimension_excluded_from_total(tmp_path):
    path = write_config(
        tmp_path,
        """
        version: "test-3"
        levels:
          - {name: 达标, min_score: 0, color: "#22c55e"}
        dimensions:
          - key: satisfaction
            name: 客户满意度
            max_score: 25
            factors:
              - field: customer_satisfaction
                label: 满意度评分
                weight: 25
                rule: {type: linear, multiplier: 2.5}
          - key: future
            name: 待启用维度
            max_score: 50
            enabled: false
            factors:
              - field: customer_satisfaction
                label: 占位
                weight: 50
                rule: {type: linear, multiplier: 5}
        alerts: []
        opportunities: []
        """,
    )
    result = ConfigDrivenStrategy(path).evaluate(make_customer(customer_satisfaction=8))
    assert result.total_score == 20
    assert result.max_score == 25
    assert len(result.dimensions) == 1


def test_config_file_change_is_picked_up(tmp_path):
    """mtime 变化即重新加载，无需重启进程。"""
    path = write_config(tmp_path, MINIMAL_CONFIG)
    engine = ConfigDrivenStrategy(path)
    assert engine.evaluate(make_customer(customer_satisfaction=8)).total_score == 80

    write_config(tmp_path, MINIMAL_CONFIG.replace("multiplier: 10", "multiplier: 5"))
    assert engine.evaluate(make_customer(customer_satisfaction=8)).total_score == 40


# ── 4. 配置校验 ──────────────────────────────────────────────────────────


def test_missing_dimensions_rejected():
    with pytest.raises(ScoringConfigError, match="dimensions"):
        parse_scoring_config({"version": "x", "levels": [{"name": "A", "min_score": 0}]})


def test_unknown_rule_type_rejected():
    with pytest.raises(ScoringConfigError, match="rule.type"):
        parse_scoring_config(
            {
                "levels": [{"name": "A", "min_score": 0}],
                "dimensions": [
                    {
                        "key": "d",
                        "name": "D",
                        "max_score": 10,
                        "factors": [{"field": "f", "rule": {"type": "magic"}}],
                    }
                ],
            }
        )


def test_duplicate_dimension_key_rejected():
    dim = {"key": "d", "name": "D", "max_score": 10, "factors": []}
    with pytest.raises(ScoringConfigError, match="重复"):
        parse_scoring_config(
            {"levels": [{"name": "A", "min_score": 0}], "dimensions": [dim, dict(dim)]}
        )


def test_unknown_condition_op_rejected():
    with pytest.raises(ScoringConfigError, match="op"):
        parse_scoring_config(
            {
                "levels": [{"name": "A", "min_score": 0}],
                "dimensions": [{"key": "d", "name": "D", "max_score": 1, "factors": []}],
                "alerts": [{"id": "a", "when": {"field": "x", "op": "wat"}, "message": "m"}],
            }
        )


def test_missing_config_file_raises_actionable_error():
    with pytest.raises(ScoringConfigError, match="不存在"):
        load_scoring_config("no/such/scoring_config.yaml")
