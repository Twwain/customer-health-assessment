"""Deterministic validation. No model judgments and no prose correction."""
from __future__ import annotations

from .protocol import ProtocolError, schema_errors
from .proofs import prove, fact_closure


def validate(plan, package, catalog, contract, schema, assigned_local_ids=()):
    from .open_plan import validate_open
    return validate_open(plan, package, catalog, contract, schema, assigned_local_ids, _validate_facts)


def _validate_facts(plan, package, catalog, contract, schema, assigned_local_ids=()):
    errors = schema_errors(plan, schema)
    if errors:
        if isinstance(plan, dict) and isinstance(plan.get("detail_refs"), list):
            for error in errors:
                parts = error.pointer.strip("/").split("/")
                if len(parts) >= 2 and parts[0] == "detail_refs" and parts[1].isdigit():
                    index = int(parts[1])
                    if index < len(plan["detail_refs"]) and isinstance(plan["detail_refs"][index], dict):
                        ref = plan["detail_refs"][index].get("render_ref")
                        node = catalog["render_nodes"].get(ref) if isinstance(ref, str) else None
                        if node and set(node["sections"]) & {"trend", "policy", "explanation", "comparison", "data_quality"}:
                            error.allowed_refs = [ref]
        return errors
    nodes = catalog["render_nodes"]
    proof_dependencies, displayed_facts, coverage, sections = set(), set(), set(), set()

    def reject(code, pointer, allowed=(), dependencies=()):
        errors.append(ProtocolError(code, pointer, allowed, dependencies))

    if plan["snapshot_id"] != package["snapshot_id"]:
        reject("snapshot_mismatch", "/snapshot_id", [package["snapshot_id"]])
    if plan["request_contract_id"] != contract["id"]:
        reject("contract_mismatch", "/request_contract_id", [contract["id"]])
    allowed = set(contract["allowed_catalog_ids"])

    def use_node(ref, section, pointer):
        node = nodes.get(ref)
        valid_refs = [k for k, n in nodes.items() if section in n["sections"] and k in allowed]
        if not node or ref not in allowed:
            reject("unknown_reference", pointer, valid_refs)
            return
        if section not in node["sections"]:
            reject("reference_type_mismatch", pointer, valid_refs)
            return
        # Proven nodes are constructed only from frozen facts. A catalog from
        # another run cannot be accepted just because an ID happens to match.
        if not prove(node, package):
            reject("unproven_node", pointer)
            return
        try:
            proof_dependencies.update(fact_closure(package, node["fact_refs"]))
        except ProtocolError:
            reject("unproven_node", pointer)
            return
        coverage.update(node["coverage_keys"])
        # Only facts actually rendered by this registered node answer a task.
        # A count's inputs are proof dependencies, not displayed field scores.
        displayed_facts.update(node["fact_refs"])
        sections.add(section)

    for name, section in (("summary_refs", "summary"), ("strength_refs", "strength")):
        for i, ref in enumerate(plan[name]):
            use_node(ref, section, f"/{name}/{i}")
    for i, risk in enumerate(plan["risks"]):
        use_node(risk["render_ref"], "risk", f"/risks/{i}")
    if len({r["render_ref"] for r in plan["risks"]}) != len(plan["risks"]):
        reject("duplicate_risk", "/risks")
    for i, detail in enumerate(plan.get("detail_refs", [])):
        use_node(detail["render_ref"], detail["section"], f"/detail_refs/{i}")
    if len({(r["section"], r["render_ref"]) for r in plan.get("detail_refs", [])}) != len(plan.get("detail_refs", [])):
        reject("duplicate_detail", "/detail_refs")
    if plan["projections"] and contract["projection_policy"] == "forbidden":
        # Removing data is not a local replacement operation. Fail closed;
        # never silently strip an unrequested projection and call it success.
        reject("projection_forbidden", "/projections")
    projection_ids = set()
    projected_fields = set()
    projected_options = set()
    for i, projection in enumerate(plan["projections"]):
        ptr = f"/projections/{i}"
        if projection["id"] not in ({f"p:{i}" for i in range(8)} | set(assigned_local_ids)) or projection["id"] in projection_ids:
            reject("invalid_local_id", ptr)
        projection_ids.add(projection["id"])
        changes = {}
        for change in projection["changes"]:
            option = catalog["projection_options"].get(change["option_ref"])
            if not option or option["field_ref"] != change["field_ref"] or change["field_ref"] in changes:
                reject("invalid_projection_target", ptr)
                continue
            changes[change["field_ref"]] = option["value"]
            projected_options.add(change["option_ref"])
        projected_fields.update(changes)
        if contract["projection_policy"] != "forbidden":
            from .projection import project
            try:
                project(package, {k.split(":", 1)[1]: v for k, v in changes.items()})
            except (ValueError, ProtocolError):
                reject("invalid_projection_baseline", ptr)
    req = contract["requirements"]
    if contract["projection_policy"] == "what_if":
        expected = req["projection_changes"]
        if projected_fields != set(expected["fields"]) or projected_options != set(expected["options"]):
            reject("projection_incomplete", "/projections")
    for name, key in (("summary", "summary_refs"), ("risks", "risks"), ("strengths", "strength_refs")):
        size, limit = len(plan[key]), req[name]
        for index in range(size, limit["min"]):
            reject("contract_incomplete", f"/{key}/{index}")
        if size > limit["max"]:
            reject("contract_exceeded", "/"+key)
    # Missing coverage gets new append slots, preserving all existing elements.
    missing_alerts = set(req["risks"]["required_alert_refs"]) - coverage
    for index, ref in enumerate(sorted(missing_alerts), start=len(plan["risks"])):
        reject("required_alert_missing", f"/risks/{index}", [k for k, n in nodes.items() if ref in n["coverage_keys"]])
    # Diagnostics describe obligations; joint allocation owns actual positions.
    for section in req["required_sections"]:
        if section not in sections:
            if section in ("trend", "policy", "explanation", "data_quality", "comparison"):
                if "detail_refs" not in plan:
                    reject("required_section_missing", "/detail_refs", [r for r,n in nodes.items() if r in allowed and section in n["sections"]])
                else:
                    reject("required_section_missing", "/detail_refs", [r for r,n in nodes.items() if r in allowed and section in n["sections"]])
    for ref in req["required_fact_refs"]:
        if ref not in displayed_facts:
            if "detail_refs" not in plan:
                reject("required_fact_missing", "/detail_refs")
            else:
                reject("required_fact_missing", "/detail_refs", [k for k, n in nodes.items() if k in allowed and ref in n["fact_refs"]])
    selected_policy = [nodes[d["render_ref"]] for d in plan.get("detail_refs", [])
                       if d["section"] == "policy" and d["render_ref"] in nodes
                       and nodes[d["render_ref"]]["template_id"] == "template:knowledge_quote"]
    if req.get("required_policy_quote") and not selected_policy:
        reject("required_policy_quote", "/detail_refs",
               [n["id"] for n in nodes.values() if n['id'] in allowed and n["template_id"] == "template:knowledge_quote"])
    selected_sources={s for n in selected_policy for s in n['source_refs']}
    for source in set(req.get('required_policy_sources',[]))-selected_sources:
        reject('required_policy_source','/detail_refs',
               [n['id'] for n in nodes.values() if n['id'] in allowed and n['template_id']=='template:knowledge_quote' and source in n['source_refs']])
    # No fabricated strength may be added to satisfy a minimum. Zero is
    # represented by a fixed server block in the validated result.
    unique = {}
    for e in errors:
        unique[(e.code, e.pointer, tuple(e.allowed_refs))] = e
    return list(unique.values())
