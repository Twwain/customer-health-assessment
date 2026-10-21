"""Count-only evidence must not identify a missing component (synthetic live cases)."""

import pytest

from services.ai.fact_check import messages_for_check, parse_result
from services.ai.llm_adapter import get_chat_adapter
from services.ai.prompt_templates import get_internal_prompt, get_template


SOURCE = ('CI-01：业务战略与IT规划、行业挑战、痛点地图及方案映射三项完成数；'
          '客户当前值：2项完成。CI-03：核心评估标准、方案对齐、预算与决策周期三项掌握数；'
          '客户当前值：2项掌握。')
CASES = [
    ('CI-01第三项（痛点地图及方案映射）、CI-03第三项（预算与决策周期）目前均缺1项。', '', 'issues'),
    ('建议客户经理本周补齐尚未掌握的预算与决策周期。', '', 'issues'),
    ('CI-01和CI-03各缺1项，建议先核实具体缺哪项，再安排补齐。', '', 'passed'),
    ('建议核实CI-03缺失的是否为预算与决策周期。', '', 'passed'),
    ('CI-03尚未掌握预算与决策周期。',
     '客户逐项明细：CI-03核心评估标准已掌握，方案对齐已掌握，预算与决策周期未掌握。', 'passed'),
    ('CI-03尚未掌握预算与决策周期。',
     '客户逐项明细：CI-03核心评估标准未掌握，方案对齐已掌握，预算与决策周期已掌握。', 'issues'),
]


def test_count_constraint_reaches_generation_and_all_quality_prompts():
    for scenario in ('assessment', 'strategy', 'alert_analysis', 'free_qa'):
        assert '完成数量不能推断具体项目' in get_template(scenario).system
    for task in ('fact_check', 'local_patch', 'unit_review'):
        assert '完成数量不能推断具体项目' in get_internal_prompt(task)


@pytest.mark.live
@pytest.mark.parametrize('draft,details,expected', CASES)
def test_live_count_evidence(draft, details, expected):
    sources = {'customer': SOURCE}
    if details:
        sources['knowledge'] = details
    response = get_chat_adapter().chat_completion(
        messages_for_check(draft, sources), temperature=0, max_tokens=4000,
        extra={'thinking': {'type': 'disabled'}},
    )
    result = parse_result(response.content, draft, sources)
    assert result.status == expected, (result, response.content)
