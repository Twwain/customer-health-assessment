"""Run lifecycle. Only persisted, validated and current results are published."""
from __future__ import annotations

from uuid import uuid4
import time

import config
from sqlalchemy.exc import IntegrityError
from models import AIAssessmentRun
from services.ai.llm_adapter import get_chat_adapter, LLMError
from .facts import capture, capture_comparison, explicit_comparison_ids
from .catalog import build_catalog
from .contracts import build_contract
from .planner import Planner
from .protocol import fingerprint, ProtocolError, load_runtime_assets
from .renderer import render
from . import repository, execution, audit


def execute(db, customer_id, scenario, *, question="", session_id=None, job_id=None, history=(),
            idempotency_key=None, base_contract=None, supersede_message_id=None, cancelled=None, deadline=None, adapter=None,
            retrieve_evidence=True, on_state=None, request_identity=None):
    try:
        repository.expire_private_data(db)
    except Exception as failure:
        raise ProtocolError('audit_persist_failed') from failure
    payload_hash = fingerprint(request_identity if request_identity is not None else dict(customer_id=customer_id,
        scenario=scenario, question=question, base_contract=base_contract, supersede_message_id=supersede_message_id))
    scope_key = fingerprint([session_id, job_id, "regenerate" if request_identity is not None else scenario, idempotency_key]) if idempotency_key else None
    if scope_key:
        existing = db.query(AIAssessmentRun).filter_by(idempotency_key=scope_key).first()
        if existing:
            if existing.payload_hash != payload_hash:
                raise ProtocolError("idempotency_conflict")
            return existing
    run_id = uuid4().hex
    is_report = scenario == "report"
    seconds = max(.1, deadline-time.monotonic()) if deadline else (
        config.REPORT_GENERATION_TIMEOUT_SECONDS if is_report else config.CHAT_TURN_TIMEOUT_SECONDS)
    absolute_deadline = deadline or time.monotonic()+seconds
    runtime = execution.register(run_id, seconds)
    run = AIAssessmentRun(run_id=run_id, customer_id=customer_id, session_id=session_id, job_id=job_id,
        idempotency_key=scope_key, payload_hash=payload_hash, scenario=scenario, status="capturing",
        versions={"runtime": runtime}, usage={"audit": audit.initial_audit()})
    planner, package = None, None
    bind = db.get_bind()
    retrieval_timings = {}
    embedding_calls = []

    def usage():
        value = planner.usage() if planner else {}
        return {**value, "retrieval": retrieval_timings,
                "embedding": package.get("retrieval_usage", {}) if package else {},
                "embedding_calls":embedding_calls}

    def checkpoint(stage, observed=None, *, status=None, fields=None, versions=None, error_code=None):
        # All intentional caller writes have committed before this helper.
        db.rollback()
        try:
            accepted = audit.update_run(bind, run_id, "checkpoint", stage=stage,
                usage=observed if observed is not None else usage(), package=package,
                status=status, fields=fields, versions=versions, error_code=error_code)
        except ProtocolError:
            raise
        except Exception as failure:
            raise ProtocolError('audit_persist_failed') from failure
        db.expire_all()
        if not accepted:
            raise ProtocolError('run_state_conflict')
        return accepted

    def settle(observed):
        db.rollback()
        try:
            audit.update_run(bind, run_id, "usage_only", usage=observed)
        except ProtocolError:
            raise
        except Exception as failure:
            raise ProtocolError('audit_persist_failed') from failure
        db.expire_all()

    def terminal(code, *, internal=False):
        db.rollback()
        status = code if code in ("cancelled", "timeout", "needs_clarification", "source_changed", "dependency_unavailable") else (
            "provider_error" if internal else "incomplete")
        audit.update_run(bind, run_id, "failure", usage=usage(), package=package, status=status, error_code=code)
        db.expire_all()

    try:
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.query(AIAssessmentRun).filter_by(idempotency_key=scope_key).first() if scope_key else None
            if not existing or existing.payload_hash != payload_hash:
                raise ProtocolError("idempotency_conflict")
            return existing
        if on_state:
            on_state(run_id, "capturing")
        # Assets are verified after the row exists, never during module import.
        bundle = load_runtime_assets()
        checkpoint("assets_verified", versions=bundle.manifest)
        planner = Planner(adapter or get_chat_adapter(),
            token_budget=config.REPORT_TURN_TOKEN_BUDGET if is_report else config.CHAT_TURN_TOKEN_BUDGET,
            output_limit=config.LLM_REPORT_MAX_TOKENS if is_report else config.LLM_MAX_TOKENS,
            deadline=absolute_deadline, cancelled=cancelled,
            max_calls=3 if scenario == "free_qa" and not base_contract else 2,
            runtime_assets=bundle,
            on_checkpoint=lambda stage, value: checkpoint(stage, value, status={
                "before_intent": "facts_ready", "before_plan": "planning", "before_repair": "repairing"}[stage]),
            on_usage=settle)
        others = explicit_comparison_ids(db, question, customer_id) if scenario == "free_qa" else []
        package = capture_comparison(bind, customer_id, others) if others else capture(bind, customer_id)
        checkpoint("capture_ready", status="facts_ready")
        if retrieve_evidence:
            from .evidence import retrieve
            from services.ai.llm_adapter import reset_embedding_usage,get_embedding_usage,embedding_request_scope
            from models import Customer
            embedding_calls.append(dict(operation_id='retrieval:1',state='registered',
                model=config.EMBEDDING_MODEL,http_attempts=0,total_tokens=0,usage_estimated=True))
            checkpoint("capture_ready",status="facts_ready")
            planner.check()
            reset_embedding_usage()
            def before_embedding_http():
                planner.check()
                checkpoint("capture_ready",status="facts_ready")
                planner.check()
            try:
                with embedding_request_scope(before_embedding_http):
                    retrieval_timings = retrieve(db, package, question or "客户客情维护策略和风险核实", db.get(Customer, customer_id))
                db.commit()
            finally:
                observed=package.get('retrieval_usage') or get_embedding_usage()
                embedding_calls[0].update(state='settled',
                    http_attempts=observed.get('http_attempts',0),total_tokens=observed.get('total_tokens',0),
                    usage_estimated=observed.get('usage_estimated',False))
                # Even cancellation/withdrawal can only settle this registered
                # operation. No evidence or diagnostics cross this late path.
                settle(usage())
            checkpoint("retrieval_ready", status="facts_ready")
        catalog = build_catalog(package)
        planner.check()
        intents = planner.resolve_intent(question, package, history) if scenario == "free_qa" and not base_contract else None
        contract = build_contract(package, catalog, scenario, intents=intents, base_contract=base_contract,
            request_hash=fingerprint(question), runtime_assets=bundle,
            policy_scope=getattr(planner, "scope", None))
        checkpoint("before_plan", status="request_resolved",
                   fields=dict(fact_package=package, request_contract=contract))
        if on_state:
            on_state(run_id, "planning")
        plan = planner.plan(package, catalog, contract, question=question, history=history)
        result = render(plan, package, catalog, contract)
        checkpoint("before_"+planner.calls[-1]['phase'], status="validated")
        planner.check()
        repository.publish(db, run_id, plan, result, package,
                           supersede_message_id=supersede_message_id, cancelled=cancelled)
    except (ProtocolError, LLMError) as exc:
        code = exc.code if isinstance(exc, ProtocolError) else "dependency_unavailable"
        if code=='idempotency_conflict':
            raise
        # The explicit emergency result is assembled while still pending. A
        # terminal row is never reopened to make a fallback appear successful.
        fallback_done = False
        if code == "dependency_unavailable" and package and not supersede_message_id:
            from .fallback import build_fallback
            db.rollback()
            current = db.get(AIAssessmentRun, run_id, populate_existing=True)
            if current and current.status in audit.PENDING:
                fallback = build_fallback(package, scenario, runtime_assets=bundle)
                if fallback:
                    fallback_plan, fallback_result, fallback_contract = fallback
                    try:
                        checkpoint("before_plan", status="validated", error_code=code,
                            fields=dict(fact_package=package, request_contract=fallback_contract))
                        repository.publish(db, run_id, fallback_plan, fallback_result, package, cancelled=cancelled)
                        fallback_done = True
                    except ProtocolError as failure:
                        code = failure.code
        if not fallback_done:
            try:
                terminal(code)
            except Exception as failure:
                db.rollback()
                raise ProtocolError('audit_persist_failed') from failure
    except Exception:
        # Database failure cannot truthfully be called a persisted terminal.
        # Stop external work, release runtime, and let bounded recovery handle
        # an orphan if even this controlled failure cannot be written.
        try:
            terminal("internal_error", internal=True)
        except Exception as failure:
            db.rollback()
            raise ProtocolError("audit_persist_failed") from failure
    finally:
        execution.unregister(run_id)
    db.expire_all()
    finished=db.get(AIAssessmentRun, run_id)
    if finished is None:
        raise ProtocolError('run_missing')
    return finished
