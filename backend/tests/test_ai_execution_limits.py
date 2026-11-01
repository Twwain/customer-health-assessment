"""Selected review regressions: stop gates and actual/reserved cost boundaries."""
from copy import deepcopy
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from test_ai_protocol import v2db, Adapter
from test_ai_audit import pending_run
from models import AIAssessmentRun, ChatMessage
from services.ai import audit, evidence, execution
from services.ai.orchestrator import execute
from services.ai.planner import Planner
from services.ai.protocol import ProtocolError, fingerprint
from services.ai import llm_adapter as transport


def nohits(package):
    package['retrieval_status']='no_hits'
    package['input_fingerprint']=package['source_fingerprint']
    package['source_fingerprint']=fingerprint([package['input_fingerprint'],[]])


@pytest.mark.parametrize('stage', ['assets_verified','capture_ready','retrieval_ready','before_plan'])
@pytest.mark.parametrize('action', ['delete','reject'])
def test_outer_checkpoint_stops_all_subsequent_work(v2db,monkeypatch,stage,action):
    db,customer,session=v2db; original=audit.update_run; retrieved=[];adapter=Adapter();ids=[]
    def guarded(bind,run_id,event,**kwargs):
        if event=='checkpoint' and kwargs.get('stage')==stage:
            ids.append(run_id)
            if action=='delete':
                with Session(bind) as writer:
                    writer.execute(delete(AIAssessmentRun).where(AIAssessmentRun.run_id==run_id));writer.commit()
            else:return False
        return original(bind,run_id,event,**kwargs)
    def retrieve(reader,package,*args):
        retrieved.append(True);nohits(package);return {}
    monkeypatch.setattr(audit,'update_run',guarded);monkeypatch.setattr(evidence,'retrieve',retrieve)
    if action=='delete':
        with pytest.raises(ProtocolError,match='run_missing'):
            execute(db,customer.id,'assessment',adapter=adapter,session_id=session.id)
        assert db.query(AIAssessmentRun).count()==0
    else:
        run=execute(db,customer.id,'assessment',adapter=adapter,session_id=session.id)
        assert run.error_code=='run_state_conflict' and not run.validated_result
    assert not adapter.calls
    assert len(retrieved)==int(stage in ('retrieval_ready','before_plan'))
    assert ids and not execution.executor_alive(ids[0],{})
    assert not db.query(ChatMessage).count()


@pytest.mark.parametrize('phase',['intent','plan','repair'])
@pytest.mark.parametrize('failure',['reject','exception','cancel'])
def test_registered_attempt_not_charged_when_stopped_before_adapter(phase,failure):
    adapter=Adapter();cancel=threading.Event();settled=[]
    def checkpoint(stage,usage):
        assert usage['llm_call_count']==0 and usage['total_tokens']==0 and usage['reserved_tokens']>0
        if failure=='reject':return False
        if failure=='exception':raise ProtocolError('audit_persist_failed')
        cancel.set()
    planner=Planner(adapter,token_budget=100000,output_limit=2000,deadline=time.monotonic()+20,
        cancelled=cancel,on_checkpoint=checkpoint,on_usage=lambda usage:settled.append(deepcopy(usage)))
    with pytest.raises(ProtocolError):planner.call(phase,{'synthetic':'input'})
    usage=settled[-1]
    assert not adapter.calls and planner.total==0
    assert usage['llm_call_count']==usage['total_tokens']==usage['reserved_tokens']==0
    assert usage['calls'][0]['attempt_state']=='not_sent' and not usage['usage_estimated']


def test_real_checkpoint_failure_after_registration_is_not_billed(v2db,monkeypatch):
    db,customer,session=v2db;original=audit.update_run;adapter=Adapter()
    def guarded(bind,run_id,event,**kwargs):
        if event=='checkpoint' and kwargs.get('usage',{}).get('calls'):
            raise ProtocolError('audit_persist_failed')
        return original(bind,run_id,event,**kwargs)
    monkeypatch.setattr(audit,'update_run',guarded)
    run=execute(db,customer.id,'assessment',session_id=session.id,adapter=adapter,retrieve_evidence=False)
    assert run.error_code=='audit_persist_failed' and not adapter.calls
    assert run.usage['llm_call_count']==run.usage['total_tokens']==0
    assert run.usage['calls'][0]['attempt_state']=='not_sent'


@pytest.mark.parametrize('terminal',['cancel','invalidate','failure'])
def test_late_embedding_cost_settles_once_without_reviving_private_state(v2db,monkeypatch,terminal):
    db,customer,session=v2db;adapter=Adapter()
    def retrieve(reader,package,*args):
        run_id=reader.query(AIAssessmentRun).one().run_id;reader.rollback()
        kwargs={'status':'cancelled','error_code':'cancelled'} if terminal=='cancel' else (
            {'status':'incomplete','error_code':'synthetic_failure'} if terminal=='failure' else {})
        audit.update_run(reader.get_bind(),run_id,terminal,**kwargs)
        nohits(package)
        package['retrieval_usage']={'http_attempts':1,'total_tokens':7,'usage_estimated':False}
        return {}
    monkeypatch.setattr(evidence,'retrieve',retrieve)
    run=execute(db,customer.id,'assessment',adapter=adapter,session_id=session.id)
    assert not adapter.calls and not run.validated_result and not run.fact_package
    assert run.usage['embedding']['http_attempts']==1 and run.usage['embedding']['total_tokens']==7
    before=deepcopy(run.usage)
    delta={'embedding_calls':deepcopy(before['embedding_calls']),'parameter_diagnostics':[{'private':'forbidden'}]}
    audit.update_run(db.get_bind(),run.run_id,'usage_only',usage=delta);db.refresh(run)
    assert run.usage==before
    delta['embedding_calls'][0]['total_tokens']=999
    audit.update_run(db.get_bind(),run.run_id,'usage_only',usage=delta);db.refresh(run)
    assert run.usage==before


def test_unknown_late_embedding_operations_and_chat_attempts_are_ignored(v2db):
    db,customer,session=v2db;run=pending_run(db,customer,session)
    audit.update_run(db.get_bind(),run.run_id,'cancel',status='cancelled',error_code='cancelled')
    incoming={'embedding_calls':[dict(operation_id='retrieval:1',state='settled',model='embedding-3',
        http_attempts=1,total_tokens=7,usage_estimated=False)],
        'calls':[dict(phase='plan',attempt=1,total_tokens=99,usage_estimated=False,attempt_state='settled')]}
    audit.update_run(db.get_bind(),run.run_id,'usage_only',usage=incoming);db.refresh(run)
    assert not run.usage.get('embedding_calls') and not run.usage['calls'] and not run.usage['total_tokens']
    execution.unregister(run.run_id)


def test_empty_retrieval_cannot_inherit_another_threads_previous_cost(v2db,monkeypatch):
    db,customer,session=v2db
    transport._embedding_observation.value={'http_attempts':1,'total_tokens':999,'usage_estimated':False}
    def retrieve(reader,package,*args):nohits(package);return {}
    monkeypatch.setattr(evidence,'retrieve',retrieve)
    run=execute(db,customer.id,'assessment',adapter=Adapter(),session_id=session.id)
    assert run.status=='succeeded' and run.usage['embedding']['http_attempts']==0
    assert run.usage['embedding']['total_tokens']==0


def test_adapter_error_after_entry_remains_estimated_not_reported_free():
    class Failing:
        def chat_completion_once(self,*args,**kwargs):raise transport.LLMUnavailableError('synthetic error')
    planner=Planner(Failing(),token_budget=100000,output_limit=2000,deadline=time.monotonic()+20)
    with pytest.raises(transport.LLMUnavailableError):planner.call('plan',{})
    usage=planner.usage()
    assert usage['llm_call_count']==1 and usage['total_tokens']>0 and usage['usage_estimated']
    assert usage['calls'][0]['attempt_state']=='adapter_started'


@pytest.mark.parametrize('queued',[False,True])
def test_embedding_gate_stops_queue_release_or_retry_without_next_http(monkeypatch,queued):
    adapter=transport.CompatAdapter(name='synthetic',api_key='synthetic',base_url='https://invalid.test',model='synthetic',max_retries=2)
    posts=[];stop=threading.Event();transport.reset_embedding_usage()
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):
            posts.append(True);stop.set()
            return SimpleNamespace(status_code=503,text='synthetic failure')
    class Queue:
        def __enter__(self):stop.set()
        def __exit__(self,*args):pass
    monkeypatch.setattr(transport.httpx,'Client',Client)
    monkeypatch.setattr(transport.time,'sleep',lambda seconds:None)
    if queued:monkeypatch.setattr(transport,'_EMBEDDING_SEM',Queue())
    def gate():
        if stop.is_set():raise ProtocolError('cancelled')
    with transport.embedding_request_scope(gate),pytest.raises(ProtocolError,match='cancelled'):
        adapter.embed(['synthetic input'])
    observed=transport.get_embedding_usage()
    assert len(posts)==observed['http_attempts']==int(not queued)
    if queued:assert observed['total_tokens']==0 and not observed['usage_estimated']
    assert getattr(transport._embedding_observation,'before_http',None) is None


def test_evidence_preserves_controlled_stop_and_observed_cost(monkeypatch):
    def retrieve(*args,**kwargs):raise ProtocolError('run_missing')
    monkeypatch.setattr(evidence.KnowledgeRetriever,'retrieve',retrieve)
    transport.reset_embedding_usage();package={}
    with pytest.raises(ProtocolError,match='run_missing'):evidence.retrieve(None,package,'synthetic',None)
    assert package['retrieval_usage']=={} and package.get('retrieval_status')!='unavailable'
