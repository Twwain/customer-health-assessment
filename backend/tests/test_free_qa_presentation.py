"""Intent-focused answers, source presentation and message scenario metadata."""
import json
from types import SimpleNamespace

import pytest

from models import AIAssessmentRun, ChatMessage, Customer
from services.ai.catalog import build_catalog
from services.ai.contracts import build_contract
from services.ai.facts import capture, capture_comparison
from services.ai.protocol import load_assets
from services.ai.renderer import render
from services.ai.repository import available_result, resolve_messages, chat_references
from services.ai.validator import validate
from test_ai_protocol import v2db, valid_plan
from test_ai_completion import fixture, intent


def task(v2db, kinds, **arguments):
    package, catalog, _, _, _ = fixture(v2db, source_count=0)
    intents = [intent(package, kind, **arguments.get(kind, {})) for kind in kinds]
    contract = build_contract(package, catalog, 'free_qa', intents=intents)
    return package, catalog, contract, valid_plan(package, catalog, contract)


def test_one_action_only_shows_its_reason_and_one_table_item(v2db):
    package, catalog, contract, plan = task(v2db, ['actions'], actions={'count': 1})
    plan['analysis']['summary'] = '不要展示的完整总体评估'
    plan['detail_refs'] = [{'section': 'trend', 'render_ref': 'r:trend'}]
    result = render(plan, package, catalog, contract)
    assert result['content'] == ':::ai-text ' + json.dumps('• ' + plan['actions'][0]['reason'], ensure_ascii=False)
    assert len(result['strategy_items']) == 1
    assert result['assessment'] == package['assessment']
    assert all(word not in result['content'] for word in ('总览', '完整预警', '总体评估', 'AI 分析', '历史窗口'))


@pytest.mark.parametrize('kind', ['score_grade', 'trend', 'risk', 'analysis', 'verify_questions'])
def test_single_intent_only_displays_requested_answer(v2db, kind):
    package, catalog, contract, plan = task(v2db, [kind], verify_questions={'count': 2})
    plan['analysis']['summary'] = '直接回答当前问题'
    result = render(plan, package, catalog, contract)
    text = result['content']
    assert not any(title in text for title in ('### ', '本次按以下任务回答', 'AI 分析'))
    if kind == 'score_grade':
        assert text == '- ' + catalog['render_nodes']['r:score']['label']
    elif kind == 'trend':
        assert text == '- ' + catalog['render_nodes']['r:trend']['label']
    elif kind == 'risk':
        assert not plan['risks']
        assert text == ':::ai-text "• 直接回答当前问题"'
    elif kind == 'analysis':
        assert text == ':::ai-text "• 直接回答当前问题"'
    else:
        assert text.count(':::ai-text ') == 2
        assert '直接回答当前问题' not in text


@pytest.mark.parametrize('kind', ['explain_score', 'data_quality'])
def test_field_answers_preserve_raw_value_and_related_people_counts(v2db, kind):
    db, customer, _ = v2db
    customer.custom_fields = {'kcr_02': [0, 0, 1, 2, -1], 'kcr_01': '100%'}
    db.commit()
    package, catalog, contract, plan = task(v2db, [kind], **{kind: {'field_refs': ['field:kcr_02']}})
    result = render(plan, package, catalog, contract)
    assert catalog['render_nodes']['r:kcr_02']['label'] in result['content']
    counts = [n for n in catalog['render_nodes'].values() if n['template_id'] == 'template:people_counts']
    assert counts and counts[0]['label'] in result['content']
    assert catalog['render_nodes']['r:kcr_01']['label'] not in result['content']
    assert result['content'].count('引擎实际总分') == int(kind == 'explain_score')


def test_multiple_intents_keep_facts_and_actions_without_repeated_score(v2db):
    package, catalog, contract, plan = task(v2db, ['score_grade', 'explain_score', 'actions'],
        explain_score={'field_refs': ['field:kcr_01']}, actions={'count': 2})
    result = render(plan, package, catalog, contract)
    assert result['content'].count(catalog['render_nodes']['r:score']['label']) == 1
    assert catalog['render_nodes']['r:kcr_01']['label'] in result['content']
    assert len(result['strategy_items']) == 2
    assert plan['actions'][0]['reason'] in result['content']


def test_policy_keeps_selected_quotes_but_hidden_optional_quotes_are_not_used(v2db):
    package, catalog, _, _, _ = fixture(v2db, source_count=2)
    policy = intent(package, 'policy', source_refs=['src:71'])
    contract = build_contract(package, catalog, 'free_qa', intents=[policy])
    plan = valid_plan(package, catalog, contract)
    plan['detail_refs'] = [{'section': 'policy', 'render_ref': 'r:evidence_status'},
                           {'section': 'policy', 'render_ref': 'r:knowledge71'}]
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    result = render(plan, package, catalog, contract)
    assert catalog['render_nodes']['r:knowledge71']['label'] in result['content']
    assert [r['chunk_id'] for r in result['references']] == [71]
    action_contract = build_contract(package, catalog, 'free_qa', intents=[intent(package, 'actions', count=1)])
    action_plan = valid_plan(package, catalog, action_contract)
    action_plan['detail_refs'] = plan['detail_refs']
    result = render(action_plan, package, catalog, action_contract)
    assert result['references'] == []
    assert not any(ref['used'] for ref in chat_references(SimpleNamespace(fact_package=package), result))
    action_plan['citation_refs'] = ['src:72']
    result = render(action_plan, package, catalog, action_contract)
    assert [r['chunk_id'] for r in result['references']] == [72]
    assert [ref['chunk_id'] for ref in chat_references(SimpleNamespace(fact_package=package), result) if ref['used']] == [72]


def test_projection_keeps_engine_result_and_comparison_keeps_both_customers(v2db):
    db, customer, _ = v2db
    customer.custom_fields = {'kcr_01': '100%'}
    other = Customer(customer_name='另一个合成客户', custom_fields={})
    db.add(other); db.commit()
    package = capture_comparison(db.get_bind(), customer.id, [other.id])
    catalog = build_catalog(package)
    compare = intent(package, 'compare', subject_refs=list(package['snapshot']['members']))
    contract = build_contract(package, catalog, 'free_qa', intents=[compare])
    plan = valid_plan(package, catalog, contract)
    plan['detail_refs'] = [{'section': 'comparison', 'render_ref': 'r:comparison'}]
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    text = render(plan, package, catalog, contract)['content']
    assert text == '- ' + catalog['render_nodes']['r:comparison']['label']
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package)
    option = next(o for o in catalog['projection_options'].values() if o['value'] != '100%')
    contract = build_contract(package, catalog, 'free_qa', intents=[intent(package, 'projection',
        field_refs=[option['field_ref']], option_refs=[option['id']])])
    plan = valid_plan(package, catalog, contract)
    plan['projections'] = [{'id': 'p:0', 'purpose': 'what_if', 'changes': [
        {'field_ref': option['field_ref'], 'option_ref': option['id']}]}]
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    text = render(plan, package, catalog, contract)['content']
    assert '引擎重评总分' in text and '不是现状或效果承诺' in text
    assert '###' not in text


def test_message_scenario_comes_from_run_and_content_is_unchanged(v2db):
    db, customer, session = v2db
    package, catalog, contract, plan = task(v2db, ['score_grade'])
    result = render(plan, package, catalog, contract)
    run = AIAssessmentRun(run_id='qa-presentation', customer_id=customer.id, session_id=session.id,
        scenario='free_qa', status='succeeded', payload_hash='synthetic', fact_package=package,
        request_contract=contract, validated_plan=plan, validated_result=result)
    db.add(run)
    message = ChatMessage(session_id=session.id, role='assistant', content='', ai_run_id=run.run_id,
        content_format='ai_v2_ref')
    db.add(message); db.commit()
    current = available_result(db, run, customer_id=customer.id, session_id=session.id)
    assert current == result
    assert resolve_messages(db, [message])[0]['scenario'] == 'free_qa'
    assert resolve_messages(db, [message], 'model_history')[0]['content'].endswith(current['content'])
    assert not db.is_modified(run)
    assert available_result(db, run, customer_id=customer.id + 1) is None
    run.fact_package = {**run.fact_package, 'evidence': [{'chunk_id': 999, 'document_id': 999, 'item_id': 999}]}
    db.commit()
    assert available_result(db, run, customer_id=customer.id) is None
    unavailable = resolve_messages(db, [message])[0]
    assert unavailable['scenario'] == 'free_qa'
    assert unavailable['availability'] == 'source_unavailable'
    assert unavailable['strategy_items'] == []


def test_free_qa_fallback_remains_explicitly_degraded(v2db):
    from services.ai.fallback import build_fallback
    db, customer, _ = v2db
    package = capture(db.get_bind(), customer.id)
    _, result, _ = build_fallback(package, 'free_qa')
    assert result['degraded'] and result['strategy_items']
    assert '未完成本次问答' in result['content'] and '总体评估' not in result['content']
    _, assessment, _ = build_fallback(package, 'assessment')
    assert assessment['degraded'] and '### 总览' in assessment['content']


@pytest.mark.parametrize('scenario', ['assessment', 'report'])
def test_comprehensive_outputs_keep_their_existing_sections(v2db, scenario):
    package, catalog, _, _, _ = fixture(v2db, source_count=0)
    contract = build_contract(package, catalog, scenario)
    result = render(valid_plan(package, catalog, contract), package, catalog, contract)
    assert '### 总览' in result['content'] and '### 总体评估' in result['content']
