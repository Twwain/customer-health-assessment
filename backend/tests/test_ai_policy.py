"""Policy intent context, conservative decisions and real publication boundaries."""
import json
import time
from types import SimpleNamespace

import pytest

from test_ai_protocol import v2db, valid_plan
from services.ai.facts import capture
from services.ai.planner import Planner
from services.ai.protocol import ProtocolError


QUESTION = '请仅按已审核的《合成回款制度》引用原文，说明单据核验、付款排期分别由谁负责、时限是多少。不要判断本客户已经逾期或已适用制度。'


class IntentAdapter:
    def __init__(self, response):
        self.response = response
        self.payload = None

    def chat_completion_once(self, messages, **kwargs):
        self.payload = json.loads(messages[-1]['content'])
        return SimpleNamespace(content=json.dumps(self.response), raw_usage={'total_tokens': 10})


def resolved(package):
    return dict(version='2.4', status='resolved', clarification_code=None, intents=[dict(
        kind='policy', subject_refs=[package['customer_ref']], field_refs=[], option_refs=[],source_refs=[], count=None)])


def test_policy_request_exposes_capability_and_bound_subject_despite_missing_factors(v2db):
    db, customer, _ = v2db
    package = capture(db.get_bind(), customer.id)
    adapter = IntentAdapter(resolved(package))
    planner = Planner(adapter, token_budget=50000, output_limit=4000, deadline=time.monotonic()+20)
    result = planner.resolve_intent('请解释制度的责任主体和时限。', package, [])
    assert result[0]['kind'] == 'policy' and result[0]['count'] is None
    payload = adapter.payload
    assert not payload['allowed_option_refs']
    assert payload['subject_context']['bound_subject_ref'] == package['customer_ref']
    assert payload['subject_context']['use_bound_subject_for_implicit_subject'] is True
    assert '不得替代' in payload['subject_context']['explicit_subject_rule']
    policy = payload['intent_definitions']['policy']
    assert '责任主体' in policy and '时限' in policy and '出处' in policy
    assert '无需客户因子' in policy and '不确认客户适用' in policy
    assert set(payload['intent_definitions']) == set(payload['allowed_intents'])
    assert 'count' in payload['argument_rules']


@pytest.mark.parametrize('status', ['clarify', 'unsupported'])
def test_explicit_policy_words_do_not_override_model_ambiguity(v2db, status):
    db, customer, _ = v2db
    package = capture(db.get_bind(), customer.id)
    adapter = IntentAdapter(dict(version='2.4', status=status, clarification_code='choose_intent' if status=='clarify' else None, intents=[]))
    planner = Planner(adapter, token_budget=50000, output_limit=4000, deadline=time.monotonic()+20)
    expected_code = 'out_of_scope' if status == 'unsupported' else 'needs_clarification'
    with pytest.raises(ProtocolError, match=expected_code):
        planner.resolve_intent(QUESTION, package, [])
    decision = planner.usage()['intent_resolution']
    assert decision['status'] == status and decision['clarification_code'] == ('choose_intent' if status=='clarify' else None)
    assert decision['stage'] == expected_code
    assert planner.usage()['llm_call_count'] == 1


def test_intent_diagnostic_does_not_retain_unregistered_model_text(v2db):
    db, customer, _ = v2db
    package = capture(db.get_bind(), customer.id)
    secret_text = 'private model content 13912345678'
    adapter = IntentAdapter(dict(version='2.4', status=secret_text, clarification_code=secret_text,
                                intents=[{'kind': secret_text}]))
    planner = Planner(adapter, token_budget=50000, output_limit=4000, deadline=time.monotonic()+20)
    with pytest.raises(ProtocolError, match='invalid_intent'):
        planner.resolve_intent(QUESTION, package, [])
    decision = planner.usage()['intent_resolution']
    assert secret_text not in json.dumps(decision)
    assert decision['status'] is None and decision['clarification_code'] is None
    assert decision['stage'] == 'invalid_intent'


def test_policy_quote_publishes_reloads_and_is_cleared_on_real_withdrawal(v2db, tmp_path, monkeypatch):
    from services.rag import knowledge_base
    from services.rag.vector_store import InMemoryVectorStore
    from services.rag.retriever import KnowledgeRetriever
    from services.ai import evidence
    from services.ai.orchestrator import execute
    from services.ai.repository import resolve_messages
    from models import ChatMessage, KnowledgeItem, AIAssessmentRun

    db, customer, session = v2db
    monkeypatch.setattr(knowledge_base, 'KNOWLEDGE_DATA_DIR', str(tmp_path/'uploads'))
    embed = lambda texts: [[1.0, 0.0] for _ in texts]
    store = InMemoryVectorStore()
    svc = knowledge_base.KnowledgeBaseService(db, store=store, embed_func=embed)
    doc = svc.create_from_upload(title='合成回款制度', category='内部规范', filename='policy.txt',
        raw='财务负责人应在36小时内完成单据核验，72小时内取得付款排期。'.encode(), status='canonical')
    item = db.query(KnowledgeItem).filter_by(document_id=doc.id).one()
    monkeypatch.setattr(evidence, 'KnowledgeRetriever', lambda: KnowledgeRetriever(store=store, embed_func=embed))

    class PolicyAdapter:
        available = True
        def chat_completion_once(self, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            if 'intent_output_schema' in payload:
                result = dict(version='2.4', status='resolved', clarification_code=None, intents=[dict(
                    kind='policy', subject_refs=payload['allowed_subject_refs'], field_refs=[], option_refs=[],source_refs=payload['allowed_source_refs'], count=None)])
            else:
                assert 'plan_schema' in payload, 'unexpected repair'
                catalog = {k: {v['id']: v for v in group} for k, group in payload['catalog'].items()}
                result = valid_plan(dict(snapshot_id=payload['snapshot_id']), catalog, payload['request_contract'])
                result['detail_refs'] = [dict(section='policy', render_ref=n['id'])
                    for n in catalog['render_nodes'].values() if 'policy' in n['sections']]
            return SimpleNamespace(content=json.dumps(result), raw_usage={'total_tokens': 10})

    run = execute(db, customer.id, 'free_qa', question=QUESTION, session_id=session.id, adapter=PolicyAdapter())
    assert run.status == 'succeeded', (run.status, run.error_code, run.usage)
    run_id = run.run_id
    db.expire_all()
    message = db.query(ChatMessage).filter_by(ai_run_id=run_id).one()
    assert message.content == '' and message.content_format == 'ai_v2_ref'
    read = resolve_messages(db, [message])[0]
    assert '36小时' in read['content'] and '72小时' in read['content']
    assert '尚未确认本客户适用性' in read['content']
    assert read['references'] and read['references'][0]['used']
    assert read['references'][0]['title'] == '合成回款制度'
    svc.revoke_item(item.id)
    db.expire_all()
    withdrawn = resolve_messages(db, [message])[0]
    assert withdrawn['availability'] == 'source_unavailable'
    assert not withdrawn['references'] and '36小时' not in withdrawn['content']
    assert db.get(AIAssessmentRun, run_id).validated_result == {}
