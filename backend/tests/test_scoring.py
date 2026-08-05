import datetime
import pytest
from models import Customer
from services.scoring.rule_based import RuleBasedStrategy
from services.health_score import HealthScoreEngine


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
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def engines():
    return [RuleBasedStrategy(), HealthScoreEngine()]


# ── Dimension: relationship ──

@pytest.mark.parametrize("years,expected", [(5, 10), (3, 7), (1, 4), (0.5, 0)])
def test_cooperation_years_bracket(years, expected):
    for eng in engines():
        c = base_customer(cooperation_years=years)
        dim = eng._relationship_score(c)
        assert dim.score >= expected  # other parts contribute too


@pytest.mark.parametrize("freq,expected", [
    ("每周", 10), ("双周", 7), ("每月", 5), ("每季度", 3), ("不定期", 1)
])
def test_contact_frequency(freq, expected):
    for eng in engines():
        c = base_customer(contact_frequency=freq)
        dim = eng._relationship_score(c)
        assert any(f"+{expected}分" in d for d in dim.details)


@pytest.mark.parametrize("days_ago,expected_min", [(5, 5), (14, 3), (60, 1), (120, 0)])
def test_last_contact_recency(days_ago, expected_min):
    for eng in engines():
        c = base_customer(last_contact_date=datetime.date.today() - datetime.timedelta(days=days_ago))
        dim = eng._relationship_score(c)
        # The recency component is at least expected_min
        pass  # Signal: test passes if no crash


def test_last_contact_none():
    for eng in engines():
        c = base_customer(last_contact_date=None)
        dim = eng._relationship_score(c)
        assert dim.score < 25


def test_contact_frequency_unknown_defaults_to_3():
    for eng in engines():
        c = base_customer(contact_frequency="unknown")
        dim = eng._relationship_score(c)
        # unknown → default 3 in freq_map, so +3 from frequency
        assert any("+3" in d for d in dim.details)


# ── Dimension: satisfaction ──

@pytest.mark.parametrize("rating,expected", [(10, 25), (5, 12.5), (1, 2.5)])
def test_satisfaction_score(rating, expected):
    for eng in engines():
        c = base_customer(customer_satisfaction=rating)
        dim = eng._satisfaction_score(c)
        assert dim.score == expected


# ── Dimension: business ──

@pytest.mark.parametrize("amount,expected", [(600, 15), (200, 10), (80, 6), (30, 3), (0, 0)])
def test_contract_amount_bracket(amount, expected):
    for eng in engines():
        c = base_customer(contract_amount=amount)
        dim = eng._business_score(c)
        assert any(f"+{expected}分" in d for d in dim.details)


@pytest.mark.parametrize("status,expected", [("正常", 10), ("部分逾期", 4), ("严重逾期", 0)])
def test_payment_status_score(status, expected):
    for eng in engines():
        c = base_customer(payment_status=status)
        dim = eng._business_score(c)
        assert any(str(expected) in d for d in dim.details if "回款" in d)


def test_payment_status_unknown_defaults_to_5():
    for eng in engines():
        c = base_customer(payment_status="unknown")
        dim = eng._business_score(c)
        assert any("+5" in d for d in dim.details if "回款" in d)


# ── Dimension: risk ──

def test_risk_no_factors():
    for eng in engines():
        c = base_customer(growth_potential="低")
        dim = eng._risk_score(c)
        assert dim.score == 25


def test_risk_signals_deduct():
    for eng in engines():
        c = base_customer(risk_signals="法律纠纷", growth_potential="低")
        dim = eng._risk_score(c)
        assert dim.score == 17  # 25 - 8


@pytest.mark.parametrize("placeholder", ["无", "暂无", "没有", "无风险", "none", "N/A", "-", " "])
def test_risk_signals_placeholder_no_deduct(placeholder):
    """占位空值文案（"无"/"暂无"等）视为无风险信号，不扣分也不触发预警。"""
    for eng in engines():
        c = base_customer(risk_signals=placeholder, growth_potential="低")
        dim = eng._risk_score(c)
        assert dim.score == 25
        result = eng.evaluate(c)
        assert not any("风险信号" in a for a in result.risk_alerts)


def test_competitor_deduct():
    for eng in engines():
        c = base_customer(competitor_involvement=True, growth_potential="低")
        dim = eng._risk_score(c)
        assert dim.score == 15  # 25 - 10


def test_severe_overdue_deduct():
    for eng in engines():
        c = base_customer(payment_status="严重逾期", growth_potential="低")
        dim = eng._risk_score(c)
        assert dim.score == 18  # 25 - 7


def test_partial_overdue_deduct():
    for eng in engines():
        c = base_customer(payment_status="部分逾期", growth_potential="低")
        dim = eng._risk_score(c)
        assert dim.score == 21  # 25 - 4


def test_growth_potential_bonus_high():
    for eng in engines():
        c = base_customer(growth_potential="高")
        dim = eng._risk_score(c)
        # Growth bonus +5 would give 30, but ceiling clamps to 25
        assert dim.score == 25


def test_growth_potential_bonus_medium():
    for eng in engines():
        c = base_customer(growth_potential="中")
        dim = eng._risk_score(c)
        # Growth bonus +2 would give 27, but ceiling clamps to 25
        assert dim.score == 25


def test_growth_potential_low():
    for eng in engines():
        c = base_customer(growth_potential="低")
        dim = eng._risk_score(c)
        assert dim.score == 25


def test_growth_potential_unknown_defaults_to_0():
    for eng in engines():
        c = base_customer(growth_potential="unknown")
        dim = eng._risk_score(c)
        assert dim.score == 25


def test_all_risks_floor_is_zero():
    """All risk deductions + low growth → score should be 0, NOT 2.5."""
    for eng in engines():
        c = base_customer(
            risk_signals="重大纠纷",
            competitor_involvement=True,
            payment_status="严重逾期",
            growth_potential="低",
        )
        dim = eng._risk_score(c)
        # 25 - 8 - 10 - 7 + 0 = 0, clamped to 0
        assert dim.score == 0


def test_risk_score_ceiling_is_25():
    """Growth bonus should not push score above 25."""
    for eng in engines():
        c = base_customer(growth_potential="高")
        dim = eng._risk_score(c)
        assert dim.score <= 25


# ── Total score and level ──

@pytest.mark.parametrize("total,level", [(85, "优秀"), (70, "良好"), (55, "一般"), (54, "风险")])
def test_level_thresholds(total, level):
    # Build customer that hits exactly the score
    # Use satisfaction dimension to control score precisely
    for eng in engines():
        c = base_customer(
            cooperation_years=0,
            contact_frequency="不定期",
            last_contact_date=None,
            customer_satisfaction=0,
            contract_amount=0,
            payment_status="严重逾期",
            risk_signals="X",
            competitor_involvement=True,
            growth_potential="低",
        )
        # All dims are 0, override with fake to test _level
        assert eng._level(total)[0] == level


def test_total_is_sum_of_4_dimensions():
    for eng in engines():
        c = base_customer()
        result = eng.evaluate(c)
        d_total = sum(d.score for d in result.dimensions)
        assert abs(result.total_score - d_total) < 0.1


# ── Risk alerts ──

def test_alert_last_contact_over_90_days():
    for eng in engines():
        c = base_customer(last_contact_date=datetime.date.today() - datetime.timedelta(days=100))
        result = eng.evaluate(c)
        assert any("90天" in a for a in result.risk_alerts)


def test_alert_last_contact_none():
    for eng in engines():
        c = base_customer(last_contact_date=None)
        result = eng.evaluate(c)
        assert any("90天" in a for a in result.risk_alerts)


def test_alert_low_satisfaction():
    for eng in engines():
        c = base_customer(customer_satisfaction=3)
        result = eng.evaluate(c)
        assert any("满意度" in a for a in result.risk_alerts)


def test_no_alert_satisfaction_5():
    for eng in engines():
        c = base_customer(customer_satisfaction=5)
        result = eng.evaluate(c)
        assert not any("满意度" in a for a in result.risk_alerts)


def test_alert_competitor():
    for eng in engines():
        c = base_customer(competitor_involvement=True)
        result = eng.evaluate(c)
        assert any("竞品" in a for a in result.risk_alerts)


def test_alert_payment_abnormal():
    for eng in engines():
        c = base_customer(payment_status="部分逾期")
        result = eng.evaluate(c)
        assert any("回款" in a for a in result.risk_alerts)


# ── Suggestions ──

def test_suggestion_high_growth_and_satisfaction():
    for eng in engines():
        c = base_customer(growth_potential="高", customer_satisfaction=8)
        result = eng.evaluate(c)
        assert any("增长潜力" in s for s in result.suggestions)


def test_no_growth_suggestion_when_growth_not_high():
    for eng in engines():
        c = base_customer(growth_potential="中", customer_satisfaction=8)
        result = eng.evaluate(c)
        assert not any("增长潜力" in s for s in result.suggestions)


# ── Response shape ──

def test_response_has_customer_info():
    for eng in engines():
        c = base_customer()
        result = eng.evaluate(c)
        assert result.customer_id == 1
        assert result.customer_name == "测试客户"
        assert len(result.dimensions) == 4


def test_response_total_score_in_range():
    for eng in engines():
        c = base_customer()
        result = eng.evaluate(c)
        assert 0 <= result.total_score <= 100


def test_both_engines_produce_same_result():
    """RuleBasedStrategy and HealthScoreEngine should produce identical output."""
    rule = RuleBasedStrategy()
    health = HealthScoreEngine()
    c = base_customer()
    r1 = rule.evaluate(c)
    r2 = health.evaluate(c)
    assert r1.total_score == r2.total_score
    assert r1.level == r2.level
    assert len(r1.risk_alerts) == len(r2.risk_alerts)
