"""Bounded private model calls using the three reviewed system blocks."""
from __future__ import annotations

import threading
import time
import re
from copy import deepcopy
from jsonschema import Draft202012Validator

from services.ai import guardrails
from services.ai.llm_adapter import estimate_tokens
from .protocol import canonical, strict_json, ProtocolError, load_runtime_assets
from .validator import validate

from .intent_contract import assets as intent_assets, compile_schema as compile_intent_schema, domains, errors as intent_errors

def safe_model_package(payload):
    """DLP text leaves without corrupting server-issued opaque references."""
    protected = set()
    opaque = re.compile(r"(?:[a-z][a-z0-9_]*:[A-Za-z0-9_.-]+|[0-9a-f]{32,64})\Z")
    untrusted_keys = {"question", "history", "untrusted_evidence", "base_plan", "label", "content", "title", "titles", "description", "value"}

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in untrusted_keys:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str) and opaque.fullmatch(value):
            protected.add(value)

    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str) and value not in protected:
            return guardrails.sanitize_for_outbound(value)[0]
        return value

    collect(payload)
    return clean(payload)


class Planner:
    def __init__(self, adapter, *, token_budget, output_limit, deadline, cancelled=None, max_calls=2,
                 runtime_assets=None,on_checkpoint=None,on_usage=None):
        self.adapter = adapter
        self.budget, self.output_limit = token_budget, output_limit
        self.deadline, self.cancelled = deadline, cancelled or threading.Event()
        self.max_calls, self.calls, self.total = max_calls, [], 0
        self.validation_errors = []
        self.validation_shapes = []
        self.intent_resolution = {}
        self.parameter_diagnostics=[];self.parameter_overflow=0;self.fact_package={};self.scope=None;self.repair_search={}
        self.runtime_assets=runtime_assets or load_runtime_assets()
        self.prompts,self.schema,self.manifest=self.runtime_assets.prompts,self.runtime_assets.schema,self.runtime_assets.manifest
        self.on_checkpoint,self.on_usage=on_checkpoint,on_usage

    def check(self):
        if self.cancelled.is_set():
            raise ProtocolError("cancelled")
        if time.monotonic() >= self.deadline:
            raise ProtocolError("timeout")

    def record_parameters(self,value,errors,phase):
        from .diagnostics import parameter_shapes
        shapes=parameter_shapes(value,errors,self.fact_package,stage='before_'+phase)
        self.parameter_overflow+=max(0,len(errors)-len(shapes))
        for shape in shapes:
            if len(self.parameter_diagnostics)>=24 or len(canonical(self.parameter_diagnostics+[shape]).encode())>8192:
                self.parameter_overflow+=1
            else:self.parameter_diagnostics.append(shape)

    def call(self, phase, package):
        self.check()
        if len(self.calls) >= self.max_calls:
            raise ProtocolError("call_budget_exceeded")
        safe_user = canonical(safe_model_package(package))
        messages = [{"role": "system", "content": self.prompts[phase]}, {"role": "user", "content": safe_user}]
        estimate = estimate_tokens(self.prompts[phase]+safe_user)
        output = min(self.output_limit, self.budget-self.total-estimate)
        if output < 512:
            raise ProtocolError("input_budget_exceeded")
        entry = dict(phase=phase, attempt=len(self.calls)+1, usage_estimated=True,total_tokens=0,
                     attempt_state='registered',reserved_tokens=estimate)
        self.calls.append(entry)
        try:
            if self.on_checkpoint:
                if self.on_checkpoint('before_'+phase,self.usage()) is False:raise ProtocolError('source_changed')
            self.check()
        except BaseException:
            entry.update(attempt_state='not_sent',usage_estimated=False,total_tokens=0)
            if self.on_usage:self.on_usage(self.usage())
            raise
        entry.update(attempt_state='adapter_started',total_tokens=estimate)
        self.total += estimate
        try:
            result = self.adapter.chat_completion_once(messages, max_tokens=output,
                                                       cancel_event=self.cancelled, deadline=self.deadline)
            used = int(result.raw_usage.get("total_tokens") or (estimate+estimate_tokens(result.content)))
            entry.update(attempt_state='settled',usage_estimated=not bool(result.raw_usage.get("total_tokens")), total_tokens=used,
                         prompt_tokens=result.raw_usage.get("prompt_tokens"), completion_tokens=result.raw_usage.get("completion_tokens"),
                         finish_reason=getattr(result, "finish_reason", ""))
            self.total += used-estimate
        finally:
            if self.on_usage:self.on_usage(self.usage())
        self.check()
        if self.total > self.budget:
            raise ProtocolError("token_budget_exceeded")
        try:return strict_json(result.content)
        except ProtocolError as exc:
            self.record_parameters(result.content,[exc],phase)
            raise

    def resolve_intent(self, question, package, history):
        from .policy_scope import resolve_policy_scope
        self.fact_package=package
        rules, _ = intent_assets(self.runtime_assets)
        intents_registry=list(rules['intent_rule_matrix'])
        self.scope=resolve_policy_scope(question,package['evidence'])
        fixed = deepcopy(rules['user_packages']['intent']['fixed_literals'])
        fixed['subject_context']['bound_subject_ref'] = package['customer_ref']
        fields, options, sources = domains(package)
        payload=dict(version=self.manifest['user_pack_version'], intent_rules_version=self.manifest['intent_rules_version'], question=question, history=list(history),
            allowed_intents=intents_registry, allowed_subject_refs=list(package['snapshot']['members']),
            allowed_field_refs=fields, allowed_option_refs=list(options), allowed_source_refs=self.scope['allowed_source_refs'],
            count_limits=dict(min=1,max=10,actions_max=8,questions_max=10), **fixed,
            retrieval_context=dict(status=package['retrieval_status'],sources=[dict(source_ref='src:'+str(e['chunk_id']),title=e['title'],
                document_ref='knowledge_document:'+str(e['document_id']),item_ref='knowledge_item:'+str(e['item_id']))
                for e in package['evidence']],document_groups=self.scope['document_groups']),source_policy=self.scope,
            intent_output_schema=compile_intent_schema(package,runtime_assets=self.runtime_assets,scope=self.scope))
        if not Draft202012Validator(self.runtime_assets.input_schema).is_valid(payload):
            raise ProtocolError('catalog_contract_mismatch')
        result = self.call('intent',payload)
        status = result.get('status') if isinstance(result,dict) else None
        code = result.get('clarification_code') if isinstance(result,dict) else None
        intents = result.get('intents') if isinstance(result,dict) else None
        self.intent_resolution = dict(stage='validating',
            status=status if isinstance(status,str) and status in ('resolved','clarify','unsupported') else None,
            clarification_code=code if isinstance(code,str) and code in ('choose_subject','choose_intent','reduce_scope','confirm_projection','choose_source') else None,
            intent_count=len(intents) if isinstance(intents,list) else None,
            kinds=[i['kind'] for i in intents[:10] if isinstance(i,dict) and isinstance(i.get('kind'),str) and i['kind'] in intents_registry]
                if isinstance(intents,list) else [])
        bad = intent_errors(result,package,question,runtime_assets=self.runtime_assets,scope=self.scope)
        if bad:
            self.intent_resolution['stage']='invalid_intent'
            self.validation_errors.append([e.as_dict() for e in bad[:24]])
            self.record_parameters(result,bad,'intent')
            reference = next((e for e in bad if e.code in ('invalid_intent_reference','needs_clarification')),bad[0])
            raise reference
        if status == 'unsupported':
            self.intent_resolution['stage']='out_of_scope'
            raise ProtocolError('out_of_scope')
        if status != 'resolved':
            self.intent_resolution['stage']='needs_clarification'
            raise ProtocolError('needs_clarification')
        self.intent_resolution['stage']='resolved'
        return intents

    def plan(self, package, catalog, contract, *, question, history):
        from .open_plan import compile_open, model_catalog, preflight, repair_envelope
        self.fact_package=package
        self.check()
        scoped_schema = compile_open(self.schema, package, catalog, contract)
        preflight(package, catalog, contract)
        safe_catalog = model_catalog(catalog, package, contract)
        payload = dict(version=self.manifest['user_pack_version'], snapshot_id=package["snapshot_id"], request_contract=contract,
            plan_schema=scoped_schema, catalog=safe_catalog, allocated_action_ids=[f"a:{i}" for i in range(8)],
            allocated_projection_ids=[f"p:{i}" for i in range(8)], question=question, history=history,
            untrusted_evidence=[dict(source_ref="src:"+str(e["chunk_id"]), content=e["content"], source_class=e["source_class"],
                                     applicability=e["applicability"]) for e in package["evidence"]], preferences={"ordering": "balanced"})
        try:
            plan = self.call("plan", payload)
            # The scoped schema guides model selection within the reviewed wire
            # protocol. Existing truthful optional detail remains valid under
            # the full contract; wire shape + semantic proof are the authority.
            errors = validate(plan, package, catalog, contract, self.schema)
        except ProtocolError as exc:
            if exc.code not in ("invalid_json", "duplicate_key", "non_finite_number"):
                raise
            plan, errors = None, [exc]
        if not errors:
            return plan
        self.validation_errors.append([e.as_dict() for e in errors])
        self.record_parameters(plan,errors,'plan')
        from .diagnostics import plan_shapes, repair_shapes
        self.validation_shapes.append(dict(phase="plan", details=plan_shapes(plan, package, catalog)))
        self.check()
        envelope = repair_envelope(plan, package, catalog, contract, self.schema, errors)
        self.repair_search=envelope.search
        if not envelope.slots:
            raise ProtocolError("incomplete")
        try:
            response = None
            response = self.call("repair", dict(version=self.manifest['user_pack_version'], request_contract=contract, catalog=safe_catalog,
                base_plan=plan, repair_envelope=envelope.public(errors), repair_output_schema=envelope.output_schema()))
            repaired = envelope.merge(response)
            repair_errors = validate(repaired, package, catalog, contract, self.schema,
                                    assigned_local_ids=envelope.assigned_local_ids)
            if repair_errors:
                self.validation_errors.append([e.as_dict() for e in repair_errors])
                self.record_parameters(repaired,repair_errors,'repair')
                raise ProtocolError("incomplete")
            return repaired
        except ProtocolError as exc:
            if response is not None:
                self.validation_shapes.append(dict(phase="repair", shape=repair_shapes(response,envelope,package,catalog)))
            self.validation_errors.append([exc.as_dict()])
            self.record_parameters(response,[exc],'repair')
            if exc.code in ("cancelled", "timeout", "input_budget_exceeded", "token_budget_exceeded"):
                raise
            raise ProtocolError("incomplete") from exc

    def usage(self):
        started=[c for c in self.calls if c['attempt_state'] in ('adapter_started','settled')]
        return dict(total_tokens=self.total, llm_call_count=len(started), calls=self.calls,
                    reserved_tokens=sum(c['reserved_tokens'] for c in self.calls if c['attempt_state']=='registered'),
                    usage_estimated=any(c["usage_estimated"] for c in started) or any(
                        c['attempt_state']=='registered' for c in self.calls), validation_errors=self.validation_errors,
                    validation_shapes=self.validation_shapes, intent_resolution=self.intent_resolution,
                    parameter_diagnostics=self.parameter_diagnostics,diagnostic_overflow=self.parameter_overflow,
                    repair_search=self.repair_search)
