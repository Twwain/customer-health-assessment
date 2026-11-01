"""Bounded joint repair of facts and projections, preserving valid siblings."""
from copy import deepcopy
import time

from jsonschema import Draft202012Validator

from .protocol import ProtocolError, RepairEnvelope
from .proofs import prove
from .validator import _validate_facts

DETAILS={'trend','policy','explanation','comparison','data_quality'}
ARRAYS=('summary_refs','risks','strength_refs','detail_refs','projections')


def certificate_schema(wire, assigned):
    schema=deepcopy(wire)
    for field,prefix in (('actions','a'),('projections','p')):
        schema['properties'][field]['items']['properties']['id']={
            'enum':[f'{prefix}:{i}' for i in range(8)]+[v for v in assigned if v.startswith(prefix+':repair')]}
    return schema


def _property(wire,name):
    value=wire['properties'][name]
    return wire['$defs'][value['$ref'].split('/')[-1]] if '$ref' in value else value


def _valid_schema(value,schema,wire):
    return Draft202012Validator(dict(schema,**{'$defs':wire.get('$defs',{})})).is_valid(value)


def _coverage(plan,nodes):
    facts,sections,alerts,sources=set(),set(),set(),set()
    def use(ref,section):
        n=nodes[ref];facts.update(n['fact_refs']);sections.add(section);alerts.update(n['coverage_keys'])
        if section=='policy' and n['template_id']=='template:knowledge_quote':sources.update(n['source_refs'])
    for field,section in (('summary_refs','summary'),('strength_refs','strength')):
        for ref in plan[field]:use(ref,section)
    for r in plan['risks']:use(r['render_ref'],'risk')
    for r in plan.get('detail_refs',[]):use(r['render_ref'],r['section'])
    return facts,sections,alerts,sources


def build_envelope(plan,package,catalog,contract,wire,*,deadline=None):
    """Only expose a repair after a full valid witness exists.

    Valid optional siblings use the authority's domain. New slots use the
    request's domain. No error-count-to-position allocation is performed.
    """
    began=time.monotonic();until=min(deadline or float('inf'),began+1.0);expansions=0
    def tick():
        nonlocal expansions
        expansions+=1
        if expansions>20000 or time.monotonic()>=until:
            raise ProtocolError('repair_search_exhausted')
    nodes=catalog['render_nodes'];allowed=set(contract['allowed_catalog_ids']);req=contract['requirements']
    legal={r:n for r,n in nodes.items() if r in allowed and prove(n,package)}
    item_schemas={f:_property(wire,f).get('items',{}) for f in ARRAYS}
    root=not isinstance(plan,dict)
    if not root and set(plan)-set(wire['properties']):raise ProtocolError('repair_not_satisfiable')
    work={} if root else deepcopy(plan)
    fields=set();bad=[];assigned=[];occupied=set();allocated={}
    if isinstance(work.get('projections'),list):
        occupied.update(v.get('id') for v in work['projections'] if isinstance(v,dict) and isinstance(v.get('id'),str))
    def local(prefix,key=None):
        if key in allocated:return allocated[key]
        i=1
        while f'{prefix}:repair{i}' in occupied:i+=1
        value=f'{prefix}:repair{i}';occupied.add(value);assigned.append(value)
        if key is not None:allocated[key]=value
        return value
    constants=dict(schema_version=wire['properties']['schema_version']['const'],snapshot_id=package['snapshot_id'],request_contract_id=contract['id'])
    for key,value in constants.items():
        if work.get(key)!=value:
            work[key]=value;fields.add((key,))
    for field in ARRAYS:
        if field=='detail_refs' and field not in work and not set(req['required_sections'])&DETAILS:continue
        if not isinstance(work.get(field),list):
            work[field]=[];fields.add((field,))
        if len(work[field])>_property(wire,field).get('maxItems',24):
            raise ProtocolError('repair_not_satisfiable','/'+field)

    def node_valid(ref,section):
        return isinstance(ref,str) and ref in legal and section in legal[ref]['sections']
    def business(field,value):
        if field=='risks':return value.get('render_ref')
        if field=='detail_refs':return (value.get('section'),value.get('render_ref'))
        if field in ('summary_refs','strength_refs'):return value
        if field=='projections':return value.get('id')
        return None
    def valid(field,v):
        if not _valid_schema(v,item_schemas[field],wire):return False
        if field in ('summary_refs','strength_refs'):return node_valid(v,'summary' if field=='summary_refs' else 'strength')
        if field=='risks':return node_valid(v['render_ref'],'risk')
        if field=='detail_refs':return node_valid(v['render_ref'],v['section'])
        if field=='projections':
            if contract['projection_policy']=='forbidden' or v['id'] not in {f'p:{i}' for i in range(8)}:return False
            expected=set(req['projection_changes']['options']);seen=set();changes={}
            for change in v['changes']:
                o=catalog['projection_options'].get(change['option_ref'])
                if not o or o['id'] not in expected or o['field_ref']!=change['field_ref'] or o['field_ref'] in seen:return False
                seen.add(o['field_ref']);changes[o['field_ref'].split(':',1)[1]]=o['value']
            from .projection import project
            try:project(package,changes)
            except (ValueError,ProtocolError):return False
            return True
        return False
    for field in ARRAYS:
        seen=set()
        for i,v in enumerate(work.get(field,[])):
            good=valid(field,v)
            key=business(field,v) if good else None
            if good and key in seen:good=False
            if good:
                seen.add(key)
            else:
                bad.append((field,i,deepcopy(v),local('p',(field,i)) if field=='projections' else None))
                work[field][i]=None

    def candidates(field,current,original=None,identifier=None):
        """Lazy finite tuples; enumeration and final authority share the budget."""
        if field in ('summary_refs','strength_refs','risks','detail_refs'):
            section={'summary_refs':'summary','strength_refs':'strength','risks':'risk'}.get(field)
            sections={section} if section else set(req['required_sections'])&DETAILS
            pinned=original.get('render_ref') if field=='detail_refs' and isinstance(original,dict) else None
            if pinned not in legal:pinned=None
            if pinned:sections=set(legal[pinned]['sections'])&DETAILS
            facts,done,alerts,sources=_coverage({**current,field:[v for v in current.get(field,[]) if v is not None]},nodes) \
                if all(v is not None for f in ARRAYS if f!=field for v in current.get(f,[])) else (set(),set(),set(),set())
            missing_f=set(req['required_fact_refs'])-facts;missing_s=set(req.get('required_policy_sources',[]))-sources
            choices=[]
            for ref,n in legal.items():
                if pinned and ref!=pinned:continue
                for sec in n['sections']:
                    if sec not in sections:continue
                    value=ref if field in ('summary_refs','strength_refs') else dict(render_ref=ref,priority=1) if field=='risks' else dict(section=sec,render_ref=ref)
                    score=len(set(n['fact_refs'])&missing_f)+len(set(n['source_refs'])&missing_s)+(sec not in done)
                    choices.append((-score,ref,sec,value))
            for *_,v in sorted(choices,key=lambda c:c[:3]):tick();yield v
        elif field=='projections' and contract['projection_policy']!='forbidden':
            covered={c['option_ref'] for v in current[field] if v is not None for c in v['changes']}
            remaining=[o for o in req['projection_changes']['options'] if o not in covered]
            slots_left=sum(v is None for v in current[field])
            if len(remaining)<slots_left:return
            take=max(1,min(12,len(remaining)-max(0,slots_left-1)))
            changes=[dict(field_ref=catalog['projection_options'][o]['field_ref'],option_ref=o) for o in remaining[:take]]
            if changes:tick();yield dict(id=identifier,purpose='what_if',changes=changes)

    appended_paths=[];answer=None
    def finish(current,remaining,paths):
        tick()
        facts,sections,alerts,sources=_coverage(current,nodes)
        lower=sum(max(0,req[name]['min']-len(current[field])) for name,field in
                  (('summary','summary_refs'),('risks','risks')))
        lower+=len((set(req['required_sections'])&DETAILS)-sections)
        if contract['projection_policy']!='forbidden':
            present={c['option_ref'] for p in current['projections'] for c in p['changes']}
            lower+=(len(set(req['projection_changes']['options'])-present)+11)//12
        if remaining<lower:return None
        need=None
        for name,field in (('summary','summary_refs'),('risks','risks')):
            if len(current[field])<req[name]['min']:need=field;break
        if need is None and set(req['risks']['required_alert_refs'])-alerts:need='risks'
        if need is None and contract['projection_policy']!='forbidden':
            present={c['option_ref'] for p in current['projections'] for c in p['changes']}
            if set(req['projection_changes']['options'])-present:need='projections'
        if need is None and (set(req['required_sections'])&DETAILS-sections or set(req['required_fact_refs'])-facts or
                             set(req.get('required_policy_sources',[]))-sources):
            # Score belongs in summary; data facts and policy sources in details.
            missing=set(req['required_fact_refs'])-facts
            need='summary_refs' if any('summary' in n['sections'] and missing&set(n['fact_refs']) for n in legal.values()) else 'detail_refs'
        if need is None:
            if not _validate_facts(current,package,catalog,contract,wire,assigned_local_ids=assigned):
                return deepcopy(current),list(paths)
            return None
        if remaining<=0:return None
        limit={'summary_refs':req['summary']['max'],'risks':req['risks']['max'],'detail_refs':24,'projections':8}[need]
        if len(current.get(need,[]))>=limit:return None
        if need not in current:current[need]=[]
        index=len(current[need])
        identifier=local('p',(need,index)) if need=='projections' else None
        for v in candidates(need,current,identifier=identifier):
            if business(need,v) in {business(need,x) for x in current[need]}:continue
            if need=='risks':
                missing=set(req['risks']['required_alert_refs'])-alerts
                if missing and not missing&set(nodes[v['render_ref']]['coverage_keys']):continue
                v=dict(v,priority=min(3,index+1))
            current[need].append(v)
            result=finish(current,remaining-1,paths+[(need,str(index))])
            current[need].pop()
            if result:return result
        return None
    def replace(n,current,limit):
        tick()
        if n==len(bad):return finish(current,limit,[])
        field,index,original,identifier=bad[n]
        for v in candidates(field,current,original,identifier):
            if business(field,v) in {business(field,x) for i,x in enumerate(current[field]) if x is not None and i!=index}:continue
            current[field][index]=v
            result=replace(n+1,current,limit)
            current[field][index]=None
            if result:return result
        return None
    # Explicit finite wire capacities; no hidden unbounded domain construction.
    for limit in range(59):
        answer=replace(0,work,limit)
        if answer:break
    if not answer:raise ProtocolError('repair_not_satisfiable')
    witness,appended_paths=answer
    assigned=[v['id'] for v in witness['projections'] if ':repair' in v['id']]
    specs=[]
    changed=set(fields)|{(f,str(i)) for f,i,_,_ in bad}|set(appended_paths)
    # Whole-field slots own their children; nonexistent root has no frozen data.
    changed={()} if root else {p for p in changed if not any(q!=p and p[:len(q)]==q for q in changed)}
    from .protocol import pointer_get
    for path in sorted(changed,key=lambda p:tuple((0,int(k)) if k.isdigit() else (1,k) for k in p)):
        value=pointer_get(witness,path)
        assigned_id=value.get('id') if len(path)==2 and path[0]=='projections' and isinstance(value,dict) else None
        refs=[value['render_ref']] if isinstance(value,dict) and 'render_ref' in value else [value] if isinstance(value,str) and value.startswith('r:') else []
        specs.append(dict(path=path,schema={'const':value},allowed_refs=refs,assigned_local_id=assigned_id))
    envelope=RepairEnvelope.from_specs(plan,certificate_schema(wire,assigned),specs)
    # The same merge used for a model response proves declared tree mutations.
    response=dict(base_plan_hash=envelope.base_hash,replacements=[
        dict(slot_id=s['slot_id'],value=pointer_get(witness,envelope.paths[s['slot_id']])) for s in envelope.slots])
    merged=envelope.merge(response)
    if merged!=witness or _validate_facts(merged,package,catalog,contract,wire,assigned_local_ids=assigned):
        raise ProtocolError('catalog_contract_mismatch')
    envelope.witness=witness
    envelope.assigned_local_ids=tuple(assigned)
    envelope.search=dict(expansions=expansions,elapsed_seconds=time.monotonic()-began)
    return envelope
