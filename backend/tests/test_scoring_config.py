"""配置驱动评分引擎测试（SOW §3.0 M0 / §11.1 验收项）。

覆盖三件事：
1. 默认配置与 v1.0 硬编码算法**逐分逐字**一致（改造不改结果）
2. 改 `scoring_config.yaml` 即改算法 —— 不需要改任何代码
3. 通过 custom_fields 注册新因子即可参与计分
"""

import datetime
import textwrap

import pytest

from models import Customer
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
    c.last_contact_date = datetime.date.today() - datetime.timedelta(days=5)
    c.customer_satisfaction = 8
    c.contract_amount = 100
    c.payment_status = "正常"
    c.risk_signals = None
    c.competitor_involvement = False
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


# ── 1. 默认配置结构 ──────────────────────────────────────────────────────


def test_default_config_loads():
    config = load_scoring_config()
    assert config.version == "1.0"
    assert config.source_path.endswith("scoring_config.yaml")


def test_default_config_has_four_enabled_dimensions_totaling_100():
    config = load_scoring_config()
    keys = [d.key for d in config.enabled_dimensions]
    assert keys == ["relationship", "satisfaction", "business", "risk"]
    assert config.total_max_score == 100


def test_reserved_multi_role_dimension_is_registered_but_disabled():
    """SOW §3.0.2 多角色因子预留：已注册、不计分、不影响总分。"""
    config = load_scoring_config()
    reserved = next(d for d in config.dimensions if d.key == "multi_role")
    assert reserved.enabled is False
    assert reserved not in config.enabled_dimensions
    assert {f.source_role for f in reserved.factors} == {"研发", "服务", "HR"}
    assert all(f.source == "custom_fields" for f in reserved.factors)


def test_levels_sorted_desc_by_min_score():
    config = load_scoring_config()
    assert [lv.name for lv in config.levels] == ["优秀", "良好", "一般", "风险"]


def test_find_factor_prefers_editable_definition():
    """payment_status 同时被商业价值与风险维度引用，取可编辑的那份。"""
    config = load_scoring_config()
    factor = config.find_factor("payment_status")
    assert factor is not None
    assert factor.input.editable is True
    assert factor.input.options == ["正常", "部分逾期", "严重逾期"]


# ── 2. 与 v1.0 硬编码算法一致 ────────────────────────────────────────────


def test_dimension_scores_match_legacy_algorithm():
    result = HealthScoreEngine().evaluate(make_customer())
    scores = {d.key: d.score for d in result.dimensions}
    assert scores == {"relationship": 17, "satisfaction": 20.0, "business": 20, "risk": 25}
    assert result.total_score == 82
    assert result.level == "良好"
    assert result.level_color == "#3b82f6"
    assert result.max_score == 100


def test_detail_text_matches_legacy_algorithm():
    result = HealthScoreEngine().evaluate(make_customer())
    details = {d.key: d.details for d in result.dimensions}
    assert details["relationship"] == [
        "合作3年(3-5年)：+7分",
        "沟通频率「每月」：+5分",
        "最近联系5天前(≤7天)：+5分",
    ]
    assert details["satisfaction"] == ["客户满意度评分 8/10 × 2.5 = 20.0分"]
    assert details["business"] == [
        "合同金额100万元(100-500万)：+10分",
        "回款情况「正常」：+10分",
    ]
    assert details["risk"] == ["基础分25分，按风险项扣减", "增长潜力「中」：+2分"]


def test_risk_dimension_penalties_and_floor():
    c = make_customer(
        risk_signals="重大纠纷",
        competitor_involvement=True,
        payment_status="严重逾期",
        growth_potential="低",
    )
    dim = next(d for d in HealthScoreEngine().evaluate(c).dimensions if d.key == "risk")
    assert dim.score == 0  # 25 - 8 - 10 - 7 + 0，下限截断为 0
    assert dim.details == [
        "基础分25分，按风险项扣减",
        "存在风险信号：-8分",
        "竞品已介入：-10分",
        "严重逾期：-7分",
        "增长潜力「低」：+0分",
    ]


def test_normal_payment_adds_no_risk_detail():
    """omit_detail_on_default：回款正常时风险维度不追加噪音明细。"""
    dim = next(
        d for d in HealthScoreEngine().evaluate(make_customer()).dimensions if d.key == "risk"
    )
    assert not any("回款" in text for text in dim.details)


def test_no_contact_record_uses_empty_branch():
    c = make_customer(last_contact_date=None)
    dim = next(
        d for d in HealthScoreEngine().evaluate(c).dimensions if d.key == "relationship"
    )
    assert "无联系记录：+0分" in dim.details


def test_alerts_and_suggestions_order_matches_legacy():
    c = make_customer(
        last_contact_date=datetime.date.today() - datetime.timedelta(days=120),
        customer_satisfaction=3,
        competitor_involvement=True,
        payment_status="部分逾期",
        risk_signals="预算削减",
    )
    result = HealthScoreEngine().evaluate(c)
    assert result.risk_alerts == [
        "超过90天未联系客户，关系存在疏远风险",
        "客户满意度较低(3/10)，存在流失风险",
        "竞品已介入，客户存在被挖角风险",
        "回款状态异常：部分逾期",
        "存在风险信号：预算削减",
    ]
    assert result.suggestions[0] == "建议尽快安排客户拜访或沟通"


def test_alerts_carry_level_for_badge_color():
    """评审结论 Q5：high=红 / medium=黄 / low=蓝，级别由配置提供。"""
    c = make_customer(competitor_involvement=True, payment_status="部分逾期")
    alerts = {a.id: a.level for a in HealthScoreEngine().evaluate(c).alerts}
    assert alerts["competitor_involved"] == "high"
    assert alerts["payment_abnormal"] == "medium"


def test_opportunity_suggestion_appended_last():
    c = make_customer(growth_potential="高", customer_satisfaction=8)
    result = HealthScoreEngine().evaluate(c)
    assert result.suggestions[-1].startswith("该客户增长潜力高")


def test_config_version_returned_in_assessment():
    assert HealthScoreEngine().evaluate(make_customer()).config_version == "1.0"


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
    assert result.dimensions[0].details == ["满意度 8 × 10 = 80.0分"]


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
