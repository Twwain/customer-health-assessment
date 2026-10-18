"""评分派生事实的行为回归；使用合成客户，不依赖私有快照或在线模型。"""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from models import Customer
from services.ai import scoring_facts
from services.ai.scoring_facts import build_scoring_facts, check_scoring_claims, format_scoring_facts, project_scores
from services.scoring.config_loader import load_scoring_config, parse_scoring_config


def customer():
    # 同构的评分输入，姓名、行业等不进入事实计算或外部服务。
    return Customer(id=999, customer_name="合成回归客户", custom_fields={
        "kcr_01": "80-99%", "kcr_02": "[1,2,1,2,3]", "kcr_04": "2次",
        "kcr_07": "支持≥60%且无反对", "kcr_08": "VP级", "kcr_10b": "1-2节点可控",
        "er_01": "80-99%", "er_05": "标杆且参与定标", "er_07": "≥4次", "er_08": "80-99%",
        "or_01": "项目级框架", "or_02": "2-3次", "or_06": "≥3项",
        "ci_01": "3项全部完成", "ci_03": "3项全部掌握", "ci_05": "仅定性",
        "his_02": "持平", "his_04": "≥50%", "his_06": "续约≥80%且增购", "his_09": "70-79",
        "risk_05": "无变动", "risk_06": "上升", "risk_07": "否",
        "risk_08b": "中(80-90%)", "risk_08c": "下行或承压",
        "svc_03": "<30min且>90%", "svc_04": "<8h且>80%", "svc_05": "2-3次",
    })


@pytest.fixture
def facts():
    return build_scoring_facts(customer())


def test_four_targets_use_engine_and_do_not_mutate_customer():
    c = customer()
    original = deepcopy(c.custom_fields)
    projection = project_scores(c, {
        "kcr_01": "100%", "kcr_04": "≥3次", "kcr_08": "CXO级且能影响EB",
        "kcr_10b": "全流程且≥3节点可控",
    })
    assert projection["before"]["total_score"] == 78.9
    assert projection["after"]["total_score"] == 83.9
    assert projection["delta"]["total_score"] == 5
    assert projection["delta"]["dimensions"]["kcr"] == 5
    assert next(d for d in projection["before"]["dimensions"] if d["key"] == "kcr")["score"] == 24
    assert next(d for d in projection["after"]["dimensions"] if d["key"] == "kcr")["score"] == 29
    assert c.custom_fields == original
    assert c.id == 999 and c.customer_name == "合成回归客户"
    json.dumps(projection, ensure_ascii=False, allow_nan=False)


def test_gaps_bands_and_weighted_single_factor_gains(facts):
    by_dim = {d["key"]: d for d in facts["dimensions"]}
    assert by_dim["or"]["gap"] == 4.2
    assert by_dim["kcr"]["gap"] == 6
    assert facts["gap_ranking"][0] == "kcr"
    assert facts["score_percent_ranking"][0] == "svc"
    svc = facts["factors"]["svc_05"]
    assert svc["current_score"] == 4 and svc["min_score"] == 1
    assert svc["lowest_options"] == ["0-1次"]
    gains = {gain["field"]: gain for gain in facts["single_factor_gain_ranking"]}
    assert gains["or_01"]["total_delta"] == 2.8
    assert gains["ci_05"]["total_delta"] == 2.1
    assert gains["or_01"]["total_delta"] > gains["ci_05"]["total_delta"]
    json.dumps(facts, ensure_ascii=False, allow_nan=False)


def test_person_options_are_not_full_distribution_targets(facts):
    person = facts["factors"]["kcr_02"]
    assert person["options"] == []
    assert person["person_levels"] == ["3", "2", "1", "0", "-1"]
    assert "单人等级" in person["option_semantics"]
    with pytest.raises(ValueError):
        project_scores(customer(), {"kcr_02": "3"})
    for target in ([3, 3, 3, 3, 4], [True, 3, 3, 3, 3], [3, 3], [3.0, 3, 3, 3, 3]):
        with pytest.raises(ValueError):
            project_scores(customer(), {"kcr_02": target})
    result = project_scores(customer(), {"kcr_02": [3, 3, 3, 3, 3]})
    assert result["delta"]["dimensions"]["kcr"] == 1
    assert customer().custom_fields["kcr_02"] == "[1,2,1,2,3]"


def test_source_supplies_engine_rankings_instead_of_model_recomputing_them(facts):
    summary = scoring_facts.ranking_summary(facts)
    assert "绝对缺口最大为KCR 关键客户关系（6分）" in summary
    assert "得分率最低为SVC 服务与支持健康度（60%）" in summary
    assert summary in format_scoring_facts(facts)
    assert "该排名不等于业务行动优先级" in format_scoring_facts(facts)


def test_engine_rankings_preserve_ties_and_exclude_undefined_percentages():
    facts = {"dimensions": [
        {"name": "自定义甲", "gap": 3, "score_percent": 60},
        {"name": "自定义乙", "gap": 3, "score_percent": 60},
        {"name": "未计比例", "gap": 0, "score_percent": None},
    ]}
    assert scoring_facts.ranking_summary(facts) == "绝对缺口最大为自定义甲、自定义乙（3分）；得分率最低为自定义甲、自定义乙（60%）"
    assert scoring_facts.ranking_summary({"dimensions": []}) == ""


@pytest.mark.parametrize("other_text", ["联系客户", "建议检查是否存在需求变化", "如果客户有时间则开展访谈"])
def test_compact_cards_do_not_share_ranking_predicates_or_negation(facts, other_text):
    from services.ai.local_quality import parse_answer
    items = [
        {"title": "组织绑定", "reason": "OR得分率70%，为所有维度最低", "action": other_text},
        {"title": "独立需求访谈", "reason": "了解客户需求", "action": "安排需求沟通"},
    ]
    draft = "```json\n" + json.dumps({"strategies": items}, ensure_ascii=False) + "\n```"
    issues = check_scoring_claims(draft, customer(), facts)
    assert len(issues) == 1 and "得分率并非" in issues[0]["reason"]
    assert issues[0]["claim"] in draft
    assert "独立需求访谈" not in issues[0]["claim"]
    assert parse_answer(draft).locate(issues) == {"u0"}


@pytest.mark.parametrize('text,expected_error', [
    ('OR组织客户关系得分率70%，绝对缺口4.2，其中OR-01升至集团级可增2.8分，为可枚举完整选项单因子中最大单项收益。', False),
    ('OR绝对缺口4.2分，单因子收益最大为OR-01。', False),
    ('OR绝对缺口4.2分，为各维度最大；OR-01升至集团级可增2.8分。', True),
    ('OR绝对缺口为各维度最大，因此建议提升OR-01。', True),
])
def test_absolute_gap_does_not_borrow_the_later_gain_maximum(facts, text, expected_error):
    issues = check_scoring_claims(text, customer(), facts)
    assert any('绝对缺口' in issue['reason'] for issue in issues) is expected_error


def sum_config():
    return parse_scoring_config({
        "version": "synthetic-sum", "levels": [{"name": "测试", "min_score": 0}],
        "dimensions": [{"key": "money", "name": "合成金额", "max_score": 10,
                        "aggregation": "sum", "base_score": 2, "clamp": {"min": 0, "max": 10},
                        "factors": [
                            {"field": "amount", "label": "金额", "source": "model",
                             "input": {"type": "select", "options": ["low", "high"]},
                             "rule": {"type": "mapping", "map": {"low": 3, "high": 20}}},
                            {"field": "extra", "label": "扩展", "source": "custom_fields",
                             "input": {"type": "select", "options": ["off", "on"]},
                             "rule": {"type": "mapping", "map": {"off": 0, "on": 3}}},
                            {"field": "disabled_factor", "label": "禁用因子", "enabled": False,
                             "rule": {"type": "constant", "score": 900}},
                        ]},
                       {"key": "disabled", "name": "禁用维度", "max_score": 90, "enabled": False,
                        "factors": [{"field": "hidden", "label": "禁用维度因子",
                                     "rule": {"type": "constant", "score": 90}}]}],
    })


def test_projection_honors_source_sum_clamp_and_disabled():
    c = SimpleNamespace(amount="low", custom_fields={"amount": "high", "extra": "off"})
    result = project_scores(c, {"amount": "high", "extra": "on"}, sum_config())
    assert result["before"]["total_score"] == 5
    assert result["after"]["total_score"] == 10
    assert result["delta"]["total_score"] == 5
    assert c.amount == "low" and c.custom_fields == {"amount": "high", "extra": "off"}
    for field in ("disabled_factor", "hidden", "unknown"):
        with pytest.raises(ValueError, match="未知或禁用"):
            project_scores(c, {field: "high"}, sum_config())
    facts = build_scoring_facts(c, sum_config())
    assert facts["max_score"] == 10
    assert set(facts["factors"]) == {"amount", "extra"}
    assert facts["factors"]["amount"]["options"][1]["dimension_delta"] == 5


def test_configuration_is_loaded_for_each_call(monkeypatch):
    default = load_scoring_config()
    monkeypatch.setattr(scoring_facts, "load_scoring_config", lambda: default)
    assert len(build_scoring_facts(customer())["dimensions"]) == 7
    monkeypatch.setattr(scoring_facts, "load_scoring_config", sum_config)
    assert build_scoring_facts(SimpleNamespace(amount="low", custom_fields={"extra": "off"}))["config_version"] == "synthetic-sum"


def test_unenumerated_inputs_do_not_invent_bands_or_gain_rankings():
    config = parse_scoring_config({
        "levels": [{"name": "测试", "min_score": 0}],
        "dimensions": [{"key": "test", "name": "测试", "max_score": 20, "factors": [
            {"field": "number", "label": "数字", "source": "custom_fields",
             "input": {"type": "number", "min": 0, "max": 5, "step": 1},
             "rule": {"type": "linear", "multiplier": 2}},
            {"field": "text", "label": "文本", "source": "custom_fields",
             "input": {"type": "text"}, "rule": {"type": "constant", "score": 1}},
            {"field": "fixed", "label": "只读", "source": "custom_fields",
             "input": {"type": "readonly", "options": ["fixed"]},
             "rule": {"type": "constant", "score": 1}},
        ]}],
    })
    c = SimpleNamespace(custom_fields={"number": 1, "text": "说明", "fixed": "fixed"})
    facts = build_scoring_facts(c, config)
    assert facts["single_factor_gain_ranking"] == []
    assert all(factor["min_score"] is None and factor["max_score"] is None for factor in facts["factors"].values())
    assert project_scores(c, {"number": 5}, config)["delta"]["total_score"] == 8
    for value in (6, -1, 1.5, True, "非法数字", float("inf")):
        with pytest.raises(ValueError):
            project_scores(c, {"number": value}, config)
    with pytest.raises(ValueError):
        project_scores(c, {"fixed": "fixed"}, config)


@pytest.mark.parametrize("changes", [{"svc_05": "100次"}, {"svc_05": None}, {"svc_05": True}])
def test_invalid_option_cannot_silently_receive_default_score(changes):
    with pytest.raises(ValueError):
        project_scores(customer(), changes)


def test_invalid_original_value_does_not_become_a_valid_lowest_band():
    c = customer()
    c.custom_fields["svc_05"] = "未提供"
    facts = build_scoring_facts(c)
    assert facts["factors"]["svc_05"]["current_score"] == 1
    assert facts["factors"]["svc_05"]["current_value_valid"] is False
    issues = check_scoring_claims("SVC-05 当前为最低档。", c, facts)
    assert issues and issues[0]["evidence"] in format_scoring_facts(facts)


@pytest.mark.parametrize("draft", [
    "OR 维度 9.8/14 为各维度中失分最多。",
    "OR维度绝对缺口为各维度中缺口最大。",
    "OR维度得分率为最低。",
    "SVC-05 主动服务频次「2-3次」（4分）为该项最低档位。",
    "SVC-05 当前处于最高档。",
    "SVC-05 目标提升至11分。",
    "CI-05 由3→10时带来+2.1，为单点提升最大项。",
    "KCR-02 单人等级提升至4。",
])
def test_false_claims_are_anchored_to_original_text_and_source(draft, facts):
    issues = check_scoring_claims(draft, customer(), facts)
    assert issues
    source = format_scoring_facts(facts)
    for issue in issues:
        assert issue["claim"] in draft
        assert issue["evidence"] in source
        assert issue["source_id"] == "scoring"


@pytest.mark.parametrize("draft", [
    "KCR为各维度中失分最多；SVC维度得分率为最低。",
    "SVC-05 当前为次低档，建议提升至最高档。",
    "SVC-05 当前4分，最低档为1分。",
    "优先建议处理CI-05，OR-01是第二优先级。",
    "CI-05 的原始映射分差最大，建议优先改进。",
    "CI-05 并非单点提升最大项。",
    "假设SVC-05为最低档，应先核实。",
    "原文误称OR为各维度中失分最多。",
    "KCR-02 单人等级提升至3。",
    "SVC-05 目标提升至最高档，预计单项总分贡献1分。",
    "SVC-05当前4分，目标升档后总分79.9分。",
    "KCR-04提升后总分增加0.5分，建议优先推进。",
    "OR缺口4.2低于KCR缺口6，后者为各维度中失分最多。",
    "SVC-05 主动服务频次“2-3次”当前4分，为次低档。",
])
def test_correct_statements_and_business_priority_do_not_raise(draft, facts):
    assert check_scoring_claims(draft, customer(), facts) == []


@pytest.mark.parametrize("description", [
    "为该因子最低档之上的一档", "处于最低档位之上", "是最低档以上一档", "属于最低档上一档",
    "为高于最低档的档位", "处于略高出最低档的档位",
    "为最高档之下的档位", "处于最高档位之下", "是最高档以下一档", "属于最高档下一档",
    "为低于最高档的档位", "处于略小于最高档的档位",
])
def test_relative_band_descriptions_are_not_extreme_band_assertions(description, facts):
    assert check_scoring_claims(f"CI-05当前映射分3，{description}。", customer(), facts) == []


@pytest.mark.parametrize("description", [
    "为该因子最低档", "处于最高档位", "为最低档上限",
    "为最低档之上的一档，同时是最低档", "为最高档之下的一档，同时属于最高档",
    "为高于最低档的档位，但处于最高档",
])
def test_relative_description_does_not_hide_a_separate_wrong_extreme_claim(description, facts):
    draft = f"CI-05当前映射分3，{description}。"
    issues = check_scoring_claims(draft, customer(), facts)
    assert issues and issues[0]["claim"] in draft
    assert issues[0]["evidence"] in format_scoring_facts(facts)


@pytest.mark.parametrize("scope", ["七个维度中", "7个维度中", "全部维度中", "各维度中", "所有启用维度中", "全体维度中"])
def test_percentage_ranking_accepts_natural_full_comparison_scopes(scope, facts):
    draft = f"OR维度得分率70%，为{scope}最低。"
    issues = check_scoring_claims(draft, customer(), facts)
    assert len(issues) == 1
    assert issues[0]["claim"] in draft
    assert "得分率" in issues[0]["reason"]
    assert issues[0]["evidence"] in format_scoring_facts(facts)
    assert check_scoring_claims(f"SVC维度得分率60%，为{scope}最低。", customer(), facts) == []


def test_conflicting_same_sentence_rates_are_checked_per_dimension(facts):
    draft = "OR维度得分率70%，为七个维度中最低，SVC维度得分率60%，同样是全部维度中最低。"
    issues = check_scoring_claims(draft, customer(), facts)
    assert len(issues) == 1
    assert "得分率" in issues[0]["reason"]
    assert issues[0]["claim"] in draft


@pytest.mark.parametrize("draft", [
    "组织客户关系得分率70%，为所有启用维度中最低。",
    "OR 组织客户关系得分率70%，为全体维度中最低。",
    "组织客户关系绝对缺口4.2分，为全部维度中最大。",
])
def test_dimension_names_are_read_from_configuration(draft, facts):
    assert check_scoring_claims(draft, customer(), facts)


@pytest.mark.parametrize("draft", [
    "OR与CI两个维度中，OR维度得分率70%最低。",
    "在OR、CI之间，OR得分率70%，为最低。",
    "所选维度中OR得分率最低。",
    "部分维度中OR缺口最大。",
    "OR-01因子得分率40%，为七个维度中最低。",
    "OR－01因子得分率40%，为七个维度中最低。",
    "OR维度得分率70%，但业务优先级为最低。",
    "OR维度原始映射分差最大，建议优先推进。",
    "OR维度得分率70%，并非七个维度中最低。",
    "假设OR维度得分率70%，为七个维度中最低。",
    "SVC 服务与支持健康度得分率60%，为七个维度中最低。",
])
def test_ranking_does_not_confuse_subsets_factors_or_priorities(draft, facts):
    assert check_scoring_claims(draft, customer(), facts) == []


def test_dimension_count_and_aliases_follow_a_new_configuration():
    config = parse_scoring_config({
        "levels": [{"name": "测试", "min_score": 0}],
        "dimensions": [
            {"key": f"dimension_{index}", "name": f"维度名称{index}", "max_score": 10,
             "factors": [{"field": f"factor_{index}", "label": "合成因子",
                          "rule": {"type": "constant", "score": index + 1}}]}
            for index in range(8)
        ],
    })
    c = SimpleNamespace(custom_fields={})
    facts = build_scoring_facts(c, config)
    assert check_scoring_claims("维度名称1得分率20%，为八个维度中最低。", c, facts)
    assert check_scoring_claims("dimension_1得分率20%，为8个维度中最低。", c, facts)
    assert check_scoring_claims("维度名称0得分率10%，为八个维度中最低。", c, facts) == []
    # 不把明确的七维子集偷换成新配置的全部八维。
    assert check_scoring_claims("维度名称1得分率20%，为七个维度中最低。", c, facts) == []


@pytest.mark.parametrize("draft,wrong", [
    ("绝对缺口最大在KCR（6分），得分率最低在SVC（60%）。", False),
    ("绝对缺口最大在KCR（6分），得分率最低位于SVC（60%）。", False),
    ("绝对缺口最大在KCR（6分），得分率最低的是SVC（60%）。", False),
    ("绝对缺口最大在KCR（6分），得分率最低为SVC（60%）。", False),
    ("OR得分率70%，第二低（最低在SVC的60%）。", False),
    ("各维度得分率最低在OR。", True),
    ("各维度得分率最低位于CI。", True),
    ("各维度得分率最低的是OR。", True),
    ("OR得分率70%，为所有维度最低。", True),
    ("OR得分率70%，但业务优先级最低在SVC。", False),
])
def test_lowest_rate_postposed_subject_prepositions_keep_correct_dimension(draft, wrong, facts):
    issues = check_scoring_claims(draft, customer(), facts)
    assert bool(issues) is wrong
    assert all(issue["claim"] in draft and issue["evidence"] in format_scoring_facts(facts) for issue in issues)
