"""Question-led QA keeps judgment open and fact tools explicit."""
import json

from services.ai.open_plan import compile_open
from services.ai.protocol import load_assets
from services.ai.renderer import render
from services.ai.validator import validate
from test_ai_protocol import v2db
from test_free_qa_presentation import task


def test_primary_risk_is_not_forced_to_first_alert_or_missing_data(v2db):
    package, catalog, contract, plan = task(v2db, ['risk', 'actions'])
    assert package['assessment']['alerts']
    assert contract['requirements']['risks']['required_alert_refs'] == []
    assert contract['requirements']['required_fact_refs'] == []
    assert len(plan['actions']) == 1
    plan['summary_refs'] = []
    plan['analysis']['summary'] = '最大风险：业务价值未进入续约决策。\n依据：应优先争取业务负责人共同评审，这是推进判断。'
    plan['actions'][0]['reason'] = '不应再次输出的行动依据'
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    result = render(plan, package, catalog, contract)
    points = [json.loads(line.removeprefix(':::ai-text ')) for line in result['content'].splitlines() if line]
    assert len(points) == 2 and all(point.startswith('• ') for point in points)
    assert '评分引擎预警' not in result['content']
    assert plan['actions'][0]['reason'] not in result['content']
    assert len(result['strategy_items']) == 1


def test_explicit_engine_alert_list_stays_complete_and_authoritative(v2db):
    package, catalog, contract, plan = task(v2db, ['alerts'])
    result = render(plan, package, catalog, contract)
    for node in catalog['render_nodes'].values():
        if node['template_id'] == 'template:engine_alert':
            assert node['label'] in result['content']
    assert plan['analysis']['summary'] not in result['content']
    assert not result['strategy_items']


def test_qa_generation_schema_avoids_unused_report_and_long_actions(v2db):
    package, catalog, contract, plan = task(v2db, ['analysis', 'actions'], actions={'count': 2})
    base = load_assets()[1]
    scoped = compile_open(base, package, catalog, contract)['properties']
    assert scoped['analysis']['properties']['summary']['maxLength'] == 400
    assert scoped['analysis']['properties']['risks']['maxItems'] == 0
    assert scoped['hypotheses']['maxItems'] == 0
    assert scoped['detail_refs'] == {'const': []}
    assert scoped['summary_refs'] == {'const': []}
    assert scoped['risks'] == {'const': []}
    assert scoped['actions']['maxItems'] == 2
    assert scoped['actions']['items']['properties']['steps']['maxItems'] == 3
    assert base['properties']['analysis']['properties']['summary']['maxLength'] == 2000


def test_business_judgment_is_valid_without_engine_alerts_or_length_gate(v2db):
    package, catalog, contract, plan = task(v2db, ['analysis'])
    plan['summary_refs'] = []
    # Concision guides generation; style does not make an otherwise valid
    # business judgment a failed fact validation or trigger model repair.
    plan['analysis']['summary'] = '这是条件判断，不是确认的业务事实。' * 30
    assert len(plan['analysis']['summary']) > 400
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    assert not render(plan, package, catalog, contract)['strategy_items']
