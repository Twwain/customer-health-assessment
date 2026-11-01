"""Explicit emergency output from frozen server facts, without model drafts."""
from .catalog import build_catalog, quoted
from .contracts import build_contract
from .renderer import render


def build_fallback(package, scenario, *, runtime_assets=None):
    from . import protocol
    runtime_assets = runtime_assets or protocol.load_runtime_assets()
    catalog = build_catalog(package)
    # Emergency output makes no claim to have answered an arbitrary QA intent.
    mode = "report" if scenario == "report" else "evaluate"
    contract = build_contract(package, catalog, mode, runtime_assets=runtime_assets)
    actions = [dict(id=f'a:{i}', title='复查客户记录', priority='recommended' if i == 0 else 'alternative',
        reason='规则兜底建议。',
        steps=[f"核实{quoted(factor['label'])}的统计时点、范围及支撑记录，取得确认后更新该项。"],
        suggested_owner='客户经理', suggested_timing='恢复分析服务后', deliverable='更新记录',
        expected_outcome='供后续评估参考。', depends_on=[], source_refs=[])
        for i, factor in enumerate(package['factors'][:contract['requirements']['actions']['min']])]
    required = set(contract["requirements"]["risks"]["required_alert_refs"])
    risk_nodes = [n for n in catalog["render_nodes"].values()
                  if "risk" in n["sections"] and required.intersection(n["coverage_keys"])]
    risks = [dict(render_ref=n["id"], priority=i+1) for i, n in enumerate(risk_nodes)]
    plan = dict(schema_version=runtime_assets.schema['properties']['schema_version']['const'], snapshot_id=package["snapshot_id"], request_contract_id=contract["id"],
        summary_refs=["r:score"], risks=risks, strength_refs=[], detail_refs=[dict(section="trend", render_ref="r:trend")],
        hypotheses=[], actions=actions, verification_questions=[], projections=[])
    plan.update(analysis=dict(summary='模型服务不可用，以下仅为规则兜底。', risks=[], strengths=[]), citation_refs=[])
    from .validator import validate
    if validate(plan, package, catalog, contract, runtime_assets.schema):
        return None
    result = render(plan, package, catalog, contract)
    if scenario == 'free_qa':
        result['content'] = 'AI 服务不可用，以下仅为规则兜底建议，未完成本次问答。'
    else:
        result["content"] = "> AI 服务不可用，以下为规则兜底，未完成本次 AI 任务。\n\n"+result["content"]
    result["degraded"] = True
    return plan, result, contract
