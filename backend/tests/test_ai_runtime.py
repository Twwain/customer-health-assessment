"""Request-scoped assets, DLP and private diagnostic boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from test_ai_protocol import v2db, Adapter
from services.ai.protocol import load_runtime_assets,ProtocolError
from services.ai.planner import Planner,safe_model_package
from services.ai.facts import capture
from services.ai.catalog import build_catalog
from services.ai.contracts import build_contract


def test_pinned_request_uses_approved_assets_without_any_later_disk_reads(v2db,monkeypatch):
    from services.ai import protocol
    db,c,_=v2db;package=capture(db.get_bind(),c.id);assets=load_runtime_assets()
    planner=Planner(Adapter(),runtime_assets=assets,token_budget=100000,output_limit=4000,deadline=time.monotonic()+20)
    def no_read():raise AssertionError('request reloaded assets')
    monkeypatch.setattr(protocol,'load_runtime_assets',no_read)
    catalog=build_catalog(package)
    contract=build_contract(package,catalog,'report',runtime_assets=assets)
    plan=planner.plan(package,catalog,contract,question='',history=[])
    assert plan['schema_version']=='2.4' and len(planner.calls)==1
    assert planner.manifest['prompts_version']=='2.4'


def test_scope_titles_are_untrusted_but_server_document_ids_are_not_corrupted():
    payload=dict(source_policy=dict(document_groups=[
        dict(document_ref='knowledge_document:ab13812345678cd',titles=['source:13812345678'])]))
    result=safe_model_package(payload)
    assert result['source_policy']['document_groups'][0]['document_ref']=='knowledge_document:ab13812345678cd'
    assert '13812345678' not in result['source_policy']['document_groups'][0]['titles'][0]


def test_request_diagnostics_have_one_global_bound_and_safe_types(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id);assets=load_runtime_assets()
    planner=Planner(Adapter(),runtime_assets=assets,token_budget=100000,output_limit=4000,deadline=time.monotonic()+20)
    planner.fact_package=package
    private='任意模型私文13812345678'
    errors=[ProtocolError('invalid_intent_reference','/intents/0/option_refs',['field:'+f['field'] for f in package['factors']])]*100
    for phase in ('intent','plan','repair'):
        planner.record_parameters({'intents':[dict(option_refs=[private])]},errors,phase)
    usage=planner.usage();raw=json.dumps(usage['parameter_diagnostics'],ensure_ascii=False,separators=(',',':'))
    assert len(usage['parameter_diagnostics'])<=24 and len(raw.encode())<=8192
    assert private not in raw and '13812345678' not in raw and usage['diagnostic_overflow']>0
    for shape in usage['parameter_diagnostics']:
        Draft202012Validator(assets.diagnostic_schema).validate(shape)
        assert shape['pointer']=='/intents/0/option_refs' and shape['json_type']=='array'


def test_invalid_json_is_only_hashed_and_checkpoint_failure_blocks_adapter(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id)
    class Broken:
        calls=0
        def chat_completion_once(self,*args,**kwargs):
            self.calls+=1
            return SimpleNamespace(content='{"contact":"13812345678",',raw_usage={'total_tokens':10})
    adapter=Broken()
    planner=Planner(adapter,token_budget=50000,output_limit=4000,deadline=time.monotonic()+20)
    planner.fact_package=package
    with pytest.raises(ProtocolError,match='invalid_json'):planner.call('intent',{})
    assert adapter.calls==1 and planner.parameter_diagnostics[0]['json_type']=='invalid_json'
    assert '13812345678' not in json.dumps(planner.usage())
    def fail(*args):raise ProtocolError('audit_persist_failed')
    blocked=Broken();planner=Planner(blocked,token_budget=50000,output_limit=4000,
        deadline=time.monotonic()+20,on_checkpoint=fail)
    with pytest.raises(ProtocolError,match='audit_persist_failed'):planner.call('intent',{})
    assert blocked.calls==0


def test_retired_candidate_references_are_not_exposed_in_diagnostics(v2db):
    from services.ai.diagnostics import parameter_shapes
    db,c,_=v2db;package=capture(db.get_bind(),c.id)
    field=package['factors'][0]['field']
    retired=['candidate:'+field,'question:'+field]
    value=dict(actions=[dict(candidate_ref=retired[0],owner_role_ref='PRIVATE_OWNER')])
    error=ProtocolError('invalid_reference','/actions/0/candidate_ref',retired)
    shape=parameter_shapes(value,[error],package)[0]
    Draft202012Validator(load_runtime_assets().diagnostic_schema).validate(shape)
    assert shape['pointer']=='/actions/0' and shape['allowed_ids']==[]
    assert shape['unknown_key_count']==2
    assert all(text not in json.dumps(shape) for text in retired+['PRIVATE_OWNER'])


def test_fallback_is_request_pinned(v2db,monkeypatch):
    from services.ai import protocol
    from services.ai.fallback import build_fallback
    db,c,_=v2db;package=capture(db.get_bind(),c.id);assets=load_runtime_assets()
    monkeypatch.setattr(protocol,'load_runtime_assets',lambda:pytest.fail('fallback reloaded assets'))
    value=build_fallback(package,'report',runtime_assets=assets)
    assert value and value[1]['degraded'] and value[0]['schema_version']=='2.4'


def test_committed_old_result_reads_without_loading_old_runtime_assets(v2db,monkeypatch):
    from types import SimpleNamespace
    from services.ai import protocol
    from services.ai.repository import available_result
    db,c,_=v2db
    old = dict(content='旧版已提交结果', strategy_items=[], references=[], contract={'version': '2.3'})
    run = SimpleNamespace(customer_id=c.id, status='succeeded', fact_package={}, validated_result=old)
    monkeypatch.setattr(protocol,'load_runtime_assets',lambda:pytest.fail('history reloaded assets'))
    assert available_result(db,run,customer_id=c.id)==old
    assert available_result(db,run,customer_id=c.id+1) is None


def test_failed_run_with_stale_result_is_not_published(v2db):
    from services.ai.repository import available_result, is_published
    db,c,_=v2db
    run=SimpleNamespace(customer_id=c.id,status='dependency_unavailable',fact_package={},
                        validated_result=dict(content='stale',degraded=True))
    assert not is_published(run)
    assert available_result(db,run,customer_id=c.id) is None
