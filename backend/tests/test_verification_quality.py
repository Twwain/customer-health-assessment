"""质量失败不交付矛盾正文；片段协议与明确目标保持引擎权威。"""
import json

import pytest

from services.ai.fact_check import messages_for_check, parse_result
from services.ai.graph_builder import AssessmentStrategyAgent
from services.ai.context_builder import ChatContext
from services.ai.strategy import split_strategy_payload
from test_fact_check import ScriptedAdapter, _score_case, _with_targets


def test_segment_ids_resolve_original_text_and_reject_unknown_anchors():
    draft = '制度规定48小时核验。'
    sources = {'rag:1': '制度规定36小时核验。'}
    message = messages_for_check(draft, sources)[1].content
    assert 'draft_segments' in message and 'evidence_id' not in message
    payload = {'issues': [{'claim_id': '0', 'source_id': 'rag:1', 'evidence_id': '0', 'reason': '时限矛盾'}],
               'score_projections': []}
    result = parse_result(json.dumps(payload), draft, sources)
    assert result.status == 'issues'
    assert result.issues[0]['claim'] == draft and result.issues[0]['evidence'] == sources['rag:1']
    for field in ('claim_id', 'source_id', 'evidence_id'):
        invalid = json.loads(json.dumps(payload))
        invalid['issues'][0][field] = '不存在'
        assert parse_result(json.dumps(invalid), draft, sources).status == 'unverified'


def test_explicit_nonissues_do_not_trigger_revision_but_engine_errors_remain():
    c, facts, sources = _score_case()
    claim = 'SVC-05当前4分为最低档。'
    payload = {'issues': [{'claim': claim, 'source_id': 'scoring', 'evidence': sources['scoring'],
                          'reason': '经核对该项不构成矛盾。'}]}
    result = parse_result(json.dumps(payload), claim, sources, customer=c, scoring_facts=facts)
    assert result.status == 'issues'  # 模型自称“不报”不能覆盖硬校验
    assert any('高于配置最低分' in issue['reason'] for issue in result.issues)
    correct = '36小时核验。'
    payload['issues'][0].update(claim=correct, source_id='rag:1', evidence=correct)
    result = parse_result(json.dumps(payload), correct, {'rag:1': correct})
    assert result.status == 'passed' and not result.issues
    payload['issues'][0]['reason'] = '一项一致，但是另一个时限不一致。'
    assert parse_result(json.dumps(payload), correct, {'rag:1': correct}).status == 'issues'


def structured_strategy(changes=None, *, action=None, outcome='KCR由24→29（+5.0），总分由78.9提升至83.9。'):
    return {'priority': 'recommended', 'urgency': 'high', 'title': '完善关系', 'reason': '补齐目标',
            'action': action or _with_targets(''), 'expected_outcome': outcome, 'reference': '',
            'score_targets': changes if changes is not None else {
                'kcr_01': '100%', 'kcr_04': '≥3次', 'kcr_08': 'CXO级且能影响EB',
                'kcr_10b': '全流程且≥3节点可控',
            }}


def check_structured(item):
    c, facts, sources = _score_case()
    draft = '经营方向总览。\n```json\n' + json.dumps({'strategies': [item]}, ensure_ascii=False) + '\n```'
    result = parse_result('{"issues":[],"score_projections":[]}', draft, sources, customer=c, scoring_facts=facts)
    return result, draft


def test_structured_targets_do_not_depend_on_model_reextracting_them():
    result, draft = check_structured(structured_strategy())
    assert result.status == 'passed'
    _, items = split_strategy_payload(draft)
    assert 'score_targets' not in items[0]  # 不外露机器字段
    wrong, _ = check_structured(structured_strategy(outcome='KCR由24→27（+3.0），总分由78.9提升至81.9。'))
    assert wrong.status == 'issues'
    assert any('29' in issue['reason'] for issue in wrong.issues)


@pytest.mark.parametrize('changes', [
    {'kcr_01': '100%'}, {'kcr_01': '错误目标'}, {}, None,
    {'unknown': '100%'}, {'kcr_01': '100%', 'svc_05': '≥6次'},
])
def test_structured_incomplete_unknown_or_illegal_targets_never_pass(changes):
    item = structured_strategy()
    item['score_targets'] = changes
    result, _ = check_structured(item)
    assert result.status == 'unverified'


def test_conflicting_repeated_action_targets_are_not_accepted():
    item = structured_strategy(action=_with_targets('') + '同时KCR-01改为60-79%。')
    result, _ = check_structured(item)
    assert result.status == 'unverified'


def test_duplicate_target_keys_cannot_be_silently_overwritten():
    c, facts, sources = _score_case()
    draft = json.dumps(structured_strategy(), ensure_ascii=False)
    draft = draft.replace('"kcr_01": "100%"', '"kcr_01": "60-79%", "kcr_01": "100%"')
    result = parse_result('{"issues":[]}', draft, sources, customer=c, scoring_facts=facts)
    assert result.status == 'unverified'


@pytest.mark.parametrize('outcome,expected', [
    ('CI维度增分2.1，总分由78.9升至81.0。', 'passed'),
    ('CI维度由6.9升至9.0，总分由78.9升至81.0。', 'passed'),
    ('CI维度增分2.1，总分由78.9升至81.1。', 'issues'),
    ('CI维度由6.9升至9.1，总分由78.9升至81.0。', 'issues'),
])
def test_projection_is_bound_to_its_own_dimension_or_total(outcome, expected):
    c, facts, sources = _score_case()
    card = {'title': '补充价值量化', 'action': 'CI-05由仅定性升至量化且客户认可',
            'expected_outcome': outcome, 'score_targets': {'ci_05': '量化且客户认可'}}
    draft = '```json\n' + json.dumps({'strategies': [card]}, ensure_ascii=False) + '\n```'
    checked = parse_result('{"issues":[],"score_projections":[]}', draft, sources,
                           customer=c, scoring_facts=facts)
    assert checked.status == expected
    if expected == 'issues':
        assert checked.issues


def test_corner_quotes_in_legal_targets_keep_the_same_direction_and_value():
    item = structured_strategy(action=_with_targets('').replace('提升到100%', '提升到「100%」'))
    result, _ = check_structured(item)
    assert result.status == 'passed'
    item['action'] = item['action'].replace('提升到「100%」', '提升到「60-79%」')
    assert check_structured(item)[0].status == 'unverified'


def test_outbound_comparison_symbol_encoding_does_not_change_target_semantics():
    from services.ai.scoring_facts import project_scores
    c, facts, _ = _score_case()
    options = facts['factors']['svc_03']['options']
    target = next(option['value'] for option in options if '<' in option['value'] or '＜' in option['value'])
    encoded = target.replace('<', '＜').replace('>', '＞')
    assert project_scores(c, {'svc_03': encoded}) == project_scores(c, {'svc_03': target})
    with pytest.raises(ValueError):
        project_scores(c, {'svc_03': encoded.replace('＜', '＞')})


def test_lowest_group_wording_is_not_an_absolute_lowest_claim():
    c, facts, sources = _score_case()
    assert parse_result('{"issues":[]}', 'OR得分率70%，为七维度中最低之一。', sources,
                        customer=c, scoring_facts=facts).status == 'passed'
    assert parse_result('{"issues":[]}', 'OR得分率70%，为七维度中最低。', sources,
                        customer=c, scoring_facts=facts).status == 'issues'


def test_model_explicit_no_issue_conclusion_does_not_trigger_false_revision():
    text = '36小时内核验。'
    payload = {'issues': [{'claim_id': '0', 'source_id': 'rag:1', 'evidence_id': '0',
                          'reason': '两者一致。此项无误，不报。'}], 'score_projections': []}
    assert parse_result(json.dumps(payload), text, {'rag:1': text}).status == 'passed'


@pytest.mark.parametrize('reason', [
    '核验时限错误，应改为36小时，但建议中的15天不属于事实错误。',
    '核验应在36小时内完成。15天的建议不应报错。',
    '前一结论需改成36小时；本项不构成矛盾。',
    '核验时限存在冲突。另一部分两者一致。此项无误，不报。',
])
def test_mixed_reason_cannot_hide_a_real_contradiction(reason):
    draft = '制度规定48小时核验。建议15天完成客户访谈。'
    sources = {'rag:1': '制度规定36小时核验。'}
    payload = {'issues': [{'claim_id': '0', 'source_id': 'rag:1', 'evidence_id': '0', 'reason': reason}],
               'score_projections': []}
    result = parse_result(json.dumps(payload), draft, sources)
    assert result.status == 'issues' and result.issues[0]['reason'] == reason
    assert '48小时' in result.feedback() and '36小时' in result.feedback()


@pytest.mark.parametrize('draft,has_issue', [
    ('OR维度9.8/14，绝对缺口4.2，得分率70%，为七维度中第二低（最低为SVC的60%）。', False),
    ('SVC得分率60%，为所有维度最低。', False),
    ('OR得分率70%，SVC得分率60%为最低。', False),
    ('OR得分率70%，高于最低的SVC的60%。', False),
    ('OR维度得分率70%，但业务优先级为最低。', False),
    ('OR得分率70%，为所有维度最低。', True),
    ('各维度得分率最低为OR。', True),
    ('OR得分率70%，第二低（最低为CI的76.7%）。', True),
    ('OR得分率70%，为所有维度最低（最低为SVC的60%）。', True),
])
def test_lowest_rate_predicate_belongs_to_its_dimension(draft, has_issue):
    c, facts, sources = _score_case()
    result = parse_result('{"issues":[],"score_projections":[]}', draft, sources,
                          customer=c, scoring_facts=facts)
    assert result.status == ('issues' if has_issue else 'passed')


def test_correct_cross_dimension_ranking_does_not_trigger_quality_fallback():
    c, facts, _ = _score_case()
    draft = 'OR维度9.8/14，绝对缺口4.2，得分率70%，为七维度中第二低（最低为SVC的60%）。'
    adapter = ScriptedAdapter([draft, '{"issues":[],"score_projections":[]}'])
    result = AssessmentStrategyAgent(adapter=adapter, tools_enabled=False, max_iterations=1).run(
        'assessment', ChatContext(customer=c, scoring_facts=facts), c, None, retrieve_enabled=False,
    )
    assert result.text == draft and not result.degraded
    assert result.timings['verification'][-1]['status'] == 'passed'
    assert result.timings['refine_rounds'] == 0 and len(adapter.calls) == 2


@pytest.mark.parametrize('scenario', ['assessment', 'strategy', 'alert_analysis'])
def test_unresolved_issue_never_enters_final_answer_or_rule_items(scenario):
    c, facts, _ = _score_case()
    from services.ai.context_builder import _scoring_facts_section
    ctx = ChatContext(customer=c, scoring_facts=facts, customer_text=_scoring_facts_section(facts))
    adapter = ScriptedAdapter(['OR得分率70%，为所有维度最低。', '{"issues":[],"score_projections":[]}'])
    result = AssessmentStrategyAgent(adapter=adapter, tools_enabled=False, max_iterations=1).run(
        scenario, ctx, c, None, retrieve_enabled=False,
    )
    assert result.degraded and 'OR得分率70%' not in result.text
    assert result.timings['verification'][-1]['status'] == 'issues'
    assert result.timings['answer_source'] == 'rules_after_quality_check'
    assert 'AI 服务当前不可用' not in result.text
    assert all('分数回升' not in item['expected_outcome'] for item in result.degraded_items)
