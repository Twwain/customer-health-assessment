"""Open judgments with unchanged authoritative fact validation."""
from copy import deepcopy
from .protocol import ProtocolError, RepairEnvelope, schema_errors
from .proofs import prove


def model_catalog(catalog, package, contract):
    """Expose fact choices and valid citations; business actions remain open."""
    safe = {k: list(v.values()) for k, v in catalog.items()}
    safe['citation_sources'] = [dict(id='src:'+str(e['chunk_id']), title=e['title'], source_class=e['source_class']) for e in package['evidence']]
    safe['render_nodes'] = [n for n in safe['render_nodes'] if n['id'] in contract['allowed_catalog_ids']]
    return safe


def compile_open(base, package, catalog, contract):
    wire = deepcopy(base)
    props = wire['properties']
    props['snapshot_id'] = {'const': package['snapshot_id']}
    props['request_contract_id'] = {'const': contract['id']}
    allowed = set(contract['allowed_catalog_ids'])
    nodes = {k: n for k, n in catalog['render_nodes'].items() if k in allowed}
    for name, section in (('summary_refs', 'summary'), ('strength_refs', 'strength')):
        refs = [k for k, n in nodes.items() if section in n['sections']]
        props[name] = dict(type='array', uniqueItems=True, maxItems=24, items={'enum': refs} if refs else False)
    refs = [k for k, n in nodes.items() if 'risk' in n['sections']]
    props['risks']['items']['properties']['render_ref'] = {'enum': refs} if refs else False
    details = []
    for ref, node in nodes.items():
        for section in set(node['sections']) & {'trend', 'policy', 'explanation', 'comparison', 'data_quality'}:
            details.append(dict(type='object', additionalProperties=False, required=['section', 'render_ref'],
                properties={'section': {'const': section}, 'render_ref': {'const': ref}}))
    props['detail_refs']['items'] = {'oneOf': details} if details else False
    sources = ['src:'+str(e['chunk_id']) for e in package['evidence']]
    for ref_schema in (props['citation_refs'], props['actions']['items']['properties']['source_refs']):
        ref_schema['items'] = {'enum': sources} if sources else False
    for name, key in (('summary_refs', 'summary'), ('strength_refs', 'strengths'), ('risks', 'risks'), ('actions', 'actions')):
        props[name].update(minItems=contract['requirements'][key]['min'], maxItems=contract['requirements'][key]['max'])
    count = contract['requirements']['questions']['exact']
    props['verification_questions'].update(minItems=count, maxItems=count)
    if contract['scenario'] == 'free_qa':
        # QA has one question-led answer, not a second hidden assessment report.
        # Optional report slots otherwise invite invalid, unseen fact copies.
        for name in ('risks', 'strength_refs'):
            props[name] = {'const': []}
        if not contract['requirements']['summary']['min']:
            props['summary_refs'] = {'const': []}
        if not set(contract['requirements']['required_sections']).intersection(
                {'trend', 'policy', 'explanation', 'comparison', 'data_quality'}):
            props['detail_refs'] = {'const': []}
        analysis = props['analysis']['properties']
        analysis['summary'].update(maxLength=400, description='按本轮问题需要的数量给简短要点，换行分隔；单一重点通常1至2点，不凑数、不扩展次要问题，不复述完整评分、预警或报告。')
        analysis['risks']['maxItems'] = 0
        analysis['strengths']['maxItems'] = 0
        props['hypotheses']['maxItems'] = 0
        action = props['actions']['items']['properties']
        for name, limit in {'title': 40, 'reason': 120, 'suggested_owner': 60,
                            'suggested_timing': 60, 'deliverable': 80, 'expected_outcome': 100}.items():
            action[name]['maxLength'] = limit
        action['steps']['maxItems'] = 3
        action['steps']['items']['maxLength'] = 100
    req = contract['requirements']
    sections = set(req['required_sections']) & {'trend', 'policy', 'explanation', 'comparison', 'data_quality'}
    for section in sections:
        props['detail_refs'].setdefault('allOf', []).append(dict(contains=dict(properties={'section': {'const': section}}, required=['section']), minContains=1))
    for fact in req['required_fact_refs']:
        refs = [r for r, n in nodes.items() if 'summary' in n['sections'] and fact in n['fact_refs']]
        if refs:
            props['summary_refs'].setdefault('allOf', []).append(dict(contains={'enum': refs}, minContains=1))
        else:
            refs = [r for r, n in nodes.items() if fact in n['fact_refs'] and sections.intersection(n['sections'])]
            if not refs:
                raise ProtocolError('catalog_contract_mismatch')
            props['detail_refs'].setdefault('allOf', []).append(dict(contains=dict(properties={'render_ref': {'enum': refs}}, required=['render_ref']), minContains=1))
    for source in req.get('required_policy_sources', []):
        refs = [r for r, n in nodes.items() if n['template_id'] == 'template:knowledge_quote' and source in n['source_refs']]
        if not refs:
            raise ProtocolError('catalog_contract_mismatch')
        props['detail_refs'].setdefault('allOf', []).append(dict(contains=dict(properties={'render_ref': {'enum': refs}}, required=['render_ref']), minContains=1))
    for alert in req['risks']['required_alert_refs']:
        refs = [r for r, n in nodes.items() if alert in n['coverage_keys'] and 'risk' in n['sections']]
        props['risks'].setdefault('allOf', []).append(dict(contains=dict(properties={'render_ref': {'enum': refs}}, required=['render_ref']), minContains=1))
    props['actions'].setdefault('allOf', []).append(dict(contains=dict(properties={'priority': {'const': 'recommended'}}, required=['priority']),
        minContains=req['actions']['recommended_min'], maxContains=req['actions']['recommended_max']))
    if contract['projection_policy'] == 'forbidden':
        props['projections'].update(maxItems=0, items=False)
    else:
        changes = []
        for ref in req['projection_changes']['options']:
            option = catalog['projection_options'].get(ref)
            if not option:
                raise ProtocolError('catalog_contract_mismatch')
            changes.append(dict(type='object', additionalProperties=False, required=['field_ref', 'option_ref'],
                properties={'field_ref': {'const': option['field_ref']}, 'option_ref': {'const': ref}}))
        projection = props['projections']['items']['properties']
        projection['id'] = {'enum': [f'p:{i}' for i in range(8)]}
        projection['purpose'] = {'const': 'what_if'}
        projection['changes'].update(items={'oneOf': changes}, uniqueItems=True, minItems=1, maxItems=min(12, len(changes)))
        props['projections'].update(minItems=1, maxItems=min(8, len(changes)))
        props['projections']['allOf'] = [dict(contains=dict(properties=dict(changes=dict(contains=change, minContains=1)), required=['changes']), minContains=1) for change in changes]
    return wire


def validate_open(plan, package, catalog, contract, schema, assigned, fact_validator):
    errors = schema_errors(plan, schema)
    if errors:
        return errors
    factual = deepcopy(plan)
    factual.update(actions=[], hypotheses=[], verification_questions=[])
    fact_contract = deepcopy(contract)
    req = fact_contract['requirements']
    req['actions'] = dict(min=0, max=0, recommended_min=0, recommended_max=0)
    req['questions'] = dict(exact=0)
    req['required_sections'] = [s for s in req['required_sections'] if s not in ('actions', 'verification_questions', 'analysis')]
    errors = fact_validator(factual, package, catalog, fact_contract, schema, assigned)
    def reject(code, path, allowed=()):
        errors.append(ProtocolError(code, path, allowed))
    sources = {'src:'+str(e['chunk_id']) for e in package['evidence']}
    for prefix, refs in [('/citation_refs', plan['citation_refs'])] + [(f'/actions/{i}/source_refs', a['source_refs']) for i, a in enumerate(plan['actions'])]:
        for i, ref in enumerate(refs):
            if ref not in sources:
                reject('unknown_source', f'{prefix}/{i}', sorted(sources))
    ids = [a['id'] for a in plan['actions']]
    if len(ids) != len(set(ids)):
        reject('action_dependency_invalid', '/actions')
    graph = {a['id']: a['depends_on'] for a in plan['actions']}
    def visit(node, path):
        if node not in graph or node in path:
            raise ValueError
        for dep in graph[node]:
            visit(dep, path | {node})
    try:
        for node in graph:
            visit(node, set())
    except ValueError:
        reject('action_dependency_invalid', '/actions')
    size = len(plan['actions']); limits = contract['requirements']['actions']
    for index in range(size, limits['min']):
        reject('contract_incomplete', f'/actions/{index}')
    if size > limits['max']:
        reject('contract_exceeded', '/actions')
    recommended = sum(a['priority'] == 'recommended' for a in plan['actions'])
    if recommended > limits['recommended_max'] or (size >= limits['min'] and recommended < limits['recommended_min']):
        reject('recommendation_count', '/actions')
    count = contract['requirements']['questions']['exact']
    for index in range(len(plan['verification_questions']), count):
        reject('contract_incomplete', f'/verification_questions/{index}')
    if len(plan['verification_questions']) > count:
        reject('contract_exceeded', '/verification_questions')
    return errors


def preflight(package, catalog, contract):
    """Only determine whether required facts can be shown; never prove prose."""
    legal = [n for k, n in catalog['render_nodes'].items() if k in contract['allowed_catalog_ids'] and prove(n, package)]
    facts = {ref for n in legal for ref in n['fact_refs']}
    coverage = {ref for n in legal for ref in n['coverage_keys']}
    sections = {s for n in legal for s in n['sections']}
    req = contract['requirements']
    needed_sections = set(req['required_sections']) - {'actions', 'verification_questions', 'analysis'}
    if (not set(req['required_fact_refs']) <= facts or not set(req['risks']['required_alert_refs']) <= coverage
            or not needed_sections <= sections):
        raise ProtocolError('catalog_contract_mismatch')
    sources = {s for n in legal if n['template_id'] == 'template:knowledge_quote' for s in n['source_refs']}
    if not set(req.get('required_policy_sources', [])) <= sources:
        raise ProtocolError('catalog_contract_mismatch')
    if contract['projection_policy'] != 'forbidden':
        options = [catalog['projection_options'].get(ref) for ref in req['projection_changes']['options']]
        if any(o is None for o in options) or {o['field_ref'] for o in options} != set(req['projection_changes']['fields']):
            raise ProtocolError('catalog_contract_mismatch')


def repair_envelope(plan, package, catalog, contract, schema, errors):
    wire = compile_open(schema, package, catalog, contract)
    assigned = []
    if not isinstance(plan, dict):
        specs = [dict(path=(), schema=wire)]
    else:
        specs = []
        fact_fields = {'summary_refs', 'risks', 'strength_refs', 'detail_refs', 'projections'}
        if any(e.pointer.strip('/').split('/')[0] in fact_fields for e in errors):
            # Keep the reviewed joint completion proof for deterministic facts
            # only. Open prose is neither a witness nor a proof obligation.
            from .completion import build_envelope
            fact_schema = deepcopy(schema)
            fact_plan = deepcopy(plan)
            fact_plan.update(actions=[], hypotheses=[], verification_questions=[])
            for field in ('analysis', 'citation_refs'):
                fact_schema['properties'].pop(field)
                fact_schema['required'].remove(field)
                fact_plan.pop(field, None)
            fact_contract = deepcopy(contract)
            req = fact_contract['requirements']
            req['actions'] = dict(min=0, max=0, recommended_min=0, recommended_max=0)
            req['questions'] = dict(exact=0)
            req['required_sections'] = [s for s in req['required_sections'] if s not in ('actions', 'analysis', 'verification_questions')]
            fact_env = build_envelope(fact_plan, package, catalog, fact_contract, fact_schema)
            assigned.extend(fact_env.assigned_local_ids)
            for slot in fact_env.slots:
                path = fact_env.paths[slot['slot_id']]
                if path and path[0] in fact_fields:
                    specs.append(dict(path=path, schema=slot['schema'], allowed_refs=slot['allowed_refs']))
        for error in errors:
            if error.pointer.strip('/').split('/')[0] in fact_fields:
                continue
            if error.code in ('action_dependency_invalid', 'recommendation_count'):
                raise ProtocolError('incomplete')
            parts = tuple(error.pointer.strip('/').split('/'))
            if not parts or parts == ('',):
                raise ProtocolError('incomplete')
            node = wire
            try:
                value = plan
                for part in parts:
                    if '$ref' in node:
                        node = wire['$defs'][node['$ref'].split('/')[-1]]
                    node = node['items'] if node.get('type') == 'array' else node['properties'][part]
                    try:
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    except (KeyError, IndexError):
                        value = None
                if isinstance(value, (list, dict)) and value:
                    raise ProtocolError('incomplete')
                if node is False:
                    raise ProtocolError('incomplete')
            except (KeyError, TypeError, ValueError):
                raise ProtocolError('incomplete')
            if error.allowed_refs and isinstance(node, dict) and node.get('type') == 'string':
                node = {'enum': error.allowed_refs}
            if not any(s['path'] == parts for s in specs):
                if len(parts) == 2 and parts[0] == 'actions' and value is None:
                    occupied = {a.get('id') for a in plan.get('actions', []) if isinstance(a, dict)}
                    index = 1
                    while f'a:repair{index}' in occupied | set(assigned):
                        index += 1
                    identifier = f'a:repair{index}'
                    assigned.append(identifier)
                    node = deepcopy(node)
                    node['properties']['id'] = {'const': identifier}
                specs.append(dict(path=parts, schema=node, allowed_refs=error.allowed_refs))
    envelope = RepairEnvelope.from_specs(plan, schema, specs)
    envelope.search = {'mode': 'open_structural', 'slots': len(specs)}
    envelope.assigned_local_ids = assigned
    return envelope
