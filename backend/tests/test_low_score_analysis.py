"""Low aggregate scores remain an AI analysis task without detailed alerts."""
import pytest

from schemas import AlertItem
from services.ai.context_builder import _assessment_section, build_alert_context
from services.ai.prompt_templates import get_template, get_internal_prompt
from services.scoring import load_scoring_config
from test_quality_recovery import _healthy_with_dimensions


@pytest.mark.parametrize('alerts', [[],
    [AlertItem(id='kcr_dimension_low', level='high', message='维度得分不足')],
    [AlertItem(id='kcr_01_data_gap', level='low', message='资料待核实')]])
def test_low_total_without_detail_requests_analysis_in_all_contexts(alerts):
    assessment = _healthy_with_dimensions(.5, .7)
    assessment.total_score = 49
    assessment.level = '风险'
    assessment.alerts = alerts
    for context in (_assessment_section(assessment), build_alert_context(assessment, None)):
        assert '低分分析状态' in context
        assert '没有因子明细业务预警' in context
        assert '原因尚待核实' in context


def test_detailed_business_alert_and_healthy_total_do_not_get_no_detail_marker():
    assessment = _healthy_with_dimensions(.5)
    assessment.total_score = 49
    rule = next(r for r in load_scoring_config().score_alerts if r.target == 'factor')
    assessment.alerts = [AlertItem(id=rule.id, level='high', message='真实明细预警')]
    assert '低分分析状态' not in _assessment_section(assessment)
    assessment.alerts = []
    assessment.total_score = load_scoring_config().levels[0].min_score
    assert '低分分析状态' not in build_alert_context(assessment, None)


@pytest.mark.parametrize('scenario', ['assessment', 'strategy', 'alert_analysis'])
def test_all_business_prompts_request_evidenced_hypotheses_without_fabrication(scenario):
    text = get_template(scenario).render_system(today='2026-10-21')
    assert '可能原因' in text
    assert '核实' in text
    assert '不编造' in text
    assert '没有因子明细业务预警' in text


def test_checker_and_selection_allow_hypotheses_but_keep_factual_premise_checks():
    checker = get_internal_prompt('fact_check')
    selector = get_internal_prompt('unit_review')
    assert '不能仅因没有明细预警或尚无确证因果而报错' in checker
    assert '当作已知事实使用的具体事件、数值和制度前提仍须有依据' in checker
    assert '不要求来源已证实所假设的因果关系' in selector
    assert '使用错误因子前提的假设仍须移除' in selector
