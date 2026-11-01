"""Independent behavioral witnesses for A1/D1. No network or production data."""
from copy import deepcopy
from itertools import combinations
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from test_ai_protocol import v2db, valid_plan
from test_ai_policy_scope import row
from services.ai.catalog import build_catalog
from services.ai.completion import certificate_schema
from services.ai.open_plan import repair_envelope
from services.ai.contracts import build_contract
from services.ai.facts import capture
from services.ai.protocol import ProtocolError, load_assets, pointer_get
from services.ai.renderer import render
from services.ai.validator import validate


def intent(package,kind,**changes):
    value=dict(kind=kind,subject_refs=[package['customer_ref']],field_refs=[],option_refs=[],source_refs=[],count=None)
    value.update(changes)
    return value


def fixture(v2db,*,source_count=1,kinds=()):
    db,customer,_=v2db
    package=capture(db.get_bind(),customer.id)
    package['evidence']=[row(chunk=71+i,document=71+i,item=71+i,title=f'制度{i+1}') for i in range(source_count)]
    package['retrieval_status']='available' if source_count else 'no_hits'
    for e in package['evidence']:
        ref='src:'+str(e['chunk_id'])
        package['sources'][ref]=dict(id=ref,kind='knowledge',record_ref='knowledge:'+str(e['chunk_id']),
            content_hash=e['content_hash'],visibility_state='active',source_class='general_knowledge')
    catalog=build_catalog(package)
    tasks=[intent(package,'policy',source_refs=['src:'+str(e['chunk_id']) for e in package['evidence']])]
    tasks += [intent(package,kind,count=2 if kind=='verify_questions' else None) for kind in kinds]
    contract=build_contract(package,catalog,'free_qa',intents=tasks)
    plan=valid_plan(package,catalog,contract);plan['detail_refs']=[]
    return package,catalog,contract,load_assets()[1],plan


def repair(plan,package,catalog,contract,wire,**kwargs):
    envelope=build_envelope(plan,package,catalog,contract,wire,**kwargs)
    assert envelope.search['mode']=='open_structural'
    good = valid_plan(package,catalog,contract)
    def value(slot):
        if 'const' in slot['schema']:
            return deepcopy(slot['schema']['const'])
        path = envelope.paths[slot['slot_id']]
        candidate = deepcopy(pointer_get(good, path))
        if path and path[0] == 'actions' and isinstance(candidate, dict):
            candidate['id'] = slot['schema']['properties']['id'].get('const', candidate['id'])
        return candidate
    response=dict(base_plan_hash=envelope.base_hash,replacements=[
        dict(slot_id=s['slot_id'],value=value(s)) for s in envelope.slots])
    # The merge order is not allowed to influence preassigned append positions.
    response['replacements'].reverse()
    merged=envelope.merge(response)
    assert not validate(merged,package,catalog,contract,wire,assigned_local_ids=envelope.assigned_local_ids)
    assert Draft202012Validator(certificate_schema(wire,envelope.assigned_local_ids)).is_valid(merged)
    return envelope,merged,response


def build_envelope(plan,package,catalog,contract,wire):
    return repair_envelope(plan,package,catalog,contract,wire,validate(plan,package,catalog,contract,wire))


def test_fact_and_open_text_repairs_are_independent(v2db):
    package,catalog,contract,wire,plan=fixture(v2db)
    plan['detail_refs']=[dict(section='invalid',render_ref='r:evidence_status')]
    del plan['analysis']['summary']
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert ('analysis','summary') in env.paths.values()
    assert len(merged['detail_refs'])==2
    assert merged['analysis']['summary']==valid_plan(package,catalog,contract)['analysis']['summary']


@pytest.mark.parametrize('source_count',[0,1,2,5])
def test_policy_missing_status_and_all_quotes_has_minimal_joint_slots(v2db,source_count):
    package,catalog,contract,wire,plan=fixture(v2db,source_count=source_count)
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert len(env.slots)==source_count+1
    assert {env.paths[s['slot_id']][0] for s in env.slots}=={'detail_refs'}
    assert len(merged['detail_refs'])==source_count+1
    assert merged['summary_refs']==plan['summary_refs']
    result=render(merged,package,catalog,contract)
    assert len(result['references'])==source_count
    assert '尚未确认本客户适用性' in result['content'] if source_count else catalog['render_nodes']['r:evidence_status']['label'] in result['content']


@pytest.mark.parametrize('existing',['status','quote'])
def test_only_missing_policy_piece_is_appended(v2db,existing):
    package,catalog,contract,wire,plan=fixture(v2db)
    original=dict(section='policy',render_ref='r:evidence_status' if existing=='status' else 'r:knowledge71')
    plan['detail_refs']=[original]
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert len(env.slots)==1 and next(iter(env.paths.values()))==('detail_refs','1')
    assert merged['detail_refs'][0]==original


def test_existing_invalid_quote_slot_is_reused_before_status_append(v2db):
    package,catalog,contract,wire,plan=fixture(v2db)
    plan['detail_refs']=[dict(section='policy',render_ref='r:unknown')]
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert len(env.slots)==2
    assert {tuple(p) for p in env.paths.values()}=={('detail_refs','0'),('detail_refs','1')}
    assert len(merged['detail_refs'])==2


def test_wrong_quote_section_keeps_known_quote_and_valid_state(v2db):
    package,catalog,contract,wire,plan=fixture(v2db)
    state=dict(section='policy',render_ref='r:evidence_status')
    plan['detail_refs']=[state,dict(section='risk',render_ref='r:knowledge71')]
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert len(env.slots)==1 and next(iter(env.paths.values()))==('detail_refs','1')
    assert merged['detail_refs']==[state,dict(section='policy',render_ref='r:knowledge71')]


def test_legitimate_optional_brothers_are_frozen_even_if_not_in_guidance(v2db):
    package,catalog,contract,wire,plan=fixture(v2db)
    plan['detail_refs']=[dict(section='explanation',render_ref='r:factor_overview')]
    risk=next(ref for ref,n in catalog['render_nodes'].items() if 'risk' in n['sections'])
    plan['risks']=[dict(render_ref=risk,priority=1)]
    before=deepcopy(plan)
    env,merged,response=repair(plan,package,catalog,contract,wire)
    assert len(env.slots)==2
    assert merged['detail_refs'][0]==before['detail_refs'][0] and merged['risks']==before['risks']
    assert merged['strength_refs']==[]
    for mutate in [
        lambda r:r.update(strength_refs=['r:score']),
        lambda r:r['replacements'][0]['value'].update(section='explanation'),
        lambda r:r['replacements'].append(dict(slot_id='slot:invented',value={})),
        lambda r:r['replacements'].append(deepcopy(r['replacements'][0]))]:
        bad=deepcopy(response);mutate(bad)
        with pytest.raises(ProtocolError):env.merge(bad)
    # Changes to the caller-owned original cannot rewrite the envelope's base.
    plan['risks'].clear();plan['strength_refs'].append('r:score');plan['detail_refs'].reverse()
    assert env.merge(response)==merged


def test_missing_entire_detail_array_uses_one_parent_slot(v2db):
    package,catalog,contract,wire,plan=fixture(v2db)
    del plan['detail_refs']
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert list(env.paths.values())==[('detail_refs',)]
    assert len(merged['detail_refs'])==2


def test_risk_minimum_and_required_alert_are_not_double_allocated(v2db):
    db,customer,_=v2db;package=capture(db.get_bind(),customer.id);catalog=build_catalog(package)
    contract=build_contract(package,catalog,'assessment');wire=load_assets()[1]
    plan=valid_plan(package,catalog,contract);plan['risks']=[]
    env,merged,_=repair(plan,package,catalog,contract,wire)
    count=len(contract['requirements']['risks']['required_alert_refs'])
    assert count>0 and len(env.slots)==count and len(merged['risks'])==count
    assert len({r['render_ref'] for r in merged['risks']})==count


@pytest.mark.parametrize('whole_field',[False,True])
def test_action_missing_node_or_whole_array_retains_business_uniqueness(v2db,whole_field):
    db,customer,_=v2db;package=capture(db.get_bind(),customer.id);catalog=build_catalog(package)
    contract=build_contract(package,catalog,'report');wire=load_assets()[1]
    plan=valid_plan(package,catalog,contract);before=deepcopy(plan['actions'])
    if whole_field:del plan['actions']
    else:plan['actions'].pop()
    env,merged,response=repair(plan,package,catalog,contract,wire)
    assert len(merged['actions'])==3 and len({a['id'] for a in merged['actions']})==3
    if whole_field:assert ('actions',) in env.paths.values()
    else:assert merged['actions'][:2]==before[:2]
    assert all(v.startswith('a:repair') for v in env.assigned_local_ids)
    if not whole_field:
        bad=deepcopy(response);bad['replacements'][0]['value']['id']='a:repair999'
        with pytest.raises(ProtocolError):env.merge(bad)


def test_question_missing_nodes_are_distinct_and_pair_with_policy(v2db):
    package,catalog,contract,wire,plan=fixture(v2db,kinds=['verify_questions'])
    plan['verification_questions']=[]
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert len(merged['verification_questions'])==2
    assert len(set(merged['verification_questions']))==2
    assert len(merged['detail_refs'])==2
    assert len(env.slots)==4


@pytest.mark.parametrize('invalid_existing',[False,True])
def test_projection_missing_or_wrong_pair_uses_exact_targets_and_local_id(v2db,invalid_existing):
    db,customer,_=v2db
    customer.custom_fields={'kcr_01':'100%','er_01':'100%'};db.commit()
    package=capture(db.get_bind(),customer.id);catalog=build_catalog(package)
    choices=[o for o in catalog['projection_options'].values() if o['field_ref'] in ('field:kcr_01','field:er_01') and o['value']=='100%']
    task=intent(package,'projection',field_refs=[o['field_ref'] for o in choices],option_refs=[o['id'] for o in choices])
    contract=build_contract(package,catalog,'free_qa',intents=[task]);wire=load_assets()[1]
    plan=valid_plan(package,catalog,contract)
    if invalid_existing:
        plan['projections']=[dict(id='p:0',purpose='what_if',changes=[dict(field_ref=choices[0]['field_ref'],option_ref=choices[1]['id'])])]
    env,merged,response=repair(plan,package,catalog,contract,wire)
    assert len(merged['projections'])==1 and merged['projections'][0]['id'] in env.assigned_local_ids
    assert {(c['field_ref'],c['option_ref']) for c in merged['projections'][0]['changes']}=={(c['field_ref'],c['id']) for c in choices}
    bad=deepcopy(response)
    slot=next(r for r in bad['replacements'] if env.paths[r['slot_id']][0]=='projections')
    slot['value']['changes'][0]['option_ref']='option:unknown'
    with pytest.raises(ProtocolError):env.merge(bad)


def test_thirteen_projection_targets_split_into_wire_legal_groups(v2db):
    db,customer,_=v2db
    initial=capture(db.get_bind(),customer.id)
    customer.custom_fields={f['field']:next(v for v in f['input']['options'] if v is not None and v!='')
        for f in initial['factors'] if f['input']['type']!='key_person_levels'
        and any(v is not None and v!='' for v in f['input']['options'])}
    db.commit()
    package=capture(db.get_bind(),customer.id);catalog=build_catalog(package)
    by_field={}
    for option in catalog['projection_options'].values():by_field.setdefault(option['field_ref'],option)
    assert len(by_field)>=13
    choices=list(by_field.values())[:13]
    task=intent(package,'projection',field_refs=[o['field_ref'] for o in choices],option_refs=[o['id'] for o in choices])
    contract=build_contract(package,catalog,'free_qa',intents=[task]);wire=load_assets()[1]
    plan=valid_plan(package,catalog,contract)
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert len(env.slots)==2 and len(merged['projections'])==2
    assert sorted(len(p['changes']) for p in merged['projections'])==[1,12]
    assert {c['option_ref'] for p in merged['projections'] for c in p['changes']}==set(task['option_refs'])


@pytest.mark.parametrize('kind',['score_grade','trend','explain_score'])
def test_multi_intent_repair_does_not_drop_other_task(v2db,kind):
    package,catalog,contract,wire,plan=fixture(v2db,kinds=[kind])
    env,merged,_=repair(plan,package,catalog,contract,wire)
    assert len(render(merged,package,catalog,contract)['references'])==1
    if kind=='trend':assert any(v['section']=='trend' for v in merged['detail_refs'])
    if kind=='explain_score':
        shown={f for v in merged['detail_refs'] for f in catalog['render_nodes'][v['render_ref']]['fact_refs']}
        assert set(contract['requirements']['required_fact_refs'])<=shown|{'f:score'}


def test_unparsed_json_can_use_whole_plan_with_a_valid_witness(v2db):
    db,customer,_=v2db;package=capture(db.get_bind(),customer.id);catalog=build_catalog(package)
    contract=build_contract(package,catalog,'report');wire=load_assets()[1]
    env,merged,_=repair(None,package,catalog,contract,wire)
    assert list(env.paths.values())==[()]
    assert len(merged['actions'])==3


def test_proven_impossible_frozen_capacity_is_rejected(v2db):
    package,catalog,contract,wire,plan=fixture(v2db)
    fields=[ref for ref,n in catalog['render_nodes'].items() if ref.startswith('r:') and
            len(n['fact_refs'])==1 and n['fact_refs'][0].startswith('f:') and 'explanation' in n['sections']
            and n['template_id'] not in ('template:knowledge_quote','template:score_grade')]
    assert len(fields)>=24
    plan['detail_refs']=[dict(section='explanation',render_ref=r) for r in fields[:24]]
    with pytest.raises(ProtocolError,match='repair_not_satisfiable'):
        build_envelope(plan,package,catalog,contract,wire)


@pytest.mark.parametrize('expires_at', ['before_plan', 'after_repair_allocation'])
def test_real_planner_deadline_prevents_next_adapter_call(v2db, monkeypatch, expires_at):
    from services.ai import planner as planner_module, open_plan
    package,catalog,contract,wire,plan=fixture(v2db)
    assert validate(plan,package,catalog,contract,wire)  # Actual missing policy facts require repair.
    clock = {'now': 101.0 if expires_at == 'before_plan' else 99.0}
    monkeypatch.setattr(planner_module, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    allocations = []
    allocate = open_plan.repair_envelope
    def allocate_then_expire(*args, **kwargs):
        envelope = allocate(*args, **kwargs)
        assert envelope.slots  # Exercise the production fact repair allocator before expiry.
        allocations.append(envelope)
        clock['now'] = 101.0
        return envelope
    monkeypatch.setattr(open_plan, 'repair_envelope', allocate_then_expire)
    class Adapter:
        def __init__(self): self.calls = []
        def chat_completion_once(self, messages, **kwargs):
            self.calls.append(json.loads(messages[-1]['content']))
            assert len(self.calls) == 1 and 'plan_schema' in self.calls[0]
            return SimpleNamespace(content=json.dumps(plan), raw_usage={'total_tokens': 10})
    adapter = Adapter()
    planner = planner_module.Planner(adapter, token_budget=100000, output_limit=4000, deadline=100.0)
    with pytest.raises(ProtocolError, match='^timeout$'):
        planner.plan(package,catalog,contract,question='合成制度问题',history=[])
    expected = 0 if expires_at == 'before_plan' else 1
    assert len(adapter.calls) == len(allocations) == expected
    assert planner.usage()['llm_call_count'] == expected
    assert all(call['phase'] == 'plan' and call['attempt_state'] == 'settled' for call in planner.calls)


def test_small_policy_domain_matches_independent_minimum_append_oracle(v2db):
    package,catalog,contract,wire,base=fixture(v2db,source_count=2,kinds=['trend'])
    candidates=[dict(section='policy',render_ref='r:evidence_status'),
                dict(section='policy',render_ref='r:knowledge71'),
                dict(section='policy',render_ref='r:knowledge72'),
                dict(section='trend',render_ref='r:trend')]
    for bits in range(16):
        plan=deepcopy(base);plan['detail_refs']=[deepcopy(v) for i,v in enumerate(candidates) if bits&(1<<i)]
        missing=[v for v in candidates if v not in plan['detail_refs']]
        if not validate(plan,package,catalog,contract,wire):continue
        expected=None
        for count in range(len(missing)+1):
            if expected is not None:break
            for extra in combinations(missing,count):
                candidate=deepcopy(plan);candidate['detail_refs']+=list(extra)
                if not validate(candidate,package,catalog,contract,wire):expected=count;break
        assert expected is not None
        env,merged,_=repair(plan,package,catalog,contract,wire)
        assert len(env.slots)==expected
        assert merged['detail_refs'][:len(plan['detail_refs'])]==plan['detail_refs']
