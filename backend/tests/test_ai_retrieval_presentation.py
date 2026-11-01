"""Frozen retrieval presentation must not change bibliography, permissions or model history."""
from copy import deepcopy

import pytest

from test_ai_protocol import v2db
from models import (AIAssessmentRun, ChatMessage, Customer, KnowledgeDocument,
                    KnowledgeItem, KnowledgeChunk)
from services.ai import chat
from services.ai.protocol import fingerprint
from services.ai.repository import available_result, resolve_messages


@pytest.fixture
def published(v2db):
    db, customer, session = v2db
    evidence = []
    items = []
    for i in range(2):
        doc = KnowledgeDocument(title=f'合成资料{i}', status='canonical', index_status='indexed')
        db.add(doc); db.flush()
        item = KnowledgeItem(document_id=doc.id, title=doc.title, status='canonical', category='内部规范')
        chunk = KnowledgeChunk(document_id=doc.id, content=f'冻结原文{i}', chunk_index=0)
        db.add_all([item, chunk]); db.flush()
        items.append(item)
        evidence.append(dict(chunk_id=chunk.id, document_id=doc.id, item_id=item.id,
            title=item.title, category=item.category, content=chunk.content,
            content_hash=fingerprint(dict(content=chunk.content, title=item.title, category=item.category,
                                          status='canonical', document_status='canonical'))))
    evidence[1]['score'] = .72
    selected = dict(id='src:'+str(evidence[0]['chunk_id']), chunk_id=evidence[0]['chunk_id'],
                    title=evidence[0]['title'], snippet=evidence[0]['content'], used=True)
    result = dict(content='业务分析', strategy_items=[], references=[selected], assessment={}, trend={})
    run = AIAssessmentRun(run_id='presentation-run', customer_id=customer.id, session_id=session.id,
        scenario='assessment', status='succeeded', payload_hash='fixture',
        fact_package={'evidence':evidence}, validated_result=result,
        usage={'total_tokens':1,'calls':[],'llm_call_count':1})
    db.add(run); db.flush()
    message = ChatMessage(session_id=session.id, role='assistant', content='',
                          ai_run_id=run.run_id, content_format='ai_v2_ref')
    db.add(message); db.commit()
    return db, customer, session, run, message, items


def test_refresh_exposes_hits_with_truthful_used_flags_and_preserves_bibliography(published):
    db, customer, session, run, message, _ = published
    original = deepcopy(run.validated_result)
    refs = resolve_messages(db, [message])[0]['references']
    assert len(refs) == 2 and [r['used'] for r in refs] == [True, False]
    assert refs[0]['score'] is None and refs[1]['score'] == .72
    for ref, e in zip(refs, run.fact_package['evidence']):
        assert (ref['document_id'],ref['item_id'],ref['chunk_id']) == (e['document_id'],e['item_id'],e['chunk_id'])
        assert ref['snippet'] == e['content']
    assert run.validated_result == original
    assert available_result(db,run,customer_id=customer.id,session_id=session.id)['references'] == original['references']
    assert resolve_messages(db,[message],purpose='model_history')[0]['references'] == original['references']
    assert '冻结原文1' not in str(resolve_messages(db,[message],purpose='model_history'))


def test_existing_run_without_selected_citations_still_displays_hits(published):
    db, _, _, run, message, _ = published
    run.validated_result = dict(run.validated_result, references=[])
    db.commit()
    refs = resolve_messages(db,[message])[0]['references']
    assert len(refs)==2 and all(not ref['used'] for ref in refs)


@pytest.mark.parametrize('change',['withdraw','content_changed','wrong_customer'])
def test_unavailable_or_wrong_scope_never_exposes_frozen_hits(published,change):
    db, _, session, run, message, items = published
    if change=='withdraw':
        items[1].status='proposed'
    elif change=='content_changed':
        db.get(KnowledgeChunk,run.fact_package['evidence'][1]['chunk_id']).content='变更后原文'
    else:
        other=Customer(customer_name='另一客户',custom_fields={})
        db.add(other);db.flush();session.customer_id=other.id
    db.commit()
    for purpose in ('display','model_history'):
        result=resolve_messages(db,[message],purpose=purpose)[0]
        assert result['references']==[] and result['availability']=='source_unavailable'


def test_stream_nonstream_and_idempotent_replay_match_refresh(published,monkeypatch):
    db, customer, session, run, message, _ = published
    run_id=run.run_id
    monkeypatch.setattr(chat,'execute',lambda worker_db,*a,**k:worker_db.get(AIAssessmentRun,run_id))
    expected=resolve_messages(db,[message])[0]['references']
    events=list(chat.run_turn(db,session,scenario='assessment',persist_user=False))
    assert not [event for event in events if event.type=='error']
    assert next(e.data['items'] for e in events if e.type=='references')==expected
    assert next(e.data['message']['references'] for e in events if e.type=='done')==expected
    from services.ai import chat_engine
    reply=chat_engine.complete_turn(db,session,scenario='assessment',persist_user=False)
    assert not reply['error']
    assert reply['references']==reply['message']['references']==expected
    identity={'fixture':'repeat'}
    run.idempotency_key=fingerprint([session.id,None,'regenerate','repeat-key'])
    run.payload_hash=fingerprint(identity)
    db.commit()
    replay=chat.replay_regeneration(db,session,'repeat-key',identity)
    assert replay['references']==replay['message']['references']==expected
