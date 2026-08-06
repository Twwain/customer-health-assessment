"""配置驱动评分引擎端到端测试（7 维度 × 60 因子）。

覆盖：
1. 默认空因子客户的最低分口径（客观事实驱动，无记录即低分）
2. 60 因子全取最高档 → 总分 100 / 健康
3. 代表性因子分档规则、RISK 风险因子扣分
4. 自动预警触发、机会点建议
5. 两种引擎（RuleBasedStrategy / HealthScoreEngine）输出一致
"""

import datetime

import pytest

from models import Customer
from factories import MAX_FACTORS
from services.health_score import HealthScoreEngine
from services.scoring.rule_based import RuleBasedStrategy


def base_customer(**overrides):
    c = Customer()
    c.id = 1
    c.customer_name = "测试客户"
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

def engines():
    return [RuleBasedStrategy(), HealthScoreEngine()]


def _dim_score(result, key):
    return next(d for d in result.dimensions if d.key == key)


# ── 总分与等级 ─────────────────────────────────────────────────────────────


def test_empty_factors_score_minimum():
    for eng in engines():
        result = eng.evaluate(base_customer())
        assert result.total_score == pytest.approx(13.1)
        assert result.level == "高危"
        assert result.max_score == 100


def test_maxed_factors_score_100():
    for eng in engines():
        result = eng.evaluate(
            base_customer(custom_fields=MAX_FACTORS, customer_satisfaction=10)
        )
        assert result.total_score == pytest.approx(100.0)
        assert result.level == "健康"


def test_total_is_sum_of_7_dimensions():
    for eng in engines():
        result = eng.evaluate(base_customer())
        d_total = sum(d.score for d in result.dimensions)
        assert abs(result.total_score - d_total) < 0.1


def test_level_thresholds_come_from_config():
    """等级表来自配置：健康80 / 亚健康60 / 风险40 / 高危0。"""
    config = RuleBasedStrategy().config
    assert [lv.name for lv in config.levels] == ["健康", "亚健康", "风险", "高危"]
    assert [lv.min_score for lv in config.levels] == [80, 60, 40, 0]


# ── 分维度计分 ─────────────────────────────────────────────────────────────


def test_kcr_dimension_sums_to_30_at_max():
    c = base_customer(custom_fields={k: v for k, v in MAX_FACTORS.items() if k.startswith("kcr")})
    for eng in engines():
        dim = _dim_score(eng.evaluate(c), "kcr")
        assert dim.score == pytest.approx(30.0)
        assert dim.max_score == 30


def test_ci_dimension_is_zero_without_insight_data():
    """CI（新增维度）：不掌握客户战略/采购决策/价值量化 → 0 分。"""
    for eng in engines():
        dim = _dim_score(eng.evaluate(base_customer()), "ci")
        assert dim.score == 0
        assert dim.max_score == 9


def test_his_09_satisfaction_brackets():
    """HIS-09 满意度调研（模型列 1-10）：9/7/6 三档 + 默认低档。"""
    for eng in engines():
        for rating, tail in ((10, "+0.9分"), (8, "+0.63分"), (6, "+0.36分"), (3, "+0.09分")):
            dim = _dim_score(eng.evaluate(base_customer(customer_satisfaction=rating)), "his")
            assert any(d.startswith("客户满意度") and d.endswith(tail) for d in dim.details)


def test_risk_factors_worst_case_lowers_score():
    """RISK 风险信号因子全取最差档 → 维度分 12 → 1.08。"""
    c = base_customer(
        custom_fields={
            "risk_05": "≥2人变动",
            "risk_06": "下降≥20%",
            "risk_07": "是且进展顺利",
            "risk_08": "≥2次或重大投诉",
            "risk_08b": "高(<60%)",
            "risk_08c": "危机",
        }
    )
    for eng in engines():
        dim = _dim_score(eng.evaluate(c), "risk")
        assert dim.score == pytest.approx(1.08)


# ── 预警与建议 ─────────────────────────────────────────────────────────────


def test_ppt_alerts_trigger():
    """自动预警（字段级可落地部分）全部触发，级别正确。"""
    c = base_customer(
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
    for eng in engines():
        alerts = {a.id: a.level for a in eng.evaluate(c).alerts}
        assert alerts["ces_deterioration"] == "high"
        assert alerts["customer_business_crisis"] == "high"
        assert alerts["competitor_poc"] == "high"
        assert alerts["relationship_deterioration"] == "high"
        assert alerts["key_person_churn"] == "medium"
        assert alerts["interaction_decline"] == "medium"
        assert alerts["champion_missing"] == "medium"


def test_legacy_alerts_trigger():
    for eng in engines():
        c = base_customer(
            last_contact_date=datetime.date.today() - datetime.timedelta(days=100),
            customer_satisfaction=3,
            competitor_involvement=True,
            payment_status="部分逾期",
            risk_signals="预算削减",
        )
        result = eng.evaluate(c)
        ids = [a.id for a in result.alerts]
        assert ids == [
            "stale_contact",
            "competitor_involved",
            "payment_abnormal",
            "low_satisfaction",
            "risk_signal",
        ]
        assert result.suggestions[0] == "建议尽快安排客户拜访或沟通"


def test_no_alerts_on_healthy_customer():
    c = base_customer(custom_fields=MAX_FACTORS, customer_satisfaction=10)
    for eng in engines():
        result = eng.evaluate(c)
        assert result.risk_alerts == []


def test_opportunity_suggestions():
    c = base_customer(
        custom_fields={"his_04": "≥50%", "his_09b": "成长期", "kcr_07": "≥60%支持且无反对"},
        customer_satisfaction=8,
    )
    for eng in engines():
        result = eng.evaluate(c)
        assert any(s.startswith("该客户钱包份额高") for s in result.suggestions)
        assert any(s.startswith("客户处于成长期") for s in result.suggestions)


# ── 响应形状与引擎一致性 ─────────────────────────────────────────────────────


def test_response_has_seven_dimensions():
    for eng in engines():
        result = eng.evaluate(base_customer())
        assert result.customer_id == 1
        assert result.customer_name == "测试客户"
        assert [d.key for d in result.dimensions] == ["kcr", "er", "or", "ci", "his", "risk", "svc"]


def test_response_total_score_in_range():
    for eng in engines():
        result = eng.evaluate(base_customer())
        assert 0 <= result.total_score <= 100


def test_both_engines_produce_same_result():
    rule = RuleBasedStrategy()
    health = HealthScoreEngine()
    c = base_customer(custom_fields=MAX_FACTORS, customer_satisfaction=10)
    r1 = rule.evaluate(c)
    r2 = health.evaluate(c)
    assert r1.total_score == r2.total_score
    assert r1.level == r2.level
    assert len(r1.risk_alerts) == len(r2.risk_alerts)
    assert [d.score for d in r1.dimensions] == [d.score for d in r2.dimensions]
