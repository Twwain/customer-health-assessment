"""A3 uses real isolated SQLite state and fault/barrier injection; no providers."""
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from test_ai_protocol import v2db, Adapter
from database import utcnow
from models import (AIAssessmentRun,AIAssessmentDependency,Customer,ChatMessage,
                    KnowledgeDocument,KnowledgeItem,KnowledgeChunk)
from services.ai import audit,execution,orchestrator,evidence
from services.ai.facts import capture,capture_comparison
from services.ai.protocol import ProtocolError,fingerprint,load_runtime_assets
from services.ai.orchestrator import execute
from services.ai.repository import sources_changed,expire_private_data,purge_customer_dependencies
from services.ai.chat import cancel_run


class InvalidIntent:
    available=True
    def __init__(self):self.calls=0
    def chat_completion_once(self,messages,**kwargs):
        self.calls+=1
        value=json.loads(messages[-1]['content'])
        result=dict(version='2.4',status='resolved',clarification_code=None,intents=[
            dict(kind='policy',subject_refs=value['allowed_subject_refs'],field_refs=[],option_refs=['PRIVATE_MODEL_VALUE_13812345678'],
                 source_refs=value['allowed_source_refs'],count=None)])
        return SimpleNamespace(content=json.dumps(result),raw_usage={'total_tokens':20})


def synthetic_source(db):
    doc=KnowledgeDocument(title='合成审计制度',status='canonical',index_status='indexed')
    db.add(doc);db.flush()
    item=KnowledgeItem(document_id=doc.id,title=doc.title,category='内部规范',status='canonical')
    chunk=KnowledgeChunk(document_id=doc.id,chunk_index=0,content='财务负责人应在36小时内完成核验。')
    db.add_all([item,chunk]);db.commit()
    return doc,item,chunk


def inject_source(package,doc,item,chunk):
    package['input_fingerprint']=package['source_fingerprint']
    row=dict(document_id=doc.id,item_id=item.id,chunk_id=chunk.id,title=item.title,category=item.category,
        content=chunk.content,source_class='general_knowledge',applicability='unknown',
        content_hash=fingerprint(dict(content=chunk.content,title=item.title,category=item.category,
            status=item.status,document_status=doc.status)))
    package['evidence']=[row];package['retrieval_status']='available'
    package['sources']['src:'+str(chunk.id)]=dict(id='src:'+str(chunk.id),kind='knowledge',
        record_ref='knowledge:'+str(chunk.id),content_hash=row['content_hash'],
        visibility_state='active',source_class='general_knowledge')
    package['source_fingerprint']=fingerprint([package['input_fingerprint'],
        [(row['document_id'],row['item_id'],row['chunk_id'],row['content_hash'])]])


def pending_run(db,customer,session,name='audit-pending',package=None):
    run=AIAssessmentRun(run_id=name,customer_id=customer.id,session_id=session.id,payload_hash='synthetic',
        scenario='free_qa',status='planning',versions={'runtime':execution.register(name,60)},
        usage={'audit':audit.initial_audit()})
    db.add(run);db.commit()
    if package:
        audit.update_run(db.get_bind(),name,'checkpoint',stage='capture_ready',package=package)
    db.expire_all()
    return db.get(AIAssessmentRun,name)


def test_failed_intent_persists_exact_assets_source_and_safe_parameter_shape(v2db):
    db,customer,session=v2db;adapter=InvalidIntent()
    run=execute(db,customer.id,'free_qa',question='解释制度',session_id=session.id,
                adapter=adapter,retrieve_evidence=False)
    assert run.status=='incomplete' and run.error_code=='invalid_intent_reference'
    assert adapter.calls==1 and run.versions['intent_rules_version']=='1.2' and run.versions['sha256']
    meta=run.usage['audit']
    assert meta['stage']=='failure' and meta['source_kind']=='captured'
    assert meta['capture_fingerprint']==run.source_fingerprint and meta['evidence_fingerprint'] is None
    assert meta['assets_state']=='verified' and meta['revision']>=3
    detail=next(d for d in run.usage['parameter_diagnostics'] if d['pointer']=='/intents/0/option_refs')
    assert detail['json_type']=='array' and detail['value_sha256'] and detail['allowed_count']==0
    assert 'PRIVATE_MODEL_VALUE' not in json.dumps(run.usage) and '13812345678' not in json.dumps(run.usage)
    assert run.fact_package=={} and db.query(AIAssessmentDependency).filter_by(run_id=run.run_id).count()==3


def test_assets_fail_after_run_creation_without_call_and_release_runtime(v2db,monkeypatch):
    db,customer,session=v2db;adapter=InvalidIntent()
    def fail():raise ProtocolError('asset_integrity_error')
    monkeypatch.setattr(orchestrator,'load_runtime_assets',fail)
    run=execute(db,customer.id,'assessment',session_id=session.id,adapter=adapter,retrieve_evidence=False)
    assert run.error_code=='asset_integrity_error' and adapter.calls==0
    assert run.usage['audit']['assets_state']=='mismatch'
    assert run.usage['audit']['source_kind']=='not_available' and run.source_fingerprint==''
    assert set(run.versions)=={'runtime'}
    assert not execution.executor_alive(run.run_id,run.versions['runtime'])


def test_new_interpreter_import_does_not_read_runtime_assets(tmp_path):
    root=Path(__file__).parents[2]
    script="""from pathlib import Path
old=Path.read_text
def read(path,*args,**kwargs):
 if 'assets' in str(path):raise FileNotFoundError('injected missing assets')
 return old(path,*args,**kwargs)
Path.read_text=read
import services.ai.orchestrator
print('imported_without_assets')
"""
    env={**os.environ,'DB_PATH':str(tmp_path/'isolated-import.db'),'SECRET_KEY_FILE':str(root/'.ch_secret')}
    process=subprocess.run([sys.executable,'-c',script],cwd=root/'backend',env=env,capture_output=True,text=True)
    assert process.returncode==0,process.stderr
    assert 'imported_without_assets' in process.stdout


def test_retrieval_failure_does_not_claim_complete_evidence(v2db,monkeypatch):
    db,customer,session=v2db;adapter=InvalidIntent()
    def fail(db,package,question,customer):
        package['retrieval_status']='unavailable';raise ProtocolError('dependency_unavailable')
    monkeypatch.setattr(evidence,'retrieve',fail)
    run=execute(db,customer.id,'assessment',session_id=session.id,adapter=adapter,supersede_message_id=999)
    assert run.status=='dependency_unavailable' and adapter.calls==0
    assert run.usage['audit']['source_kind']=='captured'
    assert run.usage['audit']['retrieval_state']=='failed'
    assert run.usage['audit']['evidence_fingerprint'] is None


def test_real_readonly_sqlite_fault_stops_before_model_then_can_record_failure(v2db,monkeypatch,tmp_path):
    db,customer,session=v2db;adapter=Adapter();original=audit.update_run
    def guarded(bind,run_id,event,**kwargs):
        if event=='checkpoint' and kwargs.get('stage')=='before_plan':
            con=sqlite3.connect((tmp_path/'fault.db').as_uri()+'?mode=rwc',uri=True)
            con.execute('CREATE TABLE IF NOT EXISTS fault(value INTEGER)');con.commit();con.close()
            con=sqlite3.connect((tmp_path/'fault.db').as_uri()+'?mode=ro',uri=True)
            try:con.execute('INSERT INTO fault VALUES(1)')
            finally:con.close()
        return original(bind,run_id,event,**kwargs)
    monkeypatch.setattr(audit,'update_run',guarded)
    run=execute(db,customer.id,'assessment',session_id=session.id,adapter=adapter,retrieve_evidence=False)
    assert run.error_code=='audit_persist_failed' and not adapter.calls
    assert run.usage['audit']['stage']=='failure'


def test_early_knowledge_dependency_withdrawal_cleans_failed_diagnostics(v2db,monkeypatch):
    db,customer,session=v2db;doc,item,chunk=synthetic_source(db)
    def retrieve(db,package,question,customer):
        inject_source(package,doc,item,chunk);return {}
    monkeypatch.setattr(evidence,'retrieve',retrieve)
    run=execute(db,customer.id,'free_qa',question='请解释《合成审计制度》。',
        session_id=session.id,adapter=InvalidIntent())
    assert run.status=='incomplete' and run.usage['audit']['source_kind']=='full_evidence'
    assert run.fact_package=={} and run.usage['parameter_diagnostics']
    deps=db.query(AIAssessmentDependency).filter_by(run_id=run.run_id,source_kind='knowledge').all()
    assert len(deps)==1 and deps[0].source_id==f'{doc.id}:{chunk.id}:{item.id}'
    status,error=run.status,run.error_code
    item.status='proposed';db.commit();sources_changed(db)
    assert run.status==status and run.error_code==error
    assert run.usage['audit']['invalidated'] and run.usage['audit']['diagnostics_cleared']
    assert not any(k in run.usage for k in audit.PRIVATE_USAGE)


def test_failed_retention_clears_all_parameter_copies(v2db):
    db,customer,session=v2db
    run=execute(db,customer.id,'free_qa',question='解释制度',session_id=session.id,
                adapter=InvalidIntent(),retrieve_evidence=False)
    run.updated_at=utcnow()-timedelta(days=8);db.commit()
    expire_private_data(db)
    assert run.usage['audit']['diagnostics_cleared']
    assert not any(k in run.usage for k in audit.PRIVATE_USAGE)
    assert run.versions['sha256'] and run.error_code=='invalid_intent_reference'


def test_early_comparison_dependency_delete_cannot_recreate_run(v2db):
    db,customer,session=v2db
    other=Customer(customer_name='合成比较成员',custom_fields={});db.add(other);db.commit()
    package=capture_comparison(db.get_bind(),customer.id,[other.id])
    run=pending_run(db,customer,session,package=package)
    assert not run.fact_package
    purge_customer_dependencies(db,other.id);db.delete(other);db.commit()
    assert db.get(AIAssessmentRun,run.run_id) is None
    assert not audit.update_run(db.get_bind(),run.run_id,'usage_only',usage={'calls':[]})
    assert db.query(AIAssessmentRun).filter_by(run_id=run.run_id).count()==0
    execution.unregister(run.run_id)


def test_cancel_during_http_settles_usage_without_publishing(v2db):
    db,customer,session=v2db;entered=threading.Event();release=threading.Event();result={}
    class Slow(Adapter):
        def chat_completion_once(self,messages,**kwargs):
            entered.set();assert release.wait(5)
            return super().chat_completion_once(messages,**kwargs)
    def work():
        try:
            with Session(db.get_bind()) as writer:
                result['id']=execute(writer,customer.id,'assessment',session_id=session.id,adapter=Slow(),retrieve_evidence=False).run_id
        except BaseException as exc:result['error']=type(exc).__name__
    thread=threading.Thread(target=work);thread.start();assert entered.wait(5)
    db.expire_all();run=db.query(AIAssessmentRun).filter_by(status='planning').one()
    cancel_run(db,session.id,run.run_id);release.set();thread.join(5)
    assert not thread.is_alive() and 'error' not in result,result
    db.refresh(run)
    assert run.status=='cancelled' and run.usage['audit']['terminal_kind']=='cancelled'
    assert run.usage['total_tokens']==100 and run.usage['calls'][0]['usage_estimated'] is False
    assert db.query(ChatMessage).filter_by(ai_run_id=run.run_id).count()==0


def test_writer_lock_serializes_withdrawal_and_prevents_private_resurrection(v2db,monkeypatch):
    db,customer,session=v2db;doc,item,chunk=synthetic_source(db)
    package=capture(db.get_bind(),customer.id);inject_source(package,doc,item,chunk)
    run=pending_run(db,customer,session,package=package)
    entered=threading.Event();release=threading.Event();withdrawing=threading.Event();withdrawn=threading.Event();errors=[]
    original=audit.apply_locked
    def barrier(writer,current,event,**kwargs):
        if event=='checkpoint' and kwargs.get('stage')=='before_plan':
            entered.set();assert release.wait(5)
        return original(writer,current,event,**kwargs)
    monkeypatch.setattr(audit,'apply_locked',barrier)
    def checkpoint():
        try:audit.update_run(db.get_bind(),run.run_id,'checkpoint',stage='before_plan',package=package,
                            fields={'fact_package':package})
        except BaseException as exc:errors.append(type(exc).__name__)
    def withdraw():
        try:
            withdrawing.set()
            with Session(db.get_bind()) as writer:
                target=writer.get(KnowledgeItem,item.id);target.status='proposed';writer.commit();sources_changed(writer)
        except BaseException as exc:errors.append(type(exc).__name__)
        finally:withdrawn.set()
    first=threading.Thread(target=checkpoint);first.start();assert entered.wait(5)
    second=threading.Thread(target=withdraw);second.start();assert withdrawing.wait(1)
    assert not withdrawn.wait(.1)
    release.set();first.join(5);second.join(5)
    assert not first.is_alive() and not second.is_alive() and not errors,errors
    db.refresh(run)
    assert run.status=='source_changed' and run.usage['audit']['invalidated'] and not run.fact_package
    with pytest.raises(ProtocolError,match='source_changed'):
        audit.update_run(db.get_bind(),run.run_id,'checkpoint',stage='before_repair',package=package,
                         fields={'fact_package':package})
    db.refresh(run);assert not run.fact_package and not run.validated_result
    execution.unregister(run.run_id)


def test_late_usage_settles_registered_attempt_once_without_terminal_or_private_changes(v2db):
    db,customer,session=v2db;run=pending_run(db,customer,session)
    estimate=dict(phase='plan',attempt=1,usage_estimated=True,total_tokens=300)
    audit.update_run(db.get_bind(),run.run_id,'checkpoint',stage='before_plan',usage={'calls':[estimate]})
    cancel_run(db,session.id,run.run_id)
    actual=dict(estimate,usage_estimated=False,total_tokens=125)
    assert audit.update_run(db.get_bind(),run.run_id,'usage_only',usage={'calls':[actual]})
    db.refresh(run);before=deepcopy(run.usage);updated=run.updated_at
    assert run.status=='cancelled' and before['total_tokens']==125
    assert audit.update_run(db.get_bind(),run.run_id,'usage_only',usage={'calls':[actual]})
    db.refresh(run);assert run.usage==before and run.updated_at==updated
    assert not audit.update_run(db.get_bind(),run.run_id,'usage_only',usage={'calls':[dict(actual,total_tokens=999)]})
    assert not audit.update_run(db.get_bind(),run.run_id,'usage_only',usage={'calls':[dict(actual,attempt=2)]})
    db.refresh(run);assert run.usage==before
    execution.unregister(run.run_id)


def test_all_audit_writes_unavailable_stop_and_recover_without_invented_assets(v2db,monkeypatch,tmp_path):
    db,customer,session=v2db;adapter=Adapter();original=audit.update_run
    fault=tmp_path/'readonly.db'
    connection=sqlite3.connect(fault);connection.execute('CREATE TABLE fault(value INTEGER)');connection.close()
    def unavailable(*args,**kwargs):
        reader=sqlite3.connect(fault.as_uri()+'?mode=ro',uri=True)
        try:reader.execute('INSERT INTO fault VALUES(1)')
        finally:reader.close()
    monkeypatch.setattr(audit,'update_run',unavailable)
    with pytest.raises(ProtocolError,match='audit_persist_failed'):
        execute(db,customer.id,'assessment',session_id=session.id,adapter=adapter,retrieve_evidence=False)
    assert not adapter.calls
    db.expire_all();run=db.query(AIAssessmentRun).filter_by(status='capturing').one()
    assert not execution.executor_alive(run.run_id,run.versions['runtime'])
    monkeypatch.setattr(audit,'update_run',original)
    assert execution.recover_pending(db)==1
    db.refresh(run)
    assert run.status=='provider_error' and run.error_code=='executor_lost'
    assert run.usage['audit']['terminal_kind']=='audit_incomplete'
    assert run.usage['audit']['assets_state']=='not_available' and set(run.versions)=={'runtime'}


def test_parameter_summaries_match_closed_schema_and_never_retain_unknown_strings(v2db):
    from jsonschema import Draft202012Validator
    from services.ai.diagnostics import parameter_shapes
    db,customer,session=v2db;package=capture(db.get_bind(),customer.id)
    private='PRIVATE_VALUE_13812345678'
    values={'intents':[dict(kind='policy',subject_refs=[],field_refs=[],option_refs=[private],source_refs=[],count=True)],
            private:{private:[private]}}
    errors=[ProtocolError('invalid_intent','/intents/0/option_refs', ['field:'+f['field'] for f in package['factors']])]*30
    summaries=parameter_shapes(values,errors,package)
    assert len(summaries)<=24 and len(json.dumps(summaries,ensure_ascii=False,separators=(',',':')).encode())<=8192
    assert private not in json.dumps(summaries)
    schema=load_runtime_assets().diagnostic_schema
    for value in summaries:Draft202012Validator(schema).validate(value)
    unknown=parameter_shapes(values,[ProtocolError('invalid_intent','/'+private)],package)[0]
    assert unknown['pointer']=='' and unknown['unknown_key_count']==1 and private not in json.dumps(unknown)
    raw=parameter_shapes('{"private":"'+private+'",bad}',[ProtocolError('invalid_json')],package)[0]
    assert raw['json_type']=='invalid_json' and raw['response_bytes']>0 and raw['value_sha256']
    assert private not in json.dumps(raw)
