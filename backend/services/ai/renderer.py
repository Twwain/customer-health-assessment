"""Render deterministic facts and safely transport validated open analysis."""
from .catalog import quoted
import json


def plain(value):
    # Decoded into a text node, never interpreted as Markdown or an instruction.
    return ':::ai-text ' + json.dumps(str(value), ensure_ascii=False)


def projection_lines(plan, package, catalog):
    from .projection import project
    lines = []
    for projection in plan['projections']:
        changes = {c['field_ref'].split(':', 1)[1]: catalog['projection_options'][c['option_ref']]['value']
                   for c in projection['changes']}
        combined = project(package, changes)
        for field, value in changes.items():
            single = project(package, {field: value})
            lines.append(f"仅将 {quoted(field)} 设为「{quoted(value)}」时，单因子变更对应总分差 {single['delta']:+g}。")
        lines.append(f"上述整组目标同时成立时，引擎重评总分 {combined['after']['total_score']:g}，整组分差 {combined['delta']:+g}。这是冻结记录的条件测算，不是现状或效果承诺。")
    return lines


def free_qa_content(plan, package, catalog, contract, projection_text):
    """Show only requested answers; all factual text still comes from server nodes."""
    from .proofs import fact_closure
    nodes = catalog['render_nodes']
    kinds = {intent['kind'] for intent in contract['intents']}
    texts, shown, quote_sources = [], set(), []

    def node(ref):
        if ref in shown:
            return
        value = nodes[ref]
        shown.add(ref)
        texts.append((value['label'], False))
        if value['template_id'] == 'template:knowledge_quote':
            quote_sources.extend(value['source_refs'])

    for intent in contract['intents']:
        kind = intent['kind']
        if kind in ('score_grade', 'explain_score') and 'compare' not in kinds:
            node('r:score')
        if kind in ('explain_score', 'data_quality'):
            fields = intent['field_refs'] or ['field:'+f['field'] for f in package['factors']]
            requested = {'f:'+field.split(':', 1)[1] for field in fields}
            if kind == 'data_quality':
                node('r:quality')
            for field in fields:
                node('r:'+field.split(':', 1)[1])
            # Derived people counts answer questions about the requested list.
            # Their dependency is relevant, while unrelated optional facts are not.
            for detail in plan.get('detail_refs', []):
                ref = detail['render_ref']
                value = nodes[ref]
                if value['template_id'] == 'template:people_counts' and requested.intersection(fact_closure(package, value['fact_refs'])):
                    node(ref)
        elif kind == 'alerts':
            if not package['assessment']['alerts']:
                texts.append(('评分引擎当前无预警。', False))
            for ref, value in nodes.items():
                if value['template_id'] == 'template:engine_alert':
                    node(ref)
        elif kind in ('trend', 'compare'):
            node('r:trend' if kind == 'trend' else 'r:comparison')
        elif kind == 'policy':
            node('r:evidence_status')
            for detail in plan.get('detail_refs', []):
                if detail['section'] == 'policy':
                    node(detail['render_ref'])
        elif kind == 'projection':
            texts.extend((text, False) for text in projection_text)
        elif kind == 'verify_questions':
            texts.extend((text, True) for text in plan['verification_questions'])

    if 'analysis' in kinds or kinds.intersection({'risk', 'policy', 'projection'}):
        texts.append((plan['analysis']['summary'], True))
    if 'actions' in kinds and not kinds.intersection({'analysis', 'risk', 'policy', 'projection'}):
        texts.extend((action['reason'], True) for action in plan['actions'])
    # Deduplicate repeated intent coverage without parsing or rewriting model text.
    lines = []
    for text, model_text in dict.fromkeys(texts):
        if not model_text:
            if text.strip():
                lines.append('- ' + text)
            continue
        for point in text.splitlines():
            if point.strip():
                # The bullet is server-authored; all model characters stay text.
                lines.append(plain('• ' + point.strip()))
    return '\n\n'.join(lines), quote_sources

def render_facts(plan, package, catalog, contract, projection_text=None):
    nodes = catalog["render_nodes"]
    intent_labels = {"score_grade": "评分和等级", "explain_score": "评分原因", "data_quality": "资料质量", "risk": "实际预警",
                     "actions": "行动建议", "verify_questions": "核实问题", "projection": "假设目标测算", "trend": "历史趋势",
                     "compare": "指定客户比较", "policy": "制度资料及适用性", "analysis": "业务分析"}
    qa_summary = "、".join(intent_labels[i["kind"]]+(f"（{i['count']}项）" if i.get("count") else "") for i in contract["intents"])
    lines = ["本次按以下任务回答："+{"evaluate": "综合评估",
             "report": "AI 报告", "free_qa": qa_summary}[contract["scenario"]], "", "### 总览"]
    lines += [nodes[r]["label"] for r in plan["summary_refs"]]
    for d in plan.get("detail_refs", []):
        lines.extend(["", nodes[d["render_ref"]]["label"]])
    alerts = package["assessment"]["alerts"]
    lines.extend(["", "### 完整预警清单"])
    lines.extend(["- 评分引擎预警："+quoted(a["message"]) for a in alerts] or ["评分引擎当前无预警。"])
    if plan["risks"]:
        lines.extend(["", "### 重点解读"]+[nodes[r["render_ref"]]["label"] for r in plan["risks"]])
    lines.extend(["", "### 优势"]+[nodes[r]["label"] for r in plan["strength_refs"]] if plan["strength_refs"] else ["", "无可确认优势。"])
    if plan["projections"]:
        lines.extend(["", "### 假设目标测算"])
        lines.extend(projection_lines(plan, package, catalog) if projection_text is None else projection_text)
    status = {"not_requested": "本次未请求外部资料", "no_hits": "检索无命中", "available": "资料仅作通用参考，尚未确认对本客户适用", "unavailable": "检索服务不可用"}[package["retrieval_status"]]
    lines.extend(["", "资料状态："+status+"。"])
    references = []
    # Only displayed knowledge quote nodes contribute bibliography entries.
    shown=[nodes[d['render_ref']] for d in plan.get('detail_refs',[])
           if nodes[d['render_ref']]['template_id']=='template:knowledge_quote']
    refs=list(dict.fromkeys(s for n in shown for s in n['source_refs']))
    for ref in refs:
        e = next(e for e in package["evidence"] if ref == "src:"+str(e["chunk_id"]))
        references.append(dict(id=ref, title=quoted(e["title"]), category=e["category"], snippet=quoted(e["content"]),
                               chunk_id=e["chunk_id"], used=True))
    return dict(content="\n".join(lines), strategy_items=[], references=references,
                assessment=package["assessment"], trend=package["trend"], contract=contract,
                fixed_blocks=dict(evidence_status="资料状态："+status+"。", strengths="无可确认优势。" if not plan["strength_refs"] else " ".join(nodes[r]["label"] for r in plan["strength_refs"])))


def render(plan, package, catalog, contract):
    from copy import deepcopy
    projection_text = projection_lines(plan, package, catalog)
    if contract['scenario'] == 'free_qa':
        content, fact_sources = free_qa_content(plan, package, catalog, contract, projection_text)
        result = dict(assessment=package['assessment'], trend=package['trend'], contract=contract)
        lines = [content]
    else:
        factual = deepcopy(plan)
        factual.update(actions=[], hypotheses=[], verification_questions=[])
        result = render_facts(factual, package, catalog, contract, projection_text)
        fact_sources = [r['id'] for r in result['references']]
        lines = [result['content'], '', '## AI 分析（业务判断，可能有误）',
                 '### 总体评估', plain(plan['analysis']['summary']), '### 主要风险点']
        lines += [plain(v) for v in plan['analysis']['risks']] or ['未提出额外风险判断。']
        lines += ['### 优势'] + ([plain(v) for v in plan['analysis']['strengths']] or ['未提出额外优势判断。'])
        if plan['hypotheses']:
            lines.append('### 待验证假设')
            for h in plan['hypotheses']:
                lines.extend([plain(h['statement']), plain('验证方式：'+h['how_to_check'])])
        if plan['verification_questions']:
            lines += ['### 核实问题']+[plain(q) for q in plan['verification_questions']]
    items = []
    titles = {a['id']: a['title'] for a in plan['actions']}
    for a in plan['actions']:
        action = '\n'.join(f'{i+1}. {s}' for i, s in enumerate(a['steps']))
        action += f"\n建议参与方：{a['suggested_owner']}；建议期限：{a['suggested_timing']}；交付物：{a['deliverable']}。"
        if a['depends_on']:
            action += '\n前置行动：'+'、'.join(titles[r] for r in a['depends_on'])
        items.append(dict(a, urgency='medium', action=action, reference=''))
    sources = list(dict.fromkeys(fact_sources+plan['citation_refs']+
                                [s for a in plan['actions'] for s in a['source_refs']]))
    evidence = {'src:'+str(e['chunk_id']): e for e in package['evidence']}
    result['references'] = [dict(id='src:'+str(e['chunk_id']), title=quoted(e['title']), category=e['category'],
        snippet=quoted(e['content']), chunk_id=e['chunk_id'], used=True) for e in (evidence[s] for s in sources)]
    result.update(content='\n'.join(lines), strategy_items=items, analysis=deepcopy(plan['analysis']),
                  hypotheses=deepcopy(plan['hypotheses']), content_format='ai_open_2_4')
    return result
