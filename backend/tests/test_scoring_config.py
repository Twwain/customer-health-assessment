"""配置驱动评分引擎测试。

覆盖三件事：
1. 默认配置（7 维度 × 60 因子）
2. 改 `scoring_config.yaml` 即改算法 —— 不需要改任何代码
3. 通过 custom_fields 注册新因子即可参与计分
"""

import datetime
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


# ── 1. 默认配置结构（7 维度 × 60 因子）───────────────────────────────────────


def test_default_config_loads():
    config = load_scoring_config()
    assert config.version == "2026.08"
    assert config.source_path.endswith("scoring_config.yaml")


def test_default_config_has_seven_enabled_dimensions_totaling_100():
    config = load_scoring_config()
    keys = [d.key for d in config.enabled_dimensions]
    assert keys == ["kcr", "er", "or", "ci", "his", "risk", "svc"]
    assert config.total_max_score == 100


def test_default_config_registers_60_factors_with_weights_summing_to_max():
    """7 维度 × 60 因子；因子权重按二级维度均权折算后等于维度满分。"""
    config = load_scoring_config()
    assert sum(len(d.factors) for d in config.enabled_dimensions) == 60
    for dim in config.enabled_dimensions:
        weight_sum = sum(f.weight for f in dim.factors)
        assert weight_sum == pytest.approx(dim.max_score, abs=1e-3), dim.key
        assert all(f.enabled for f in dim.factors)


def test_factors_carry_role_and_secondary_dimension_info():
    """方案 10.2 数据采集分工：AR/SR/FR/财务/市场/管理层/生态/系统多角色输入。"""
    config = load_scoring_config()
    roles = {f.source_role for d in config.dimensions for f in d.factors}
    assert {"AR", "SR", "FR", "财务", "市场", "管理层", "生态/行业", "系统"} <= roles
    assert all(
        f.source in ("model", "custom_fields")
        for d in config.dimensions
        for f in d.factors
    )


def test_levels_sorted_desc_by_min_score():
    config = load_scoring_config()
    assert [lv.name for lv in config.levels] == ["健康", "亚健康", "风险", "高危"]
    assert [lv.min_score for lv in config.levels] == [80, 60, 40, 0]


def test_find_factor_prefers_editable_definition():
    """HIS-09 满意度调研复用模型列 customer_satisfaction，应可被表单找到。"""
    config = load_scoring_config()
    factor = config.find_factor("customer_satisfaction")
    assert factor is not None
    assert factor.input.editable is True
    assert factor.source == "model"
    assert config.find_factor("not_registered_field") is None


def test_dimension_factors_carry_parsed_sub_dimension():
    """评估结果的因子明细应携带从描述解析出的二级维度（供 PDF/前端分组）。"""
    strategy = ConfigDrivenStrategy()
    resp = strategy.evaluate(make_customer())
    kcr = next(d for d in resp.dimensions if d.key == "kcr")
    subs = {f.sub_dimension for f in kcr.factors}
    assert "决策链覆盖度" in subs


# ── 2. 因子库计分（四级计算模型折算结果）─────────────────────────────────────


def test_empty_factors_score_minimum():
    """未填报因子按各因子最低档计分（客观事实驱动，无记录即低分）。"""
    result = HealthScoreEngine().evaluate(make_customer())
    scores = {d.key: d.score for d in result.dimensions}
    assert scores["kcr"] == pytest.approx(3.27)
    assert scores["er"] == pytest.approx(2.22)
    assert scores["or"] == pytest.approx(0.233333)
    assert scores["ci"] == 0
    assert scores["his"] == pytest.approx(1.92)   # 含满意度 8 → 0.63
    assert scores["risk"] == pytest.approx(4.92)
    assert scores["svc"] == pytest.approx(0.5, abs=1e-3)
    assert result.total_score == pytest.approx(13.1, abs=1e-3)
    assert result.level == "高危"
    assert result.max_score == 100


def test_maxed_factors_score_100():
    """60 个因子全部取最高档 → 7 维度各自满分、总分 100、等级健康。"""
    result = HealthScoreEngine().evaluate(
        make_customer(custom_fields=MAX_FACTORS, customer_satisfaction=10)
    )
    assert result.total_score == pytest.approx(100.0)
    assert result.level == "健康"
    assert result.level_color == "#22c55e"


def test_kcr_01_options_score():
    """KCR-01 决策链识别完整度：下拉档位 100%/≥80%/≥60%/≥40%/<40% 对应分值。"""
    engine = HealthScoreEngine()
    cases = [("100%", "+2.1分"), ("≥80%", "+1.68分"), ("≥60%", "+1.26分"), ("≥40%", "+0.84分"), ("<40%", "+0.21分")]
    for value, tail in cases:
        dim = engine._dimension_score("kcr", make_customer(custom_fields={"kcr_01": value}))
        assert any(d.startswith("已识别占比") and d.endswith(tail) for d in dim.details)


def test_his_01_cumulative_amount_options():
    """HIS-01 累计合作金额：下拉档位 >5亿/1-5亿/5千万-1亿/1-5千万/<1千万 对应分值。"""
    engine = HealthScoreEngine()
    cases = [(">5亿", "+1.4分"), ("1-5亿", "+1.12分"), ("5千万-1亿", "+0.84分"), ("1-5千万", "+0.56分"), ("<1千万", "+0.28分")]
    for value, tail in cases:
        dim = engine._dimension_score("his", make_customer(custom_fields={"his_01": value}))
        assert any(d.startswith("累计合作金额") and d.endswith(tail) for d in dim.details)


def test_risk_factors_worst_case_lower_score():
    """RISK 风险信号因子全取最差档 → 维度分下降（12 满分 → 1.08）。"""
    c = make_customer(
        custom_fields={
            "risk_05": "≥2人变动",
            "risk_06": "下降≥20%",
            "risk_07": "是且进展顺利",
            "risk_08": "≥2次或重大投诉",
            "risk_08b": "高(<60%)",
            "risk_08c": "危机",
        }
    )
    dim = next(d for d in HealthScoreEngine().evaluate(c).dimensions if d.key == "risk")
    assert dim.score == pytest.approx(1.08)


def test_alerts_cover_ppt_triggers():
    """自动预警（字段级可落地部分）：CES/经营风险/竞品POC/客情恶化/关键人变动/互动下降/Champion缺失。"""
    c = make_customer(
        custom_fields={
            "kcr_08": "无",
            "risk_05": "≥2人变动",
            "risk_06": "下降≥20%",
            "risk_07": "是且进展顺利",
            "risk_08": "≥2次或重大投诉",
            "risk_08b": "高(<60%)",
            "risk_08c": "危机",
        }
    )
    alerts = {a.id: a.level for a in HealthScoreEngine().evaluate(c).alerts}
    assert alerts["ces_deterioration"] == "high"
    assert alerts["customer_business_crisis"] == "high"
    assert alerts["competitor_poc"] == "high"
    assert alerts["relationship_deterioration"] == "high"
    assert alerts["key_person_churn"] == "medium"
    assert alerts["interaction_decline"] == "medium"
    assert alerts["champion_missing"] == "medium"


def test_expanded_alerts_cover_all_dimensions():
    """高价值扩展预警覆盖 KCR/ER/OR/CI/HIS/RISK/SVC 七个维度。"""
    c = make_customer(
        custom_fields={
            "kcr_03": "<40%",
            "kcr_07": "<20%支持",
            "er_09": "0人",
            "or_02": "0次",
            "ci_03": "不了解",
            "his_07": ">90天",
            "his_09b": "衰退加速",
            "risk_02": "≥第4名",
            "svc_01": "0项达标",
            "svc_03": "未达标",
            "svc_06": "无回访",
        }
    )
    alerts = {a.id: a.level for a in HealthScoreEngine().evaluate(c).alerts}
    assert alerts == {
        "key_person_support_critical": "high",
        "executive_coverage_gap": "medium",
        "frontline_backup_missing": "medium",
        "executive_visit_missing": "medium",
        "decision_criteria_unknown": "medium",
        "payment_cycle_over_90": "high",
        "lifecycle_decline_accelerating": "high",
        "competitive_position_critical": "high",
        "delivery_quality_failure": "high",
        "service_sla_failure": "high",
        "customer_followup_missing": "medium",
    }


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


def test_removed_legacy_fields_do_not_trigger_alerts():
    """旧字段不再预警，但当前 HIS-09 满意度因子仍正常预警。"""
    c = make_customer(
        last_contact_date=datetime.date.today() - datetime.timedelta(days=120),
        customer_satisfaction=3,
        competitor_involvement=True,
        payment_status="部分逾期",
        risk_signals="预算削减",
    )
    result = HealthScoreEngine().evaluate(c)
    assert [a.id for a in result.alerts] == ["low_satisfaction"]


def test_opportunity_suggestion_appended_last():
    """机会点建议：钱包份额高+满意度好、成长期+Champion 支持。"""
    c = make_customer(
        custom_fields={"his_04": "≥50%", "his_09b": "成长期", "kcr_07": "≥60%支持且无反对"},
        customer_satisfaction=8,
    )
    result = HealthScoreEngine().evaluate(c)
    assert any(s.startswith("该客户钱包份额高") for s in result.suggestions)
    assert any(s.startswith("客户处于成长期") for s in result.suggestions)


def test_config_version_returned_in_assessment():
    assert HealthScoreEngine().evaluate(make_customer()).config_version == "2026.08"


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
