"""Versioned completion predicates, independent of model instructions."""
from .protocol import fingerprint, ProtocolError
from .policy_scope import resolve_policy_scope, freeze_policy_scope, rebind_policy_scope

def build_contract(package, catalog, scenario, *, intents=None, base_contract=None, request_hash=None,
                   runtime_assets=None, policy_scope=None):
    scenario = "evaluate" if scenario == "assessment" else scenario
    if scenario not in ("evaluate", "free_qa", "report"):
        raise ProtocolError("unsupported_scenario")
    if base_contract:
        if base_contract.get('version') != '2.4':
            raise ProtocolError('needs_clarification')
        from .intent_contract import rebind_intents
        rebound = rebind_intents(base_contract, package, runtime_assets=runtime_assets) if base_contract.get('intents') else None
        rebound_scope = (rebind_policy_scope(base_contract, package)
                         if any(i.get('kind') == 'policy' for i in base_contract.get('intents', [])) else None)
        contract = build_contract(package,catalog,base_contract['scenario'],intents=rebound,
                                  request_hash=base_contract.get('request_hash'), runtime_assets=runtime_assets,
                                  policy_scope=rebound_scope)
        contract["base_contract_ref"] = base_contract["id"]
        contract.pop("id", None)
        contract["id"] = "contract:"+fingerprint([package["snapshot_id"], contract])[:32]
        return contract
    action_min, action_max = {"evaluate": (1, 3), "report": (3, 5), "free_qa": (0, 0)}[scenario]
    alert_refs = ["alert:"+a["id"] for a in sorted(package["assessment"]["alerts"],
        key=lambda a: ({"high": 0, "medium": 1, "low": 2}.get(a["level"], 3), a["id"]))[:3]] if scenario == "evaluate" else []
    req = dict(summary=dict(min=1, max=2, required_node_kinds=["engine_score"]),
        risks=dict(min=len(alert_refs), max=3, required_alert_refs=alert_refs), strengths=dict(min=0, max=2),
        actions=dict(min=action_min, max=action_max, recommended_min=1 if action_min else 0, recommended_max=3 if action_min else 0),
        questions=dict(exact=0), required_sections=["summary"], required_subjects=[package["customer_ref"]],
        required_fact_refs=["f:score"])
    if action_min:
        req["required_sections"].append("actions")
    if scenario in ("evaluate", "report"):
        req["required_sections"].append("trend")
    projection = "forbidden"
    if scenario == "free_qa":
        # QA facts are requested tools, not a mandatory assessment outline.
        req['summary']['min'] = 0
        req['summary']['required_node_kinds'] = []
        req['required_sections'] = []
        req['required_fact_refs'] = []
        if not intents:
            raise ProtocolError("needs_clarification")
        from .intent_contract import errors
        policy_scope = policy_scope if policy_scope is not None else resolve_policy_scope('', package['evidence'])
        from .intent_contract import assets
        violations=errors(dict(version=assets(runtime_assets)[1]['oneOf'][0]['properties']['version']['const'],status='resolved',clarification_code=None,intents=intents),package,
                          runtime_assets=runtime_assets,scope=policy_scope)
        if violations:
            raise violations[0]
        for intent in intents:
            kind = intent["kind"]
            if kind == "compare":
                if len(package["snapshot"]["members"]) < 2 or set(intent["subject_refs"]) != set(package["snapshot"]["members"]):
                    raise ProtocolError("needs_clarification")
                req["required_sections"].append("comparison")
                req["required_subjects"] = list(package["snapshot"]["members"])
                req["required_fact_refs"] += ["f:c"+str(p["raw"]["id"])+"_score" for p in package["comparison_members"]]
            elif intent["subject_refs"] != [package["customer_ref"]]:
                raise ProtocolError("needs_clarification")
            if kind == "compare":
                continue
            if kind == 'analysis':
                req['required_sections'].append('analysis')
                continue
            if kind == "verify_questions":
                req["questions"]["exact"] = intent["count"] or 1
                req["required_sections"].append("verification_questions")
            elif kind == "actions":
                count = intent["count"] or 1
                if count > 8:
                    raise ProtocolError("needs_clarification")
                req["actions"].update(min=count, max=count, recommended_min=1, recommended_max=min(3, count))
                req["required_sections"].append("actions")
            elif kind == "projection":
                if not intent["field_refs"] or not intent["option_refs"]:
                    raise ProtocolError("needs_clarification")
                projection = "what_if"
                req["projection_changes"] = dict(fields=intent["field_refs"], options=intent["option_refs"])
            elif kind == "policy":
                req["required_sections"].append("policy")
                req["required_fact_refs"].append("f:evidence_status")
                req["required_policy_sources"] = list(intent['source_refs'])
                req["required_policy_quote"] = bool(intent['source_refs'])
            elif kind in ("trend", "data_quality", "risk", "alerts", "score_grade", "explain_score"):
                if kind == "trend":
                    req["required_sections"].append("trend")
                if kind in ('score_grade', 'explain_score'):
                    req['summary']['min'] = 1
                    req['required_sections'].append('summary')
                    req['required_fact_refs'].append('f:score')
                if kind in ("data_quality", "explain_score"):
                    refs = intent["field_refs"] or ["field:"+f["field"] for f in package["factors"]]
                    req["required_fact_refs"] += ["f:"+r.split(":", 1)[1] for r in refs]
                    req["required_sections"].append("data_quality" if kind == "data_quality" else "explanation")
            else:
                raise ProtocolError("needs_clarification")
    allowed = [ref for group in catalog.values() for ref in group]+["src:"+str(e["chunk_id"]) for e in package["evidence"]]
    contract = dict(version="2.4", scenario=scenario, subject_refs=[package["customer_ref"]],
        intents=intents or [], requirements=req, allowed_catalog_ids=allowed, projection_policy=projection,
        budget_ref="budget:existing", base_contract_ref=None, request_hash=request_hash)
    selected=set(req.get('required_policy_sources',[]))
    if 'required_policy_sources' in req:
        excluded={ref for ref,node in catalog['render_nodes'].items()
                  if node['template_id']=='template:knowledge_quote' and not set(node['source_refs']).issubset(selected)}
        contract['allowed_catalog_ids']=[ref for ref in allowed if ref not in excluded]
    contract['policy_sources']={'src:'+str(e['chunk_id']):e['content_hash'] for e in package['evidence']
                                if 'src:'+str(e['chunk_id']) in selected}
    if 'required_policy_sources' in req:
        contract['policy_scope'] = freeze_policy_scope(policy_scope, package['evidence'], selected)
    contract['projection_targets']={}
    for intent in intents or []:
        if intent['kind']=='projection':
            for option in intent['option_refs']:
                field,index=option.split(':',1)[1].rsplit('.',1)
                factor=next(f for f in package['factors'] if f['field']==field)
                contract['projection_targets']['field:'+field]=factor['input']['options'][int(index)]
    contract["id"] = "contract:"+fingerprint([package["snapshot_id"], contract])[:32]
    return contract
