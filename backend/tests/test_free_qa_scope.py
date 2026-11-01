"""Out-of-scope questions stop before planning, without fallback suggestions."""
import json
from types import SimpleNamespace

import pytest

from models import AIAssessmentRun, ChatMessage
from services.ai.chat import run_turn
from services.ai import evidence, orchestrator
from test_ai_protocol import v2db


@pytest.mark.parametrize('status,clarification,code,text', [
    ('unsupported', None, 'out_of_scope', '这个问题超出客情助手的回答范围'),
    ('clarify', 'choose_subject', 'needs_clarification', '请明确要查询的客户'),
])
def test_scope_and_clarification_have_distinct_messages_and_no_plan(
        v2db, monkeypatch, status, clarification, code, text):
    db, customer, session = v2db
    calls = []

    class ScopeAdapter:
        def chat_completion_once(self, messages, **kwargs):
            calls.append(messages)
            assert len(calls) == 1, 'Scope refusal must not invoke plan or repair'
            return SimpleNamespace(content=json.dumps(dict(
                version='2.4', status=status, intents=[], clarification_code=clarification)),
                raw_usage={'total_tokens': 10})

    def no_hits(db, package, question, customer):
        package['retrieval_status'] = 'no_hits'
        return {}

    monkeypatch.setattr(orchestrator, 'get_chat_adapter', ScopeAdapter)
    monkeypatch.setattr(evidence, 'retrieve', no_hits)
    events = list(run_turn(db, session, content='教我做一份蛋糕', scenario='free_qa'))
    errors = [event.data for event in events if event.type == 'error']
    assert len(errors) == 1
    assert errors[0]['error_code'] == code
    assert text in errors[0]['message']
    assert not any(event.type in ('delta', 'strategy', 'done') for event in events)
    assert len(calls) == 1
    run = db.query(AIAssessmentRun).one()
    assert run.error_code == code
    assert not run.validated_result
    assert db.query(ChatMessage).filter_by(role='assistant').count() == 0
    if status == 'unsupported':
        assert run.status == 'incomplete'
        assert '请明确' not in errors[0]['message']
