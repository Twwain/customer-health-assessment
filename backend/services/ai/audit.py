"""Private bounded audit writes, serialized with terminal and withdrawal writes."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json

from sqlalchemy.orm import Session

from models import (AIAssessmentRun, AIAssessmentDependency, Customer,
                    KnowledgeDocument, KnowledgeItem, KnowledgeChunk)
from .protocol import ProtocolError, fingerprint

PENDING = frozenset(("capturing", "facts_ready", "request_resolved", "planning",
                     "validating", "repairing", "validated", "committing"))
STAGES = frozenset(("run_created", "assets_verified", "capture_ready", "retrieval_ready",
                    "before_intent", "before_plan", "before_repair", "failure", "success"))
PRIVATE_USAGE = frozenset(("parameter_diagnostics", "validation_shapes", "validation_errors",
                          "intent_resolution", "source_ids"))


def initial_audit(*, legacy=False, status="capturing"):
    terminal = status not in PENDING
    kind = {"cancelled": "cancelled", "timeout": "timeout", "source_changed": "source_changed",
            "source_unavailable": "source_unavailable", "succeeded": "succeeded"}.get(
                status, "failed" if terminal else "none")
    return dict(version="1.0", stage="success" if status == "succeeded" else "failure" if terminal else "run_created",
        assets_state="not_available", source_kind="not_available", capture_fingerprint=None,
        evidence_fingerprint=None, retrieval_state="not_started",
        invalidated=status in ("source_changed", "source_unavailable"),
        diagnostics_cleared=status in ("source_changed", "source_unavailable"), overflow_count=0,
        revision=0, terminal_kind=kind, legacy_initialized=legacy)


def begin_write(db):
    """Caller must have committed its own intended work before this boundary."""
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    db.expire_all()


def _audit(run):
    saved = (run.usage or {}).get("audit")
    value = initial_audit(legacy=not bool(saved), status=run.status)
    if saved:
        value.update({k: v for k, v in saved.items() if k in value})
    if run.status in ("source_changed", "source_unavailable"):
        value["invalidated"] = value["diagnostics_cleared"] = True
    return value


def _clean_usage(value):
    value = deepcopy(value or {})
    for key in PRIVATE_USAGE:
        value.pop(key, None)
    return value


def _bounded_diagnostics(items):
    output = []
    for item in items[:24]:
        candidate = output + [item]
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode()) > 8192:
            break
        output = candidate
    return output


def _merge_usage(current, incoming, *, late=False):
    """Only registered attempts can settle after a terminal transition."""
    output = deepcopy(current or {})
    existing = {(c.get("phase"), c.get("attempt")): deepcopy(c)
                for c in output.get("calls", []) if isinstance(c, dict)}
    for call in (incoming or {}).get("calls", [])[:3]:
        if not isinstance(call, dict):
            continue
        phase, attempt = call.get("phase"), call.get("attempt")
        if phase not in ("intent", "plan", "repair") or type(attempt) is not int or not 1 <= attempt <= 3:
            continue
        key = (phase, attempt)
        if key not in existing and (late or len(existing) >= 3):
            continue
        previous = existing.get(key)
        safe = {k: v for k, v in call.items() if k in ("phase", "attempt", "usage_estimated",
            "total_tokens", "prompt_tokens", "completion_tokens", "finish_reason","attempt_state","reserved_tokens")}
        state=safe.get('attempt_state','adapter_started')
        if state not in ('registered','not_sent','adapter_started','settled'):continue
        previous_state=(previous or {}).get('attempt_state','adapter_started')
        if previous and previous_state!='registered' and state in ('registered','not_sent'):continue
        for token in ("total_tokens", "prompt_tokens", "completion_tokens","reserved_tokens"):
            if token in safe and (type(safe[token]) is not int or safe[token] < 0):
                safe.pop(token)
        if safe.get("finish_reason") not in ("", "stop", "length", "content_filter", "tool_calls"):
            safe.pop("finish_reason", None)
        # The first actual settlement is final, and equal replays are no-ops.
        if previous and (previous_state in ('not_sent','settled') or not previous.get("usage_estimated", True)):
            continue
        existing[key] = {**(previous or {}), **safe}
    output["calls"] = [existing[k] for k in sorted(existing, key=lambda k: k[1])]
    started=[c for c in output["calls"] if c.get('attempt_state','adapter_started') in ('adapter_started','settled')]
    output["llm_call_count"] = len(started)
    output["total_tokens"] = sum(c.get("total_tokens", 0) for c in started)
    output["reserved_tokens"] = sum(c.get('reserved_tokens',0) for c in output['calls'] if c.get('attempt_state')=='registered')
    output["usage_estimated"] = any(c.get("usage_estimated", True) for c in started) or any(
        c.get('attempt_state')=='registered' for c in output['calls'])
    # Retrieval has its own registered operation and cost unit. Terminal rows
    # can settle its observed usage, but can never register new work.
    embedding={c.get('operation_id'):deepcopy(c) for c in output.get('embedding_calls',[]) if isinstance(c,dict)}
    for call in (incoming or {}).get('embedding_calls',[])[:1]:
        if not isinstance(call,dict) or call.get('operation_id')!='retrieval:1':continue
        previous=embedding.get('retrieval:1')
        if previous is None and (late or call.get('state')!='registered'):continue
        if previous and previous.get('state')=='settled':continue
        if call.get('state') not in ('registered','settled'):continue
        if previous and call.get('model')!=previous.get('model'):continue
        if not isinstance(call.get('model'),str) or len(call['model'])>100:continue
        if any(type(call.get(k)) is not int or call[k]<0 for k in ('http_attempts','total_tokens')):continue
        if type(call.get('usage_estimated')) is not bool:continue
        embedding['retrieval:1']={k:deepcopy(call[k]) for k in
            ('operation_id','state','model','http_attempts','total_tokens','usage_estimated')}
    if embedding:
        output['embedding_calls']=list(embedding.values())
        finished=[c for c in embedding.values() if c.get('state')=='settled']
        output['embedding']=dict(http_attempts=sum(c['http_attempts'] for c in finished),
            total_tokens=sum(c['total_tokens'] for c in finished),
            usage_estimated=any(c['usage_estimated'] for c in finished) or not bool(finished),
            model=next(iter(embedding.values()))['model'])
    if not late:
        for key in ("validation_errors", "validation_shapes", "intent_resolution", "retrieval",
                    "repair_search"):
            if key in (incoming or {}):
                value=deepcopy(incoming[key])
                if key=='validation_errors':
                    value=[[{k:(v[:8] if k=='allowed_refs' and isinstance(v,list) else v)
                             for k,v in error.items()} for error in group[:24]] for group in value[:3]]
                elif key=='validation_shapes':
                    value=value[:3]
                if key in ('validation_errors','validation_shapes'):
                    while value and len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())>8192:
                        value.pop()
                output[key] = value
        if "parameter_diagnostics" in (incoming or {}):
            output["parameter_diagnostics"] = _bounded_diagnostics(incoming["parameter_diagnostics"])
    return output


def record_dependencies(db, run, package):
    """Record captured identities before intent, and reuse this at publication."""
    values = [("customer", str(run.customer_id), package["customer_hash"]),
              ("config", "active", package["config_hash"]),
              ("history", str(run.customer_id), package["history_hash"])]
    values += [("customer", str(p["raw"]["id"]), p["customer_hash"])
               for p in package.get("comparison_members", [])]
    values += [("knowledge", f"{e['document_id']}:{e['chunk_id']}:{e['item_id']}", e["content_hash"])
               for e in package.get("evidence", [])]
    existing = {(d.source_kind, d.source_id, d.content_hash) for d in
                db.query(AIAssessmentDependency).filter_by(run_id=run.run_id).all()}
    for kind, sid, digest in values:
        if (kind, sid, digest) not in existing:
            db.add(AIAssessmentDependency(run_id=run.run_id, source_kind=kind, source_id=sid, content_hash=digest))
            existing.add((kind, sid, digest))


def dependencies_valid(db, run):
    """Validate early identities without retaining a failed request's source text."""
    for dep in db.query(AIAssessmentDependency).filter_by(run_id=run.run_id).all():
        if dep.source_kind == "customer":
            if db.get(Customer, int(dep.source_id)) is None:
                return False
        elif dep.source_kind == "knowledge":
            parts = dep.source_id.split(":")
            if not all(p.isdigit() for p in parts) or len(parts) not in (1, 3):
                return False
            doc = db.get(KnowledgeDocument, int(parts[0]))
            if not doc or doc.status != "canonical" or doc.index_status != "indexed":
                return False
            if len(parts) == 3:
                chunk, item = db.get(KnowledgeChunk, int(parts[1])), db.get(KnowledgeItem, int(parts[2]))
                if not chunk or not item or chunk.document_id != doc.id or item.document_id != doc.id or item.status != "canonical":
                    return False
                if fingerprint(dict(content=chunk.content, title=item.title, category=item.category,
                                    status=item.status, document_status=doc.status)) != dep.content_hash:
                    return False
            else:
                # Legacy rows only named a document; its old private snapshot,
                # when present, is checked by the read bridge as before.
                items = db.query(KnowledgeItem).filter_by(document_id=doc.id, status="canonical").all()
                if not items:
                    return False
    return True


def _clear_private(run, usage, audit):
    run.fact_package, run.validated_plan, run.validated_result = {}, {}, {}
    audit["diagnostics_cleared"] = True
    return _clean_usage(usage)


def current_package_fingerprint(db,package):
    """Read inside the writer snapshot, including on a single-connection DB."""
    from .facts import capture
    clock=datetime.now(timezone.utc)
    captured=capture(db.get_bind(),package['raw']['id'],reader=db,clock=clock)
    value=captured['source_fingerprint']
    if package.get('comparison_members'):
        members=[capture(db.get_bind(),p['raw']['id'],reader=db,clock=clock)
                 for p in package['comparison_members']]
        value=fingerprint([value,[p['source_fingerprint'] for p in members]])
    return value


def apply_locked(db, run, event, *, stage=None, usage=None, package=None, versions=None,
                 fields=None, status=None, error_code=None):
    """Apply to the current row under the caller's short writer transaction."""
    if run is None:
        return False
    meta = _audit(run)
    current = deepcopy(run.usage or {})
    pending = run.status in PENDING
    published = bool(run.validated_result and run.status in ("succeeded", "dependency_unavailable"))
    if event in ('checkpoint','failure','publish') and pending and not dependencies_valid(db,run):
        apply_locked(db,run,'invalidate')
        if usage:
            apply_locked(db,run,'usage_only',usage=usage)
        return False
    if event in ("checkpoint", "usage_only") and meta["invalidated"]:
        current = _clean_usage(current)
    if event == "checkpoint":
        if not pending or meta["invalidated"]:
            return False
        if not dependencies_valid(db, run):
            apply_locked(db, run, "invalidate")
            return False
        if package:
            from .repository import evidence_valid
            current_hash=current_package_fingerprint(db,package)
            if not evidence_valid(db, package) or current_hash != package.get(
                    "input_fingerprint", package["source_fingerprint"]):
                apply_locked(db, run, "invalidate")
                return False
        if stage not in STAGES or stage in ("failure", "success"):
            raise ProtocolError("audit_persist_failed")
        current = _merge_usage(current, usage)
        meta["stage"] = stage
        if versions is not None:
            run.versions = {**deepcopy(versions), "runtime": run.versions.get("runtime", {})}
            meta["assets_state"] = "verified"
        if stage == "capture_ready" and package:
            meta.update(source_kind="captured", capture_fingerprint=package["source_fingerprint"])
        if stage == "retrieval_ready" and package:
            if package["retrieval_status"] in ("available", "no_hits"):
                meta.update(source_kind="full_evidence", evidence_fingerprint=package["source_fingerprint"],
                            retrieval_state="succeeded")
        if package:
            run.source_fingerprint = package["source_fingerprint"]
            record_dependencies(db, run, package)
            if package.get('retrieval_status')=='unavailable':
                meta['retrieval_state']='failed'
        for key, value in (fields or {}).items():
            if key not in ("fact_package", "request_contract"):
                raise ProtocolError("audit_persist_failed")
            setattr(run, key, deepcopy(value))
        if status is not None:
            if status not in PENDING:
                raise ProtocolError("audit_persist_failed")
            run.status = status
        if error_code is not None:
            run.error_code=error_code
    elif event == "usage_only":
        registered={(c.get('phase'),c.get('attempt')):c for c in current.get('calls',[]) if isinstance(c,dict)}
        for call in (usage or {}).get('calls',[])[:3]:
            previous=registered.get((call.get('phase'),call.get('attempt')))
            if previous is None or (not previous.get('usage_estimated',True) and
                    call.get('total_tokens')!=previous.get('total_tokens')):
                return False
        current = _merge_usage(current, usage, late=True)
        if meta["diagnostics_cleared"]:
            current = _clean_usage(current)
        candidate={**current,'audit':meta}
        if candidate==(run.usage or {}):
            return True
    elif event in ("failure", "cancel", "recover"):
        if published or not pending:
            # Actual late usage is still settled, but private state is frozen.
            if usage:
                current = _merge_usage(current, usage, late=True)
            else:
                return False
        else:
            current = _merge_usage(current, usage)
            run.status = status or ("cancelled" if event == "cancel" else "incomplete")
            run.error_code = error_code or run.status
            kind = {"cancelled": "cancelled", "timeout": "timeout", "executor_lost": "executor_lost",
                    "source_changed": "source_changed", "asset_integrity_error": "failed"}.get(run.error_code, "failed")
            if event == "recover" and (not (run.usage or {}).get("audit") or meta['assets_state']!='verified'):
                kind = "audit_incomplete"
            meta.update(stage="failure", terminal_kind=kind)
            if run.error_code == "asset_integrity_error":
                meta["assets_state"] = "mismatch"
            if package and package.get("retrieval_status") == "unavailable":
                meta["retrieval_state"] = "failed"
    elif event == "publish":
        if not pending or meta["invalidated"]:
            return False
        run.status = "succeeded"
        meta.update(stage="success", terminal_kind="succeeded")
    elif event == "invalidate":
        if published:
            run.status, run.error_code = "source_unavailable", "source_unavailable"
            meta.update(stage="failure", terminal_kind="source_unavailable")
        elif pending:
            run.status, run.error_code = "source_changed", "source_changed"
            meta.update(stage="failure", terminal_kind="source_changed")
        meta["invalidated"] = True
        current = _clear_private(run, current, meta)
    elif event == "expire":
        if pending or published:
            return False
        current = _clear_private(run, current, meta)
    else:
        raise ProtocolError("audit_persist_failed")
    meta["revision"] += 1
    incoming_count=len((usage or {}).get('parameter_diagnostics',[]))
    kept_count=len(current.get('parameter_diagnostics',[]))
    meta['overflow_count']=max(meta['overflow_count'],max(0,incoming_count-kept_count),
                               (usage or {}).get('diagnostic_overflow',0))
    if meta["diagnostics_cleared"]:
        current = _clean_usage(current)
    current["audit"] = meta
    run.usage = current
    return True


def update_run(bind, run_id, event, **kwargs):
    """No caller's stale ORM state is written back after a concurrent terminal."""
    with Session(bind=getattr(bind,'engine',bind)) as writer:
        begin_write(writer)
        run = writer.get(AIAssessmentRun, run_id)
        if run is None:
            writer.rollback()
            if event=='checkpoint':raise ProtocolError('run_missing')
            return False
        accepted = apply_locked(writer, run, event, **kwargs)
        writer.commit()
        if not accepted and event == "checkpoint":
            code = run.error_code if run.error_code in ("cancelled", "timeout", "source_changed") else "run_state_conflict"
            raise ProtocolError(code)
        return accepted
