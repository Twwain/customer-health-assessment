"""One bounded intent domain used by model guidance and authoritative checks."""
from copy import deepcopy
from jsonschema import Draft202012Validator

from .protocol import ProtocolError
from .policy_scope import (normalize_evidence, resolve_policy_scope, rebind_policy_scope,
                           source_ref)


def assets(runtime_assets=None):
    if runtime_assets is None:
        from .protocol import load_runtime_assets
        runtime_assets = load_runtime_assets()
    return runtime_assets.rules, runtime_assets.intent_schema


def domains(package):
    fields = ['field:'+f['field'] for f in package['factors']]
    options = {f"option:{f['field']}.{i}":'field:'+f['field']
               for f in package['factors'] if f['state']=='known' and f['input']['type']!='key_person_levels'
               for i,_ in enumerate(f['input']['options'])}
    sources = [source_ref(e) for e in normalize_evidence(package['evidence'])]
    return fields, options, sources


def choices(values, *, minimum=0):
    return dict(type='array',uniqueItems=True,minItems=minimum,maxItems=len(values),
                items={'enum':list(values)} if values else False)


def compile_schema(package, *, runtime_assets=None, scope=None):
    rules, original = assets(runtime_assets)
    base = deepcopy(original)
    fields, options, _ = domains(package)
    scope = scope if scope is not None else resolve_policy_scope('', package['evidence'])
    sources = scope['allowed_source_refs']
    variants=[]
    for kind, rule in rules['intent_rule_matrix'].items():
        if kind == 'policy' and scope['status'] in ('named_missing', 'named_ambiguous'):
            continue
        item=deepcopy(base['oneOf'][0]['properties']['intents']['items'])
        p=item['properties'];p['kind']={'const':kind}
        p['subject_refs']=({'const':list(package['snapshot']['members'])} if kind=='compare'
                           else {'const':[package['customer_ref']]})
        p['field_refs']=choices(fields) if rule['fields']=='optional_current_fields' else choices([])
        p['option_refs']=choices([])
        minimum = len(sources) if scope['status'] == 'named_unique' else 1 if sources else 0
        p['source_refs']=choices(sources,minimum=minimum) if kind=='policy' else choices([])
        count=rule['count']
        p['count']=({'anyOf':[{'type':'null'},{'type':'integer','minimum':count['min'],'maximum':count['max']}]}
                    if 'min' in count else {'const':None})
        if kind=='compare' and len(package['snapshot']['members'])<2:
            continue
        if kind=='projection':
            if not options:
                continue
            p['field_refs']=choices(sorted(set(options.values())),minimum=1)
            p['option_refs']=choices(list(options),minimum=1)
            # Each selected field has exactly its selected option; forbid an
            # option for an unselected field and multiple targets for a field.
            item['allOf']=[]
            for field in sorted(set(options.values())):
                bound=[o for o,f in options.items() if f==field]
                contains={'type':'array','contains':{'enum':bound},'minContains':1,'maxContains':1}
                item['allOf'].append({'if':{'properties':{'field_refs':{'contains':{'const':field}}}},
                    'then':{'properties':{'option_refs':contains}},
                    'else':{'properties':{'option_refs':{'not':{'contains':{'enum':bound}}}}}})
        variants.append(item)
    base['oneOf'][0]['properties']['intents']['items']={'oneOf':variants}
    return base


def errors(response, package, question='', *, runtime_assets=None, scope=None):
    """Return bounded protocol errors; never embed unknown model strings."""
    scope = scope if scope is not None else resolve_policy_scope(question, package['evidence'])
    if scope['status'] in ('named_missing', 'named_ambiguous') and isinstance(response, dict) and response.get('status') == 'resolved':
        intents = response.get('intents')
        if isinstance(intents, list):
            for n, intent in enumerate(intents[:10]):
                if isinstance(intent, dict) and intent.get('kind') == 'policy':
                    return [ProtocolError('needs_clarification', f'/intents/{n}/source_refs')]
    schema=compile_schema(package, runtime_assets=runtime_assets, scope=scope)
    found=[]
    for error in Draft202012Validator(schema).iter_errors(response):
        # oneOf aggregates can obscure which argument is wrong. Include leaf
        # branch errors only for the actual registered status/kind.
        found.append(ProtocolError('invalid_intent','/'+ '/'.join(map(str,error.absolute_path))))
    if found:
        # Stable direct argument pointers independent of JSONSchema oneOf text.
        if isinstance(response,dict) and isinstance(response.get('intents'),list):
            rules,_=assets(runtime_assets);fields,options,_=domains(package)
            sources=scope['allowed_source_refs']
            for n,intent in enumerate(response['intents'][:10]):
                if not isinstance(intent,dict) or not isinstance(intent.get('kind'),str) or intent['kind'] not in rules['intent_rule_matrix']:
                    continue
                kind=intent['kind'];r=rules['intent_rule_matrix'][kind]
                allowed={'subject_refs':list(package['snapshot']['members']) if kind=='compare' else [package['customer_ref']],
                    'field_refs':fields if r['fields']!='empty' else [],
                    'option_refs':list(options) if kind=='projection' else [],
                    'source_refs':sources if kind=='policy' else []}
                for key,valid in allowed.items():
                    value=intent.get(key)
                    if not isinstance(value,list) or any(not isinstance(v,str) or v not in valid for v in value):
                        found.append(ProtocolError('invalid_intent_reference',f'/intents/{n}/{key}',valid))
                    elif key=='subject_refs' and set(value)!=set(valid):
                        found.append(ProtocolError('invalid_intent_reference',f'/intents/{n}/{key}',valid))
                variant=next((v for v in schema['oneOf'][0]['properties']['intents']['items']['oneOf']
                              if v['properties']['kind']['const']==kind),None)
                if variant:
                    for key,argument in variant['properties'].items():
                        if key in intent and not Draft202012Validator(argument).is_valid(intent[key]):
                            found.append(ProtocolError('invalid_intent_argument',f'/intents/{n}/{key}'))
                    if not Draft202012Validator(variant).is_valid(intent) and kind=='projection':
                        found.append(ProtocolError('invalid_intent_argument',f'/intents/{n}/option_refs'))
        return found
    return []


def rebind_intents(base, package, *, runtime_assets=None):
    """Regenerate a current contract without substituting changed sources."""
    if base.get('version') != '2.4':
        raise ProtocolError('needs_clarification')
    intents=deepcopy(base.get('intents',[]))
    scope = (rebind_policy_scope(base, package) if any(i.get('kind') == 'policy' for i in intents)
             else resolve_policy_scope('', package['evidence']))
    fields,options,_=domains(package)
    for intent in intents:
        kind=intent['kind']
        if kind=='policy':
            if set(intent.get('source_refs', [])) != set(scope['allowed_source_refs']):
                raise ProtocolError('needs_clarification', '/intents/source_refs')
        if kind=='projection':
            targets=base.get('projection_targets')
            if not targets:
                raise ProtocolError('needs_clarification','/intents/option_refs')
            mapped=[]
            for field,value in targets.items():
                factor=next((f for f in package['factors'] if 'field:'+f['field']==field),None)
                matches=[f"option:{factor['field']}.{i}" for i,v in enumerate(factor['input']['options']) if v==value] if factor else []
                if len(matches)!=1 or matches[0] not in options:
                    raise ProtocolError('needs_clarification','/intents/option_refs')
                mapped.extend(matches)
            intent['option_refs']=mapped
    response=dict(version=assets(runtime_assets)[1]['oneOf'][0]['properties']['version']['const'],status='resolved',clarification_code=None,intents=intents)
    bad=errors(response,package,runtime_assets=runtime_assets,scope=scope)
    if bad:
        raise bad[0]
    return intents
