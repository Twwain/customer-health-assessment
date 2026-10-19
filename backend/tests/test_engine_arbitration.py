"""引擎仅裁决完整覆盖的精确评分表达式，不替代语义与制度核对。"""
import json
from copy import deepcopy

import pytest

from services.ai.context_builder import ChatContext
from services.ai.fact_check import parse_result
from services.ai.graph_builder import AssessmentStrategyAgent
from services.ai.scoring_facts import build_scoring_facts, format_scoring_facts, project_scores
from services.ai.strategy import split_strategy_payload
from test_fact_check import ScriptedAdapter, _score_case, _with_targets


TARGETS = {'kcr_01': '100%', 'kcr_04': '≥3次', 'kcr_08': 'CXO级且能影响EB',
           'kcr_10b': '全流程且≥3节点可控'}


def card(claim, *, targets=None, action=None):
    return {'title': '改善关键关系', 'priority': 'recommended', 'urgency': 'high',
            'reason': '补齐已识别的关系缺口', 'action': _with_targets('') if action is None else action,
            'expected_outcome': claim, 'reference': '',
            'score_targets': dict(TARGETS) if targets is None else targets}


def draft_for(*items):
    return '```json\n' + json.dumps({'strategies': items}, ensure_ascii=False) + '\n```'


def dispute(claim, sources):
    return {'claim': claim, 'source_id': 'scoring', 'evidence': sources['scoring'],
            'reason': '该评分推算与所列因子收益不符，需按引擎重算。'}


@pytest.mark.parametrize('claim', [
    'KCR维度由24/30提升至29/30', 'KCR由24→29（+5.0）',
    '总分由78.9提升至83.9', '总增分5', '总增分5分', 'KCR维度增分5分',
])
def test_verified_exact_numeric_dispute_is_decided_by_engine(claim):
    c, facts, sources = _score_case()
    original = deepcopy(c.custom_fields)
    result = parse_result(json.dumps({'issues': [dispute(claim, sources)]}), draft_for(card(claim)),
                          sources, customer=c, scoring_facts=facts)
    assert result.status == 'passed' and not result.issues and not result.reason
    assert any(proof['claim'] == claim for proof in result.verified_projections)
    assert c.custom_fields == original


@pytest.mark.parametrize('claim', ['KCR由24→27', '总增分3', '总分由78.9提升至81.9'])
def test_wrong_arithmetic_is_rejected_even_without_model_issues(claim):
    c, facts, sources = _score_case()
    result = parse_result('{"issues":[]}', draft_for(card(claim)), sources, customer=c, scoring_facts=facts)
    assert result.status == 'issues'
    assert any(i['source_id'].startswith('projection:') for i in result.issues)
    assert result.verified_projections == []


@pytest.mark.parametrize('claim,suffix', [
    ('总增分5', '万分'), ('总增分5', '倍'), ('总增分5', ' 万分'),
    ('总增分5', '\n倍'), ('总增分5', '/10'), ('总增分5', '至8'),
    ('总增分5', '～8'), ('总增分5', 'e3'), ('总增分5', ',000分'),
    ('总增分5分', '倍'), ('总增分5分', '/年'), ('总增分5', '（万分）'),
    ('KCR由24→29', '万分'), ('KCR由24→29', '倍'), ('KCR由24→29', '～35'),
    ('KCR由24→29', '（+5万分）'), ('KCR由24→29（+5.0）', '倍'),
    ('KCR24/30→29/30', '万分'), ('KCR预计达到29分', '以内'),
    ('KCR提高5分', '至10分'), ('KCR维度 +5 分', '×100'),
    ('总增分5', '”万分'), ('总增分5', '】倍'),
])
def test_numeric_prefix_with_uncovered_suffix_never_provides_positive_proof(claim, suffix):
    c, facts, sources = _score_case()
    draft = draft_for(card(claim + suffix))
    for issues in ([], [dispute(claim, sources)]):
        result = parse_result(json.dumps({'issues': issues}), draft, sources, customer=c, scoring_facts=facts)
        assert result.status != 'passed' and (result.reason or result.issues)
        assert result.verified_projections == []


@pytest.mark.parametrize('claim,suffix', [
    ('总增分5', ''), ('总增分5分', '。'), ('KCR由24→29分', '，需跟踪落实。'),
    ('KCR由24→29（+5.0）', '；后续再评估。'), ('KCR24/30→29/30', '。'),
    ('KCR预计达到29分', '。'), ('KCR提高5分', '；另需关注业务进展。'),
    ('总增分5', '”。”'),
])
def test_supported_units_and_real_statement_boundaries_still_allow_arbitration(claim, suffix):
    c, facts, sources = _score_case()
    result = parse_result(json.dumps({'issues': [dispute(claim, sources)]}), draft_for(card(claim + suffix)),
                          sources, customer=c, scoring_facts=facts)
    assert result.status == 'passed' and not result.issues and not result.reason
    assert result.verified_projections


@pytest.mark.parametrize('targets,action', [
    ({}, ''), ({'kcr_01': '100%'}, _with_targets('')),
    ({**TARGETS, 'kcr_01': '非法档位'}, _with_targets('')),
    (TARGETS, ''), (TARGETS, _with_targets('').replace('提升到100%', '提升到60-79%')),
])
def test_incomplete_illegal_or_unanchored_targets_never_receive_arbitration(targets, action):
    c, facts, sources = _score_case()
    claim = 'KCR由24→29'
    result = parse_result(json.dumps({'issues': [dispute(claim, sources)]}),
                          draft_for(card(claim, targets=targets, action=action)), sources,
                          customer=c, scoring_facts=facts)
    assert result.status != 'passed' and result.reason and not result.verified_projections


@pytest.mark.parametrize('kind', ['whole_card', 'segment_id', 'mixed_claim', 'unsupported', 'semantic_prefix'])
def test_scoring_source_does_not_exempt_whole_cards_or_non_numeric_semantics(kind):
    c, facts, sources = _score_case()
    claim = 'KCR由24→29'
    if kind == 'mixed_claim':
        claim += '，按制度必须72小时核验'
    elif kind == 'unsupported':
        claim = '关键关系是唯一正确的业务优先级'
    elif kind == 'semantic_prefix':
        claim = 'KCR按制度不得由24→29'
    item = card(claim)
    issue = dispute(json.dumps(item, ensure_ascii=False) if kind == 'whole_card' else claim, sources)
    if kind == 'segment_id':
        issue = {'claim_id': '0', 'source_id': 'scoring', 'evidence_id': '0', 'reason': issue['reason']}
    result = parse_result(json.dumps({'issues': [issue]}), draft_for(item), sources,
                          customer=c, scoring_facts=facts)
    assert result.status == 'issues' and any(i.get('reason') == issue['reason'] for i in result.issues)


@pytest.mark.parametrize('invalid', ['evidence', 'source', 'customer'])
def test_arithmetic_proof_does_not_bypass_source_or_customer_validation(invalid):
    c, facts, sources = _score_case()
    claim = 'KCR由24→29'
    issue = dispute(claim, sources)
    if invalid == 'evidence':
        issue['evidence'] = '编造的引擎证据'
    elif invalid == 'source':
        issue['source_id'] = 'rag:unknown'
    else:
        c = None
    result = parse_result(json.dumps({'issues': [issue]}), draft_for(card(claim)), sources,
                          customer=c, scoring_facts=facts)
    assert result.status != 'passed' and result.reason


def test_policy_error_remains_after_exact_arithmetic_dispute_is_resolved():
    c, facts, sources = _score_case()
    claim = 'KCR由24→29'
    policy = '按制度72小时内完成单据核验。'
    sources['rag:1'] = '按制度36小时内完成单据核验。'
    policy_issue = {'claim': policy, 'source_id': 'rag:1', 'evidence': sources['rag:1'],
                    'reason': '核验动作时限应为36小时。'}
    result = parse_result(json.dumps({'issues': [dispute(claim, sources), policy_issue]}),
                          draft_for(card(claim, action=_with_targets('') + policy)), sources,
                          customer=c, scoring_facts=facts)
    assert result.status == 'issues' and result.issues == [policy_issue]
    assert result.verified_projections


@pytest.mark.parametrize('second', ['valid', 'missing', 'wrong', 'unrelated'])
def test_identical_expression_never_borrows_another_cards_target_evidence(second):
    c, facts, sources = _score_case()
    claim = 'KCR由24→29'
    other = card(claim)
    if second == 'missing':
        other.update(score_targets={}, action='客户经理先核实目标。')
    elif second == 'wrong':
        other.update(score_targets={'kcr_01': '100%'}, action='KCR-01由80-99%提升到100%。')
    elif second == 'unrelated':
        other.update(score_targets={'ci_05': '量化且客户认可'}, action='CI-05由仅定性提升到量化且客户认可。')
    result = parse_result(json.dumps({'issues': [dispute(claim, sources)]}),
                          draft_for(card(claim), other), sources, customer=c, scoring_facts=facts)
    if second == 'valid':
        assert result.status == 'passed'
    else:
        assert result.status != 'passed' and result.issues


def test_total_gain_uses_unrounded_engine_delta_and_total_endpoints_use_display_rounding():
    from models import Customer
    from seed_factors import RISKY_FACTORS

    c = Customer(custom_fields=dict(RISKY_FACTORS))
    changes = {'risk_06': '持平', 'risk_07': '是但可控', 'risk_08b': '较高(60-79%)'}
    projection = project_scores(c, changes)
    before, after = projection['before']['total_score'], projection['after']['total_score']
    gain = projection['delta']['total_score']
    assert gain == 3.36 and round(after - before, 2) != gain
    facts = build_scoring_facts(c)
    sources = {'scoring': format_scoring_facts(facts)}
    expressions = [f'总分由{before:g}提升至{after:g}', f'总增分{gain:g}']
    item = card('，'.join(expressions), targets=changes,
                action='RISK-06达到持平；RISK-07达到是但可控；RISK-08B达到较高(60-79%)。')
    result = parse_result(json.dumps({'issues': [dispute(e, sources) for e in expressions]}),
                          draft_for(item), sources, customer=c, scoring_facts=facts)
    assert result.status == 'passed' and not result.issues
    assert len(result.verified_projections) == 2


def test_agent_preserves_numeric_card_without_repairing_engine_confirmed_arithmetic():
    c, facts, sources = _score_case()
    claim = 'KCR由24→29'
    item = card(claim)
    adapter = ScriptedAdapter([draft_for(item), json.dumps({'issues': [dispute(claim, sources)]})])
    result = AssessmentStrategyAgent(adapter=adapter, tools_enabled=False, max_iterations=1).run(
        'strategy', ChatContext(customer=c, scoring_facts=facts), c, None, retrieve_enabled=False,
    )
    assert len(adapter.calls) == 2 and not result.degraded
    assert result.timings['refine_rounds'] == 0
    assert result.timings['verification'][-1]['engine_verified_projections']
    _, items = split_strategy_payload(result.text)
    assert items[0]['action'] == item['action'] and items[0]['title'] == item['title']
