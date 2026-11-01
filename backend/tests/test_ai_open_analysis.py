"""Open judgments, reference validity and one bounded structural repair."""
from copy import deepcopy
import hashlib
import json
import time
from types import SimpleNamespace
import pytest
from test_ai_protocol import v2db, valid_plan, Adapter
from test_ai_completion import fixture
from services.ai.protocol import ASSETS, ProtocolError, load_assets
from services.ai.open_plan import repair_envelope
from services.ai.validator import validate
from services.ai.renderer import render
from services.ai.planner import Planner
from services.ai.catalog import build_catalog
from services.ai.contracts import build_contract
from services.ai.facts import capture


def test_reviewed_asset_bytes_are_exact():
    expected = {'prompts.json':'8cb0f0c475903a800e1c32fcfcc234267240ec70b2e5ec7cbc08d1d754cd4a27',
        'plan.schema.json':'dda71d800924cf4b0464bcac30e8c2cf11b215b9b90615c24906f1a3c665ced7',
        'intent.schema.json':'42395685b9549634f3896781f9eec897c404d16618cf754e48e74ffc52328fe6'}
    for name, digest in expected.items():
        assert hashlib.sha256((ASSETS/name).read_bytes()).hexdigest() == digest


def test_prompt_format_preserves_reviewed_system_text():
    expected = {
        'intent': 'f24ab2301f3c9aa0812286ea53ce5fda6aa88d1f39b091230d9e5bb8b09635e2',
        'plan': '6e02cddea981ce431c7e65a33bec98395eaedb9424f76dfe2b77b49c8c5313f7',
        'repair': 'f29a7026e27b9e25d22672b00c6f52ca470ef06b6fcf12e5716e3d5523495aae',
    }
    prompts, _, _ = load_assets()
    assert {key: hashlib.sha256(value.encode('utf-8')).hexdigest()
            for key, value in prompts.items()} == expected


@pytest.mark.parametrize('location', ['detail', 'new', 'both'])
def test_reference_union_from_actual_selected_fields(v2db, location):
    package, catalog, contract, wire, plan = fixture(v2db)
    # Report has no policy requirement but may choose valid citations.
    contract = build_contract(package, catalog, 'report')
    plan = valid_plan(package, catalog, contract)
    if location in ('detail', 'both'):
        plan['detail_refs'].append(dict(section='policy', render_ref='r:knowledge71'))
    if location in ('new', 'both'):
        plan['citation_refs'] = ['src:71']
        plan['actions'][0]['source_refs'] = ['src:71']
    assert not validate(plan, package, catalog, contract, wire)
    result = render(plan, package, catalog, contract)
    assert [r['id'] for r in result['references']] == ['src:71']
    assert result['references'][0]['snippet']


def test_real_planner_repairs_only_unknown_source_once(v2db):
    package,catalog,contract,wire,plan=fixture(v2db)
    plan['detail_refs']=[dict(section='policy',render_ref='r:evidence_status'),dict(section='policy',render_ref='r:knowledge71')]
    plan['citation_refs']=['src:missing']
    class RepairAdapter:
        def __init__(self): self.calls=[]
        def chat_completion_once(self, messages, **kwargs):
            payload=json.loads(messages[-1]['content']);self.calls.append(payload)
            if 'plan_schema' in payload:
                value=plan
            else:
                env=payload['repair_envelope'];slots=env['replace_slots']
                assert len(slots)==1
                value=dict(base_plan_hash=env['base_plan_hash'], replacements=[dict(slot_id=slots[0]['slot_id'],value='src:71')])
            return SimpleNamespace(content=json.dumps(value),raw_usage={'total_tokens':10})
    adapter=RepairAdapter()
    planner=Planner(adapter,token_budget=100000,output_limit=4000,deadline=time.monotonic()+20)
    fixed=planner.plan(package,catalog,contract,question='合成制度',history=[])
    assert fixed['analysis']==plan['analysis'] and fixed['detail_refs']==plan['detail_refs']
    assert fixed['citation_refs']==['src:71'] and len(adapter.calls)==2


def test_dependency_cycle_is_terminal_not_a_judgment_failure(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id);catalog=build_catalog(package)
    contract=build_contract(package,catalog,'report');schema=load_assets()[1]
    plan=valid_plan(package,catalog,contract);plan['actions'][0]['depends_on']=['a:0']
    errors=validate(plan,package,catalog,contract,schema)
    assert any(e.code=='action_dependency_invalid' for e in errors)
    with pytest.raises(ProtocolError,match='incomplete'):
        repair_envelope(plan,package,catalog,contract,schema,errors)


def test_analysis_intent_and_no_candidate_catalog(v2db):
    db,c,_=v2db;package=capture(db.get_bind(),c.id);catalog=build_catalog(package)
    task=dict(kind='analysis',subject_refs=[package['customer_ref']],field_refs=[],option_refs=[],source_refs=[],count=None)
    contract=build_contract(package,catalog,'free_qa',intents=[task])
    adapter=Adapter();planner=Planner(adapter,token_budget=100000,output_limit=4000,deadline=time.monotonic()+20)
    plan=planner.plan(package,catalog,contract,question='有哪些推进机会？',history=[])
    assert plan['analysis']['summary'] and len(adapter.calls)==1
    model_catalog=json.loads(adapter.calls[0][-1]['content'])['catalog']
    assert 'action_candidates' not in model_catalog and 'hypothesis_candidates' not in model_catalog
