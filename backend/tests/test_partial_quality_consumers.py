"""局部核实结果跨真实HTTP/事务/报告边界保持一致；Agent结果为受控夹具。"""
import json
from dataclasses import replace

import pytest

from models import AssessmentHistory, ChatMessage
from services import assessment_history
from services.ai import chat_engine, llm_adapter, strategy
from services.ai.answer_presentation import HISTORY_NOTICE, present_history_answer, present_strategies
from services.ai import local_quality
from services.ai.graph_builder import AgentResult, AssessmentStrategyAgent
from services.pdf_report import PdfReportGenerator
from services.report_builder import build_report_data
from test_chat import (
    FakeAdapter, _parse_sse, _reset_adapters, client, customer, db, engine,
    fake_llm, session_factory,
)
from test_report import _collect_flow_text
from test_fact_check import ScriptedAdapter


NOTICE = "已移除部分依据不足的结论，以下保留已核实的分析与建议。"
SAFE_ITEM = {
    "priority": "recommended", "title": "安排需求访谈", "urgency": "medium",
    "reason": "建议进一步确认客户需求", "action": "客户经理15天内组织客户访谈",
    "expected_outcome": "记录客户反馈并更新客户画像", "reference": "",
    "score_targets": {},
}


def _verified_partial_result():
    return AgentResult(
        text="## 客户经营建议\n保留客户访谈这一独立行动。\n```json\n"
             + json.dumps({"strategies": [SAFE_ITEM]}, ensure_ascii=False) + "\n```",
        references=[], degraded=False, degraded_items=[], warnings=[NOTICE],
        iterations=2, model="controlled-partial-quality",
        timings={
            "answer_source": "ai_with_local_repairs",
            "verification": [{"status": "passed", "issues": [], "reason": ""}],
            "llm_calls": [{"phase": "verification", "finish_reason": "stop", "total_tokens": 200}],
        },
    )


@pytest.mark.parametrize("stream", [False, True])
def test_partial_success_replaces_old_answer_and_persists_only_final_cards(
    client, customer, db, fake_llm, monkeypatch, stream,
):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    old = client.post(f"/api/chat/sessions/{session_id}/messages",
                      json={"content": "初次分析", "stream": False}).json()["message"]
    snapshot = assessment_history.record_assessment(db, customer, strategy_snapshot=[{"title": "旧策略"}])

    def generate(*args, **kwargs):
        kwargs["on_event"]("delta", {"text": "错误草稿：组织客户关系得分率最低。"})
        return _verified_partial_result()

    monkeypatch.setattr(chat_engine, "generate", generate)
    response = client.post(f"/api/chat/sessions/{session_id}/regenerate",
                           json={"scenario": "strategy", "stream": stream})
    assert response.status_code == 200
    if stream:
        events = _parse_sse(response.text)
        assert events[-1][0] == "done"
        done = events[-1][1]
        replacements = [data["text"] for name, data in events if name == "replace" and data.get("reason") == "final_sync"]
        assert replacements and "错误草稿" not in replacements[-1]
    else:
        done = response.json()
    final = done["message"]
    assert final["degraded"] is False
    assert final["strategy_items"][0]["title"] == SAFE_ITEM["title"]
    assert "规则引擎" not in final["content"] and NOTICE in final["content"]
    assert "错误草稿" not in final["content"]
    assert present_history_answer(final["content"]) == final["content"]
    assert HISTORY_NOTICE not in final["content"]

    db.expire_all()
    assert db.get(ChatMessage, old["id"]).superseded_by_id == final["id"]
    assert db.get(AssessmentHistory, snapshot.id).strategy_snapshot == final["strategy_items"]
    history = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
    assert [m["id"] for m in history if m["role"] == "assistant"] == [final["id"]]

    next_adapter = FakeAdapter(chunks=["后续完整回答"])
    llm_adapter.set_chat_adapter(next_adapter)
    client.post(f"/api/chat/sessions/{session_id}/messages",
                json={"content": "继续交流", "scenario": "free_qa", "stream": False})
    sent_history = [m.content for m in next_adapter.calls[0] if m.role == "assistant"]
    # 当前会话上下文沿用正文契约；卡片已独立核验HTTP/快照，不把其存在误作正文要求。
    assert any("保留客户访谈这一独立行动" in content for content in sent_history)
    assert all("错误草稿" not in content for content in sent_history)


def test_report_and_pdf_preserve_verified_ai_cards_and_local_notice(db, customer, fake_llm, monkeypatch):
    monkeypatch.setattr(strategy, "generate", lambda *args, **kwargs: _verified_partial_result())
    report = build_report_data(db, customer, include_ai=True)
    assert report.has_ai and not report.degraded
    assert [item["title"] for item in report.strategy_items] == [SAFE_ITEM["title"]]
    assert report.warnings == [NOTICE]
    assert report.timings["answer_source"] == "ai_with_local_repairs"
    generator = PdfReportGenerator()
    text = _collect_flow_text(generator._ai_strategy_section(report.strategy_items, report.references))
    assert SAFE_ITEM["title"] in text and SAFE_ITEM["action"] in text
    assert "错误草稿" not in text
    pdf = generator.generate(report.assessment, strategy_items=report.strategy_items,
                             references=report.references, generation_warnings=report.warnings)
    assert pdf.startswith(b"%PDF")


@pytest.mark.parametrize('stream', [False, True])
def test_final_policy_patch_persists_only_verified_cards_and_updates_snapshot(
    client, customer, db, fake_llm, monkeypatch, stream,
):
    from test_chat import FakeAdapter

    source = '账款逾期后，财务负责人应在36小时内完成单据核验，在72小时内取得付款排期。'
    bad_action = '按制度72小时内取得客户书面付款排期。'
    fixed_action = '按制度72小时内取得付款排期；建议另行取得书面确认。'
    wrong = {**SAFE_ITEM, 'title': '回款制度动作', 'action': bad_action}
    fixed = {**wrong, 'action': fixed_action}
    draft = '```json\n' + json.dumps({'strategies': [wrong, SAFE_ITEM]}, ensure_ascii=False) + '\n```'
    answer = local_quality.parse_answer(draft)
    anchors = [{'source_id': 'knowledge', 'evidence': source}]
    review = json.dumps({'units': [{'id': u.id, 'decision': 'keep', 'depends_on': [], 'sources': anchors}
                                   for u in answer.units]})
    found = json.dumps({'issues': [{'claim': bad_action, 'source_id': 'knowledge', 'evidence': source,
                                   'reason': '将书面误作制度硬要求，应改为独立建议。'}]})
    patch = json.dumps({'dependencies': [{'id': u.id, 'depends_on': []} for u in answer.units],
                        'patches': [{'id': 'u0', 'item': fixed, 'sources': anchors}]})

    def generate(scenario, ctx, customer, db, **kwargs):
        adapter = ScriptedAdapter([draft, 'not-json', 'not-json', review, found, patch, '{"issues":[]}'])
        ctx = replace(ctx, knowledge_text=source, references=[])
        result = AssessmentStrategyAgent(adapter=adapter, max_iterations=1, tools_enabled=False,
                                         on_event=kwargs.get('on_event')).run(
            scenario, ctx, customer, db, retrieve_enabled=False)
        assert len(adapter.calls) == 7 and result.timings['final_repairs'] == 1
        assert result.timings['verification'][-1]['status'] == 'passed'
        return result

    snapshot = assessment_history.record_assessment(db, customer, strategy_snapshot=[])
    monkeypatch.setattr(chat_engine, 'generate', generate)
    sid = client.post('/api/chat/sessions', json={'customer_id': customer.id}).json()['id']
    response = client.post(f'/api/chat/sessions/{sid}/messages',
                           json={'content': '生成策略', 'scenario': 'strategy', 'stream': stream})
    assert response.status_code == 200
    done = _parse_sse(response.text)[-1][1] if stream else response.json()
    final = done['message']
    assert not final['degraded']
    assert bad_action not in json.dumps(final, ensure_ascii=False)
    assert [i['action'] for i in final['strategy_items']] == [fixed_action, SAFE_ITEM['action']]
    db.expire_all()
    assert db.get(ChatMessage, final['id']).strategy_items == final['strategy_items']
    assert db.get(AssessmentHistory, snapshot.id).strategy_snapshot == final['strategy_items']
    history = client.get(f'/api/chat/sessions/{sid}').json()['messages']
    assert bad_action not in json.dumps(history, ensure_ascii=False)

    next_adapter = FakeAdapter(chunks=['后续完整答复'])
    llm_adapter.set_chat_adapter(next_adapter)
    client.post(f'/api/chat/sessions/{sid}/messages',
                json={'content': '继续', 'scenario': 'free_qa', 'stream': False})
    assert next_adapter.calls
    sent = [m.content for m in next_adapter.calls[0] if m.role == 'assistant']
    assert all(bad_action not in text for text in sent)


VERIFIED_OVERVIEW = "已核实客户当前仍有沟通基础，可继续了解需求。"
CONTROLLED_SOURCE = VERIFIED_OVERVIEW + "制度规定36小时内核验。建议开展需求访谈。"
BAD_ACTION = "制度规定72小时内核验。"
MIXED_NOTICE = "概况已通过核实；未形成可核实的 AI 行动策略，以下行动来自规则建议。"


def _install_real_graph_selection(monkeypatch, *, keep_one_card=False):
    """Run the actual Graph/check/selection protocols; only the model is controlled."""
    results = []

    def generate(scenario, ctx, customer, db, **kwargs):
        bad = {**SAFE_ITEM, "title": "不可靠核验行动", "action": BAD_ACTION}
        raw_items = [bad, SAFE_ITEM] if keep_one_card else [bad]
        draft = VERIFIED_OVERVIEW + '\n\n```json\n' + json.dumps({"strategies": raw_items}, ensure_ascii=False) + '\n```'
        answer = local_quality.parse_answer(draft)
        issue = {"issues": [{"claim": BAD_ACTION, "source_id": "knowledge",
                             "evidence": "制度规定36小时内核验。", "reason": "核验动作时限应为36小时"}],
                 "score_projections": []}
        selected = {"units": [{"id": unit.id, "decision": "keep", "depends_on": [],
                               "sources": [{"source_id": "knowledge", "evidence": CONTROLLED_SOURCE}]}
                              for unit in answer.units]}
        adapter = ScriptedAdapter([draft, json.dumps(issue, ensure_ascii=False),
                                   json.dumps(selected, ensure_ascii=False), '{"issues":[],"score_projections":[]}'])
        ctx = replace(ctx, knowledge_text=CONTROLLED_SOURCE, references=[])
        result = AssessmentStrategyAgent(
            adapter=adapter, max_iterations=1, tools_enabled=False, on_event=kwargs.get("on_event"),
            cancel_event=kwargs.get("cancel_event"), deadline=kwargs.get("deadline"),
            token_budget=kwargs.get("token_budget"),
        ).run(scenario, ctx, customer, db, retrieve_enabled=False)
        assert len(adapter.calls) == 4
        assert result.timings["verification"][-1]["status"] == "passed"
        results.append(result)
        return result

    monkeypatch.setattr(chat_engine, "generate", generate)
    monkeypatch.setattr(strategy, "generate", generate)
    return results


@pytest.mark.parametrize("stream", [False, True])
def test_verified_overview_without_ai_cards_keeps_overview_and_supplies_explicit_rule_cards(
    client, customer, db, fake_llm, monkeypatch, stream,
):
    results = _install_real_graph_selection(monkeypatch)
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    response = client.post(f"/api/chat/sessions/{session_id}/messages",
                           json={"content": "生成策略", "scenario": "strategy", "stream": stream})
    assert response.status_code == 200
    if stream:
        events = _parse_sse(response.text)
        assert events[-1][0] == "done"
        done = events[-1][1]
        synchronized = [data["text"] for name, data in events if name == "replace" and data.get("reason") == "final_sync"]
        assert synchronized and BAD_ACTION not in synchronized[-1]
    else:
        done = response.json()
    message = done["message"]
    generated = results[-1]
    assert generated.degraded and generated.text == VERIFIED_OVERVIEW
    assert generated.timings["answer_source"] == "ai_summary_with_rule_strategies"
    assert generated.warnings == [MIXED_NOTICE]
    assert generated.degraded_items
    assert message["degraded"] is True and message["strategy_items"]
    assert VERIFIED_OVERVIEW in message["content"] and MIXED_NOTICE in message["content"]
    assert BAD_ACTION not in message["content"] and "不可靠核验行动" not in json.dumps(message, ensure_ascii=False)
    assert all(item["reference"] == "评分规则建议" for item in message["strategy_items"])
    db.expire_all()
    stored = db.get(ChatMessage, message["id"])
    assert stored.content == message["content"] and stored.strategy_items == message["strategy_items"]


@pytest.mark.parametrize("stream", [False, True])
def test_zero_ai_card_regeneration_retains_old_complete_strategy_and_snapshot(
    client, customer, db, fake_llm, monkeypatch, stream,
):
    snapshot = assessment_history.record_assessment(db, customer, strategy_snapshot=[])
    llm_adapter.set_chat_adapter(FakeAdapter(chunks=[
        "旧的完整策略。\n```json\n" + json.dumps({"strategies": [SAFE_ITEM]}, ensure_ascii=False) + "\n```"
    ]))
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    old_response = client.post(f"/api/chat/sessions/{session_id}/messages",
                               json={"content": "生成策略", "scenario": "strategy", "stream": False}).json()
    old = old_response["message"]
    assert not old["degraded"] and old["strategy_items"]
    db.expire_all()
    original_snapshot = db.get(AssessmentHistory, snapshot.id).strategy_snapshot
    results = _install_real_graph_selection(monkeypatch)
    response = client.post(f"/api/chat/sessions/{session_id}/regenerate",
                           json={"scenario": "strategy", "stream": stream})
    if stream:
        events = _parse_sse(response.text)
        assert events[-1][0] == "error"
        error = events[-1][1]
        assert all(name != "done" for name, _ in events)
    else:
        assert response.status_code == 200
        error = response.json()["error"]
    if stream:
        assert error["retained_message_id"] == old["id"]
    else:
        assert "已保留原答案" in error and response.json()["message"] is None
    assert results[-1].timings["answer_source"] == "ai_summary_with_rule_strategies"
    db.expire_all()
    assert db.get(ChatMessage, old["id"]).superseded_by_id is None
    assert db.get(AssessmentHistory, snapshot.id).strategy_snapshot == original_snapshot
    history = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
    assistants = [message for message in history if message["role"] == "assistant"]
    assert [message["id"] for message in assistants] == [old["id"]]
    assert assistants[0]["strategy_items"] == old["strategy_items"]


def test_zero_ai_card_report_and_pdf_use_the_same_explicit_rule_cards(db, customer, fake_llm, monkeypatch):
    results = _install_real_graph_selection(monkeypatch)
    report = build_report_data(db, customer, include_ai=True)
    generated = results[-1]
    assert report.has_ai and report.degraded and report.strategy_items
    assert report.timings["answer_source"] == "ai_summary_with_rule_strategies"
    assert report.warnings == [MIXED_NOTICE]
    assert report.strategy_items == present_strategies(generated.degraded_items)
    assert all(item["reference"] == "评分规则建议" for item in report.strategy_items)
    generator = PdfReportGenerator()
    section = _collect_flow_text(generator._ai_strategy_section(report.strategy_items, report.references))
    assert "评分规则建议" in section and "不可靠核验行动" not in section and BAD_ACTION not in section
    pdf = generator.generate(report.assessment, strategy_items=report.strategy_items,
                             references=report.references, generation_warnings=report.warnings)
    assert pdf.startswith(b"%PDF")


def test_one_verified_ai_card_still_completes_regeneration_and_replaces_snapshot(
    client, customer, db, fake_llm, monkeypatch,
):
    snapshot = assessment_history.record_assessment(db, customer, strategy_snapshot=[])
    llm_adapter.set_chat_adapter(FakeAdapter(chunks=[
        "旧的完整策略。\n```json\n" + json.dumps({"strategies": [{**SAFE_ITEM, "title": "旧卡片"}]}, ensure_ascii=False) + "\n```"
    ]))
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    old = client.post(f"/api/chat/sessions/{session_id}/messages",
                      json={"content": "生成策略", "scenario": "strategy", "stream": False}).json()["message"]
    results = _install_real_graph_selection(monkeypatch, keep_one_card=True)
    response = client.post(f"/api/chat/sessions/{session_id}/regenerate",
                           json={"scenario": "strategy", "stream": False}).json()
    final = response["message"]
    assert not final["degraded"] and not results[-1].degraded
    assert results[-1].timings["answer_source"] == "ai_with_local_repairs"
    assert [item["title"] for item in final["strategy_items"]] == [SAFE_ITEM["title"]]
    db.expire_all()
    assert db.get(ChatMessage, old["id"]).superseded_by_id == final["id"]
    assert db.get(AssessmentHistory, snapshot.id).strategy_snapshot == final["strategy_items"]
