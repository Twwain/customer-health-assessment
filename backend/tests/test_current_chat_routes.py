"""Current public entry points and retired result handling."""
import pytest

from models import ChatMessage
from services.ai import chat_engine
from services.ai.repository import UNAVAILABLE, resolve_messages
from test_chat import client, customer, db, engine, session_factory, offline_llm, _reset_adapters


@pytest.mark.parametrize("stream", [False, True])
def test_assessment_uses_current_rule_fallback(client, customer, offline_llm, stream):
    sid = client.post('/api/chat/sessions', json={'customer_id': customer.id}).json()['id']
    response = client.post(f'/api/chat/sessions/{sid}/evaluate', json={'stream': stream})
    assert response.status_code == 200
    if stream:
        assert 'event: context' in response.text and 'event: done' in response.text
    else:
        value = response.json()
        assert value['message']['run_id'] and value['degraded']
        assert value['assessment']['customer_id'] == customer.id
    assert not chat_engine.is_session_streaming(sid)


def test_removed_shortcuts_and_scenarios_are_not_public_routes(client, customer):
    sid = client.post('/api/chat/sessions', json={'customer_id': customer.id}).json()['id']
    for path in ('strategy', 'alert-analysis'):
        assert client.post(f'/api/chat/sessions/{sid}/{path}', json={}).status_code == 404
    assert client.post(f'/api/chat/sessions/{sid}/messages', json={
        'content': '综合分析', 'scenario': 'strategy', 'stream': False}).status_code == 400


def test_obsolete_answer_is_hidden_without_mutating_record(db, customer):
    session = chat_engine.create_session(db, customer_id=customer.id)
    row = ChatMessage(session_id=session.id, role='assistant', content='旧结果',
                      references=[{'id': 'old-source'}], strategy_items=[{'title': '旧卡片'}])
    db.add(row)
    db.commit()
    for purpose in ('display', 'preview', 'model_history'):
        value = resolve_messages(db, [row], purpose)[0]
        assert value['content'] == UNAVAILABLE
        assert not value['references'] and not value['strategy_items']
    db.refresh(row)
    assert row.content == '旧结果' and row.references == [{'id': 'old-source'}]
