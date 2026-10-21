"""每个启用因子独立预警，资料不完整不冒充已确认风险。"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from factories import MAX_FACTORS
from services.scoring.config_driven import ConfigDrivenStrategy
from services.scoring.config_loader import AlertConfig, Condition, load_scoring_config
from services.scoring.rules import evaluate_factor


CONFIG = load_scoring_config()
FACTORS = [f for d in CONFIG.enabled_dimensions for f in d.enabled_factors]


def customer():
    return SimpleNamespace(id=1, customer_name="因子覆盖测试", customer_satisfaction=90,
                           custom_fields=deepcopy(MAX_FACTORS))


def put(c, f, value):
    if f.source == "custom_fields":
        c.custom_fields[f.field] = value
    else:
        setattr(c, f.field, value)


def test_all_enabled_factors_have_explicit_low_score_rules():
    covered = {a.key for a in CONFIG.score_alerts
               if a.target == "factor" and a.op == "lte" and a.value == 4}
    assert covered == {f.field for f in FACTORS}
    assert len(covered) == 28


@pytest.mark.parametrize("factor", FACTORS, ids=lambda f: f.field)
def test_each_factor_low_score_is_independently_visible(factor):
    c = customer()
    options = factor.input.options
    values = (["[0,0,0,0,0]"] if factor.input.type == "key_person_levels" else options)
    low = min(values, key=lambda value: evaluate_factor(factor, value).score)
    assert evaluate_factor(factor, low).score <= 4
    put(c, factor, low)
    ids = {a.id for a in ConfigDrivenStrategy().evaluate(c).alerts}
    special = {"risk_08b": "ces_critical", "risk_08c": "customer_business_risk_critical"}
    assert factor.field + "_low" in ids or special.get(factor.field) in ids


@pytest.mark.parametrize("value", [None, "", "invalid99", True])
@pytest.mark.parametrize("field", ["risk_08b", "risk_08c", "kcr_01"])
def test_default_scores_create_data_gap_not_confirmed_factor_risk(field, value):
    c = customer()
    factor = next(f for f in FACTORS if f.field == field)
    put(c, factor, value)
    alerts = ConfigDrivenStrategy().evaluate(c).alerts
    ids = {a.id for a in alerts}
    assert field + "_data_gap" in ids
    assert field + "_low" not in ids
    assert "ces_critical" not in ids
    assert "customer_business_risk_critical" not in ids
    assert next(a for a in alerts if a.id == field + "_data_gap").level == "low"


def test_special_factor_alert_wins_over_generic_low_score():
    c = customer()
    c.custom_fields.update(kcr_02="[-1,0,0,0,0]", risk_08b="高(<60%)", risk_08c="危机")
    ids = {a.id for a in ConfigDrivenStrategy().evaluate(c).alerts}
    assert {"key_person_opposition", "ces_critical", "customer_business_risk_critical"} <= ids
    assert not {"kcr_02_low", "risk_08b_low", "risk_08c_low"} & ids


@pytest.mark.parametrize("value", ["[-1]", "[-1,0,0,0,99]", "[-1,0,0,0,true]",
                                  [-1, 0, 0, 0, True]])
def test_invalid_opposition_list_is_data_gap_without_confirmed_opposition(value):
    c = customer()
    c.custom_fields["kcr_02"] = value
    alerts = ConfigDrivenStrategy().evaluate(c).alerts
    ids = {a.id for a in alerts}
    assert "kcr_02_data_gap" in ids
    assert "key_person_opposition" not in ids
    assert "kcr_02_low" not in ids
    assert not any(a.level == "high" for a in alerts)


@pytest.mark.parametrize("logic", ["all", "any"])
def test_composite_raw_alert_checks_invalid_factor_beyond_first_leaf(logic):
    c = customer()
    c.custom_fields["kcr_02"] = "[-1]"
    rule = AlertConfig(id="composite", level="high", message="复合预警",
        when=Condition(logic=logic, children=[
            Condition(field="kcr_01", source="custom_fields", op="truthy"),
            Condition(field="kcr_02", source="custom_fields", op="contains_item", value=-1),
        ]))

    class CompositeStrategy(ConfigDrivenStrategy):
        @property
        def config(self):
            return replace(CONFIG, alerts=[rule])

    ids = {a.id for a in CompositeStrategy().evaluate(c).alerts}
    assert "composite" not in ids
    assert "kcr_02_data_gap" in ids
    c.custom_fields["kcr_02"] = "[-1,0,0,0,0]"
    assert "composite" in {a.id for a in CompositeStrategy().evaluate(c).alerts}


def test_four_triggers_and_next_legal_band_does_not():
    c = customer()
    c.custom_fields["or_02"] = "1次"
    assert "or_02_low" in {a.id for a in ConfigDrivenStrategy().evaluate(c).alerts}
    c.custom_fields["or_02"] = "2-3次"
    assert "or_02_low" not in {a.id for a in ConfigDrivenStrategy().evaluate(c).alerts}


def test_dimension_alert_with_missing_input_is_explicitly_uncertain():
    c = customer()
    for f in next(d for d in CONFIG.enabled_dimensions if d.key == "kcr").enabled_factors:
        put(c, f, None)
    alert = next(a for a in ConfigDrivenStrategy().evaluate(c).alerts if a.id == "kcr_dimension_low")
    assert "不能据此确认业务风险" in alert.message
    assert alert.level == "low"


def test_superseded_generic_suggestion_is_not_duplicated():
    c = customer()
    c.custom_fields["risk_08c"] = "危机"
    result = ConfigDrivenStrategy().evaluate(c)
    assert not any("针对RISK-08C" in s for s in result.suggestions)
    assert any("调整授信" in s for s in result.suggestions)


@pytest.mark.parametrize("field", ["kcr_01", "er_01", "er_08", "his_04"])
@pytest.mark.parametrize("value,valid", [(-1, False), (101, False), (0, True), (100, True),
                                       (0.5, True), ("50", True)])
def test_percentage_legacy_numeric_bounds(field, value, valid):
    c = customer()
    factor = next(f for f in FACTORS if f.field == field)
    put(c, factor, value)
    ids = {a.id for a in ConfigDrivenStrategy().evaluate(c).alerts}
    assert (field + "_data_gap" not in ids) is valid


@pytest.mark.parametrize("field", ["kcr_04", "er_07", "or_02", "or_06", "svc_05"])
@pytest.mark.parametrize("value,valid", [(-1, False), (0.5, False), (0, True), (100, True), ("2", True)])
def test_count_legacy_numeric_bounds_and_integer_steps(field, value, valid):
    c = customer()
    factor = next(f for f in FACTORS if f.field == field)
    put(c, factor, value)
    ids = {a.id for a in ConfigDrivenStrategy().evaluate(c).alerts}
    assert (field + "_data_gap" not in ids) is valid


@pytest.mark.parametrize("field", ["ci_01", "ci_03"])
@pytest.mark.parametrize("value,valid", [(-1, False), (4, False), (0.5, False),
                                       (0, True), (3, True), ("2", True)])
def test_completed_item_numeric_bounds(field, value, valid):
    c = customer()
    factor = next(f for f in FACTORS if f.field == field)
    put(c, factor, value)
    ids = {a.id for a in ConfigDrivenStrategy().evaluate(c).alerts}
    assert (field + "_data_gap" not in ids) is valid
