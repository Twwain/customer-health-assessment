"""A-only intent/quote regressions. Synthetic facts, no provider calls."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from test_ai_protocol import v2db, valid_plan
from models import Customer
from services.ai.facts import capture, capture_comparison
from services.ai.catalog import build_catalog
from services.ai.contracts import build_contract
from services.ai.intent_contract import compile_schema as intent_schema, errors, rebind_intents
from services.ai.protocol import ProtocolError, load_assets, schema_errors, fingerprint
from services.ai.open_plan import compile_open
from services.ai.validator import validate
from services.ai.renderer import render
from services.ai.planner import Planner, safe_model_package


def intent(package, kind, **kwargs):
    return dict(kind=kind, subject_refs=[package['customer_ref']], field_refs=[],
                option_refs=[], source_refs=[], count=None, **kwargs)


def response(*intents):
    return dict(version='2.4', status='resolved', clarification_code=None, intents=list(intents))


def evidence(package):
    package['retrieval_status']='available'
    for i,title in [(71,'单据制度'),(72,'排期制度')]:
        e=dict(chunk_id=i,document_id=i,item_id=i,title=title,category='内部规范',
               content=f'财务负责人应在{i}小时内完成核验。',source_class='general_knowledge',
               applicability='unknown',content_hash=fingerprint([i,title]))
        package['evidence'].append(e)
        package['sources']['src:'+str(i)]=dict(id='src:'+str(i),kind='knowledge',
            record_ref='knowledge:'+str(i),content_hash=e['content_hash'],
            visibility_state='active',source_class='general_knowledge')


@pytest.mark.parametrize('kind',['score_grade','explain_score','data_quality','risk',
                                'actions','verify_questions','trend','policy'])
def test_each_nonprojection_kind_has_witness_and_forbids_irrelevant_parameters(v2db,kind):
    db,c,_=v2db; package=capture(db.get_bind(),c.id)
    value=intent(package,kind); schema=intent_schema(package)
    assert Draft202012Validator(schema).is_valid(response(value))
    assert not errors(response(value),package)
    for key,bad in [('subject_refs',[]),('subject_refs',['customer:other']),
                    ('option_refs',['option:invented']),('source_refs',['src:customer']),('count',0)]:
        changed=dict(value,**{key:bad})
        assert not Draft202012Validator(schema).is_valid(response(changed))
        assert errors(response(changed),package)
    if kind not in ('explain_score','data_quality'):
        field='field:'+package['factors'][0]['field']
        assert errors(response(dict(value,field_refs=[field])),package)
    catalog=build_catalog(package)
    contract=build_contract(package,catalog,'free_qa',intents=[value])
    plan=valid_plan(package,catalog,contract)
    if kind in ('explain_score','data_quality'):
        plan['detail_refs']=[dict(section='explanation' if kind=='explain_score' else 'data_quality',
                                 render_ref='r:factor_overview')]
    if kind=='policy':plan['detail_refs']=[dict(section='policy',render_ref='r:evidence_status')]
    assert not schema_errors(plan,compile_open(load_assets()[1],package,catalog,contract))
    assert not validate(plan,package,catalog,contract,load_assets()[1])


@pytest.mark.parametrize('kind,maximum',[('actions',8),('verify_questions',10)])
def test_count_boundaries_and_diagnostic_pointer(v2db,kind,maximum):
    db,c,_=v2db;package=capture(db.get_bind(),c.id)
    for count in [None,1,maximum]:
        assert not errors(response(dict(intent(package,kind),count=count)),package)
    for count in [0,-1,maximum+1,1.5,True,'2']:
        bad=errors(response(dict(intent(package,kind),count=count)),package)
        assert any(e.pointer=='/intents/0/count' for e in bad)


def test_closed_statuses_duplicate_kinds_and_no_arbitrary_schema_values(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id)
    good=intent(package,'score_grade')
    for value in [response(),response(good,good),dict(response(good),version='2.1'),
                  dict(response(good),status='clarify'),dict(response(good),clarification_code='choose_source')]:
        assert errors(value,package)
    for status,code in [('clarify','choose_source'),('unsupported',None)]:
        assert not errors(dict(version='2.4',status=status,intents=[],clarification_code=code),package)
    branch=next(v for v in intent_schema(package)['oneOf'][0]['properties']['intents']['items']['oneOf']
                if v['properties']['kind']['const']=='policy')
    assert branch['properties']['option_refs']['items'] is False
    assert branch['properties']['option_refs']['maxItems']==0


def test_compare_requires_all_explicit_members(v2db):
    db,c,_=v2db;other=Customer(customer_name='另一个合成客户',custom_fields={});db.add(other);db.commit()
    package=capture_comparison(db.get_bind(),c.id,[other.id]);catalog=build_catalog(package)
    value=dict(intent(package,'compare'),subject_refs=list(package['snapshot']['members']))
    assert not errors(response(value),package)
    assert errors(response(dict(value,subject_refs=[package['customer_ref']])),package)
    contract=build_contract(package,catalog,'free_qa',intents=[value])
    plan=valid_plan(package,catalog,contract);plan['detail_refs']=[dict(section='comparison',render_ref='r:comparison')]
    assert not schema_errors(plan,compile_open(load_assets()[1],package,catalog,contract))
    assert not validate(plan,package,catalog,contract,load_assets()[1])


def test_projection_binding_and_scoped_plan_and_regeneration(v2db):
    db,c,_=v2db;c.custom_fields={'kcr_01':'100%','er_01':'100%'};db.commit()
    package=capture(db.get_bind(),c.id);catalog=build_catalog(package)
    opts=[o for o in catalog['projection_options'].values() if o['value']=='100%']
    assert len(opts)==2
    value=dict(intent(package,'projection'),field_refs=[o['field_ref'] for o in opts],option_refs=[o['id'] for o in opts])
    assert not errors(response(value),package)
    assert errors(response(dict(value,field_refs=value['field_refs'][:1])),package)
    assert errors(response(dict(value,option_refs=value['option_refs'][:1])),package)
    contract=build_contract(package,catalog,'free_qa',intents=[value])
    plan=valid_plan(package,catalog,contract)
    plan['projections']=[dict(id='p:0',purpose='what_if',changes=[dict(field_ref=o['field_ref'],option_ref=o['id']) for o in opts])]
    schema=compile_open(load_assets()[1],package,catalog,contract)
    assert not schema_errors(plan,schema) and not validate(plan,package,catalog,contract,load_assets()[1])
    for mutate in [lambda p:p['projections'][0].update(id='p:unknown'),
                   lambda p:p['projections'][0]['changes'].pop(),
                   lambda p:p['projections'][0]['changes'][0].update(option_ref=opts[1]['id'])]:
        bad=deepcopy(plan);mutate(bad);assert schema_errors(bad,schema)
    changed=deepcopy(package)
    for f in changed['factors']:
        if f['field'] in ('kcr_01','er_01'):f['input']['options'].reverse()
    rebound=rebind_intents(contract,changed)[0]
    for ref in rebound['option_refs']:
        name,index=ref.split(':')[1].rsplit('.',1)
        assert next(f for f in changed['factors'] if f['field']==name)['input']['options'][int(index)]=='100%'


def test_policy_selected_sources_closed_and_reference_derivation(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id);evidence(package);catalog=build_catalog(package)
    value=dict(intent(package,'policy'),source_refs=['src:71','src:72'])
    assert not errors(response(value),package,'请说明《单据制度》和《排期制度》。')
    assert errors(response(dict(value,source_refs=['src:72'])),package,'请说明《单据制度》。')
    assert errors(response(value),package,'请说明《未召回制度》。')
    contract=build_contract(package,catalog,'free_qa',intents=[value])
    plan=valid_plan(package,catalog,contract)
    plan['detail_refs']=[dict(section='policy',render_ref=r) for r in ['r:evidence_status','r:knowledge72','r:knowledge71']]
    schema=compile_open(load_assets()[1],package,catalog,contract)
    assert not schema_errors(plan,schema) and not validate(plan,package,catalog,contract,load_assets()[1])
    assert [r['id'] for r in render(plan,package,catalog,contract)['references']]==['src:72','src:71']
    bad=deepcopy(plan);bad['detail_refs'].pop()
    assert schema_errors(bad,schema) and any(e.code=='required_policy_source' for e in validate(bad,package,catalog,contract,load_assets()[1]))
    for change in ({'citation_refs': ['src:unknown']}, {'unexpected': []}):
        bad=dict(plan,**change);assert validate(bad,package,catalog,contract,load_assets()[1])
    only=dict(value,source_refs=['src:71']);restricted=build_contract(package,catalog,'free_qa',intents=[only])
    assert 'r:knowledge72' not in restricted['allowed_catalog_ids']
    assert validate(plan,package,catalog,restricted,load_assets()[1])
    minimal=valid_plan(package,catalog,build_contract(package,catalog,'report'))
    assert not render(minimal,package,catalog,build_contract(package,catalog,'report'))['references']
    duplicate=deepcopy(plan);duplicate['detail_refs'].append(dict(section='policy',render_ref='r:knowledge71'))
    assert len(render(duplicate,package,catalog,contract)['references'])==2


def test_missing_quote_repair_derives_reference_without_second_repair(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id);evidence(package);catalog=build_catalog(package)
    value=dict(intent(package,'policy'),source_refs=['src:71'])
    contract=build_contract(package,catalog,'free_qa',intents=[value])
    good=valid_plan(package,catalog,contract);good['detail_refs']=[dict(section='policy',render_ref='r:evidence_status')]
    class Adapter:
        calls=0
        def chat_completion_once(self,messages,**kwargs):
            self.calls+=1;payload=json.loads(messages[-1]['content'])
            if 'plan_schema' in payload:out=good
            else:
                envelope=payload['repair_envelope'];slots=envelope['append_slots']
                assert not envelope['replace_slots'] and len(slots)==1
                assert all('citation' not in str(s) for s in slots)
                out=dict(base_plan_hash=envelope['base_plan_hash'],replacements=[dict(slot_id=slots[0]['slot_id'],
                    value=dict(section='policy',render_ref='r:knowledge71'))])
            return SimpleNamespace(content=json.dumps(out),raw_usage={'total_tokens':10})
    adapter=Adapter();planner=Planner(adapter,token_budget=100000,output_limit=4000,deadline=time.monotonic()+20)
    result=planner.plan(package,catalog,contract,question='',history=[])
    assert adapter.calls==2 and result['detail_refs'][0]==good['detail_refs'][0]
    assert [r['id'] for r in render(result,package,catalog,contract)['references']]==['src:71']


def test_regeneration_preserves_selected_sources_and_rejects_old_contracts(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id);evidence(package);catalog=build_catalog(package)
    value=dict(intent(package,'policy'),source_refs=['src:71'])
    base=build_contract(package,catalog,'free_qa',intents=[value],request_hash='original')
    rebuilt=build_contract(package,catalog,'free_qa',base_contract=base)
    assert rebuilt['request_hash']=='original' and rebuilt['base_contract_ref']==base['id']
    assert rebuilt['requirements']['required_policy_sources']==['src:71']
    changed_binding=deepcopy(base)
    changed_binding['policy_scope']['source_bindings']['src:71']['content_hash']='changed'
    for changed,code in [(dict(base,version='2.1'),'needs_clarification'),
                          (dict(base,version='2.2'),'needs_clarification'),
                          (dict(base,version='2.3'),'needs_clarification'),
                          (changed_binding,'source_changed')]:
        with pytest.raises(ProtocolError,match=code):rebind_intents(changed,package)
    old=build_contract(package,catalog,'free_qa',intents=[intent(package,'score_grade')])
    old['version']='2.1';del old['intents'][0]['source_refs']
    with pytest.raises(ProtocolError,match='needs_clarification'):
        rebind_intents(old,package)


def test_empty_hypotheses_and_nohit_named_source_and_dlp(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id)
    value=intent(package,'policy')
    assert errors(response(value),package,'请说明《未召回制度》。')
    catalog=build_catalog(package);contract=build_contract(package,catalog,'report')
    schema=compile_open(load_assets()[1],package,catalog,contract)
    assert Draft202012Validator(schema['properties']['hypotheses']).is_valid([
        dict(statement='客户可能愿意参与试验，但尚未确认。', how_to_check='建议下一次沟通邀请评审。')])
    assert schema['properties']['projections']['items'] is False
    payload={'id':'option:kcr_01.0','question':'联系人13812345678','schema':schema}
    safe=safe_model_package(payload)
    assert safe['id']==payload['id'] and '13812345678' not in safe['question']


def test_projection_guidance_keeps_wire_limit_per_group(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id);catalog=build_catalog(package)
    contract=build_contract(package,catalog,'report');contract['projection_policy']='what_if'
    fields=['field:synthetic'+str(i) for i in range(13)]
    options=['option:synthetic'+str(i)+'.0' for i in range(13)]
    catalog['projection_options']={o:dict(id=o,field_ref=f,value='synthetic') for f,o in zip(fields,options)}
    contract['requirements']['projection_changes']=dict(fields=fields,options=options)
    base=load_assets()[1];schema=compile_open(base,package,catalog,contract)
    changes=[dict(field_ref=f,option_ref=o) for f,o in zip(fields,options)]
    groups=schema['properties']['projections']
    assert groups['items']['properties']['changes']['maxItems']==12
    bad=[dict(id='p:0',purpose='what_if',changes=changes)]
    good=[dict(id='p:0',purpose='what_if',changes=changes[:12]),dict(id='p:1',purpose='what_if',changes=changes[12:])]
    # The standalone projection schema has no unresolved refs after scoping.
    assert not Draft202012Validator(groups).is_valid(bad)
    assert Draft202012Validator(groups).is_valid(good)
    assert not Draft202012Validator(groups).is_valid(good[:1])
