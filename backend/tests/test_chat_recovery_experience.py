"""Short stable previews and bounded recovery across many verification rounds."""
import json

import pytest

from services.ai import chat_engine
from services.ai.context_builder import ChatContext
from services.ai.graph_builder import AgentResult, AssessmentStrategyAgent
from services.ai.local_quality import parse_answer
from test_chat import client, customer, db, engine, session_factory, _reset_adapters, fake_llm, _parse_sse
from test_fact_check import ScriptedAdapter
from test_local_quality import GOOD, SAFE, SOURCE, CLEAN, patch
from test_local_quality import BAD, ISSUE, review


def issue(text):
    return json.dumps({'issues': [{'claim': text, 'source_id': 'knowledge',
        'evidence': SOURCE, 'reason': '制度时限应为36小时'}]}, ensure_ascii=False)


def long_repair_run(count, *, max_iterations=16, finish=True):
    current = '制度规定72小时内核验。'
    replies = [current, issue(current)]
    for index in range(count):
        next_text = GOOD if finish and index == count - 1 else f'制度规定{73 + index}小时内核验。'
        replies += [patch(parse_answer(current), {'u0': {'text': next_text}}), CLEAN if next_text == GOOD else issue(next_text)]
        current = next_text
    if not finish:
        replies.append('{"units":[{"id":"u0","decision":"remove","depends_on":[],"sources":[]}]}')
    adapter = ScriptedAdapter(replies)
    agent = AssessmentStrategyAgent(adapter=adapter, max_iterations=max_iterations, tools_enabled=False)
    result = agent.run('assessment', ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False)
    return result, adapter


def test_can_repair_twelve_rounds_before_delivering_verified_answer():
    result, adapter = long_repair_run(12)
    assert not result.degraded and GOOD in result.text
    assert result.timings['refine_rounds'] == 12
    assert len(adapter.calls) == 26
    assert result.timings['verification'][-1]['status'] == 'passed'


@pytest.mark.parametrize('max_iterations, success', [(16, True), (4, False)])
def test_selected_answer_can_use_remaining_rounds_for_more_than_one_final_repair(max_iterations, success):
    from services.ai.llm_adapter import LLMUnavailableError
    later_bad = '制度规定75小时内核验。'
    intermediate = '制度规定74小时内核验。'
    draft = BAD + '\n\n' + later_bad + '\n\n' + SAFE
    selected = later_bad + '\n\n' + SAFE
    replies = [draft, ISSUE, LLMUnavailableError('修订暂不可用'), LLMUnavailableError('修订仍不可用'),
               review(parse_answer(draft), remove={'u0'}), issue(later_bad),
               patch(parse_answer(selected), {'u0': {'text': intermediate}}), issue(intermediate)]
    if success:
        replies += [patch(parse_answer(intermediate + '\n\n' + SAFE), {'u0': {'text': GOOD}}), CLEAN]
    adapter = ScriptedAdapter(replies)
    result = AssessmentStrategyAgent(adapter=adapter, max_iterations=max_iterations, tools_enabled=False).run(
        'assessment', ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False)
    assert result.degraded is not success
    assert result.timings['final_repairs'] == (2 if success else 1)
    assert result.iterations <= max_iterations
    assert len(adapter.calls) == len(replies)
    if success:
        assert GOOD in result.text and SAFE in result.text and BAD not in result.text
        assert result.timings['verification'][-1]['status'] == 'passed'
    else:
        assert intermediate not in result.text


def test_selected_answer_retries_checker_with_remaining_budget_without_rewriting_content():
    from services.ai.llm_adapter import LLMUnavailableError
    draft = BAD + '\n\n' + SAFE
    adapter = ScriptedAdapter([draft, ISSUE, LLMUnavailableError('修订暂不可用'),
        LLMUnavailableError('修订仍不可用'), review(parse_answer(draft), remove={'u0'}),
        'not-json', 'not-json', CLEAN])
    result = AssessmentStrategyAgent(adapter=adapter, max_iterations=16, tools_enabled=False).run(
        'assessment', ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False)
    assert not result.degraded and result.text == SAFE
    assert result.timings['final_repairs'] == 0
    assert result.timings['verification'][-1]['status'] == 'passed'
    assert len(adapter.calls) == 8


def test_recovery_has_a_hard_cap_and_never_accepts_remaining_errors():
    result, adapter = long_repair_run(20, max_iterations=99, finish=False)
    assert result.degraded and result.iterations == 21
    assert result.timings['refine_rounds'] == 20 and len(adapter.calls) == 43


def test_checker_failures_are_retried_without_rewriting_business_content():
    adapter = ScriptedAdapter([SAFE] + ['not-json'] * 8 + [CLEAN])
    result = AssessmentStrategyAgent(adapter=adapter, max_iterations=16, tools_enabled=False).run(
        'assessment', ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False)
    assert not result.degraded and result.text == SAFE
    assert result.timings['refine_rounds'] == 0
    assert len(result.timings['verification']) == 9
    assert [call['phase'] for call in result.timings['llm_calls']][1:] == ['verification'] * 9


def test_unchanged_patch_switches_to_selection_and_keeps_independent_analysis():
    draft = BAD + '\n\n' + SAFE
    answer = parse_answer(draft)
    adapter = ScriptedAdapter([draft, ISSUE, patch(answer, {'u0': {'text': BAD}}),
                               patch(answer, {'u0': {'text': BAD}}),
                               review(answer, remove={'u0'}), CLEAN])
    result = AssessmentStrategyAgent(adapter=adapter, max_iterations=16, tools_enabled=False).run(
        'assessment', ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False)
    assert not result.degraded and SAFE in result.text and BAD not in result.text
    assert result.timings['refine_rounds'] == 2
    assert any(item['operation'] == 'recovery_switch' for item in result.timings['local_repairs'])


def test_repeated_checker_contradiction_switches_to_independent_selection():
    contradiction = json.dumps({'issues': [{'claim': SAFE, 'source_id': 'knowledge',
        'evidence': SOURCE, 'reason': '该表述正确，不报告问题'}]}, ensure_ascii=False)
    answer = parse_answer(SAFE)
    adapter = ScriptedAdapter([SAFE] + [contradiction] * 4 + [review(answer), CLEAN])
    result = AssessmentStrategyAgent(adapter=adapter, max_iterations=16, tools_enabled=False).run(
        'assessment', ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False)
    assert not result.degraded and result.text == SAFE
    assert result.timings['refine_rounds'] == 0
    assert any(item['operation'] == 'recovery_switch' for item in result.timings['local_repairs'])
    recheck_messages = adapter.calls[3][0]
    assert 'disputed_issues' in recheck_messages[-1].content
    assert '先逐项重新判断' in recheck_messages[0].content


def test_cyclic_repairs_stop_with_the_last_checked_draft():
    draft = BAD + '\n\n' + SAFE
    second = '制度规定73小时内核验。\n\n' + SAFE
    first_answer, second_answer = parse_answer(draft), parse_answer(second)
    adapter = ScriptedAdapter([draft, ISSUE,
        patch(first_answer, {'u0': {'text': '制度规定73小时内核验。'}}), issue('制度规定73小时内核验。'),
        patch(second_answer, {'u0': {'text': BAD}}), review(second_answer, remove={'u0'}), CLEAN])
    result = AssessmentStrategyAgent(adapter=adapter, max_iterations=16, tools_enabled=False).run(
        'assessment', ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False)
    assert not result.degraded and result.text == SAFE
    assert result.timings['refine_rounds'] == 2
    assert len(adapter.calls) == 7
    assert any('循环正文' in item.get('reason', '') for item in result.timings['local_repairs'])


@pytest.mark.parametrize('stream', [False, True])
def test_preview_stays_short_and_final_answer_replaces_it_once(client, customer, db, fake_llm, monkeypatch, stream):
    draft = '这是尚在核对的初稿内容。' * 50
    corrected = '这是经过核实的最终分析。' * 20

    def generate(*args, **kwargs):
        send = kwargs['on_event']
        for start in range(0, len(draft), 11):
            send('delta', {'text': draft[start:start+11], 'phase': 'reason'})
        send('stage', {'stage': 'verification', 'message': '正在核对'})
        for _ in range(3):
            send('replace', {'text': '', 'reason': 'refine'})
            send('delta', {'text': '这段修订内容不应重复打字。' * 30, 'phase': 'refine'})
        return AgentResult(text=corrected, references=[], degraded=False, degraded_items=[],
                           warnings=[], iterations=4, model='test', timings={})

    monkeypatch.setattr(chat_engine, 'generate', generate)
    sid = client.post('/api/chat/sessions', json={'customer_id': customer.id}).json()['id']
    response = client.post(f'/api/chat/sessions/{sid}/messages',
        json={'scenario': 'assessment', 'content': '综合评估', 'stream': stream})
    assert response.status_code == 200
    if stream:
        events = _parse_sse(response.text)
        preview = ''.join(data['text'] for name, data in events if name == 'delta')
        assert 0 < len(preview) <= 48 and preview == draft[:len(preview)]
        replacements = [data for name, data in events if name == 'replace']
        assert len(replacements) == 1 and replacements[0]['reason'] == 'final_sync'
        final = events[-1][1]['message']
        assert replacements[0]['text'] == final['content']
    else:
        final = response.json()['message']
    assert corrected in final['content'] and '修订内容' not in final['content']
