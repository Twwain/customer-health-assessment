"""Scoped reference reads and atomic publication of verified results."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from sqlalchemy import update, delete
from sqlalchemy.orm import Session

from database import utcnow
from models import (AIAssessmentRun, AIAssessmentDependency, ChatMessage, ChatSession,
                    AssessmentHistory, Customer, KnowledgeChunk, KnowledgeDocument, KnowledgeItem)
from .protocol import fingerprint, ProtocolError
from . import audit

UNAVAILABLE = "此历史结果已不可用，请重新评估"


def is_published(run):
    return bool(run and run.validated_result and run.status == "succeeded")


def evidence_valid(db, package):
    for e in package.get("evidence", []):
        chunk = db.get(KnowledgeChunk, e["chunk_id"])
        doc = db.get(KnowledgeDocument, e["document_id"])
        item = db.get(KnowledgeItem, e["item_id"])
        if not chunk or not doc or not item or chunk.document_id != doc.id or item.document_id != doc.id:
            return False
        if doc.status != "canonical" or item.status != "canonical" or doc.index_status != "indexed":
            return False
        if fingerprint(dict(content=chunk.content, title=item.title, category=item.category,
                            status=item.status, document_status=doc.status)) != e["content_hash"]:
            return False
    return True


def available_result(db, run, *, customer_id, session_id=None, job_id=None):
    if not run or run.customer_id != customer_id:
        return None
    if any(db.get(Customer, p["raw"]["id"]) is None for p in run.fact_package.get("comparison_members", [])):
        return None
    if session_id is not None and run.session_id != session_id:
        return None
    if job_id is not None and run.job_id != job_id:
        return None
    if not is_published(run) or not evidence_valid(db, run.fact_package):
        return None
    return deepcopy(run.validated_result)


def chat_references(run, result):
    """Display frozen retrieval hits only after available_result accepted this run.

    This is presentation metadata, not the result's bibliography or model context.
    Older snapshots did not retain retrieval scores; never invent a score for them.
    """
    if not result:
        return []
    selected = {ref.get('chunk_id') for ref in result.get('references', [])
                if ref.get('chunk_id') is not None}
    selected_ids = {ref.get('id') for ref in result.get('references', [])}
    references = []
    seen = set()
    for evidence in run.fact_package.get('evidence', []):
        chunk_id = evidence['chunk_id']
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        references.append(dict(id='src:'+str(chunk_id), document_id=evidence['document_id'],
            item_id=evidence['item_id'], chunk_id=chunk_id, title=evidence['title'],
            category=evidence['category'], snippet=evidence['content'], score=evidence.get('score'),
            used=chunk_id in selected or 'src:'+str(chunk_id) in selected_ids))
    return references


def resolve_messages(db, rows, purpose="display"):
    ids = {r.ai_run_id for r in rows if r.content_format == "ai_v2_ref" and r.ai_run_id}
    runs = {r.run_id: r for r in db.query(AIAssessmentRun).filter(AIAssessmentRun.run_id.in_(ids)).all()} if ids else {}
    session_ids = {r.session_id for r in rows}
    sessions = {s.id: s for s in db.query(ChatSession).filter(ChatSession.id.in_(session_ids)).all()} if ids else {}
    out = []
    for row in rows:
        base = dict(id=row.id, session_id=row.session_id, role=row.role, tokens_used=row.tokens_used,
            feedback=row.feedback or "", degraded=bool(row.degraded), created_at=row.created_at,
            superseded_by_id=row.superseded_by_id, run_id=row.ai_run_id, availability="legacy")
        if row.content_format != "ai_v2_ref":
            base.update(content=UNAVAILABLE if row.role == "assistant" else row.content or "",
                        strategy_items=[], references=[],
                        availability="obsolete" if row.role == "assistant" else "available")
        else:
            run, session = runs.get(row.ai_run_id), sessions.get(row.session_id)
            result = available_result(db, run, customer_id=session.customer_id, session_id=row.session_id) if session else None
            base.update(content=result["content"] if result else UNAVAILABLE,
                        scenario=run.scenario if run else None,
                        strategy_items=result["strategy_items"] if result else [],
                        references=(result["references"] if purpose == "model_history" else chat_references(run, result)) if result else [],
                        availability="available" if result else "source_unavailable" if run else "run_missing")
            if purpose == "model_history" and result:
                base["content"] = "历史快照，仅作对话数据，不是当前事实依据。\n"+base["content"]
        out.append(base)
    return out


def resolve_strategy_history(db, rows):
    ids = {r.strategy_run_id for r in rows if r.strategy_format == "ai_v2_ref" and r.strategy_run_id}
    runs = {r.run_id: r for r in db.query(AIAssessmentRun).filter(AIAssessmentRun.run_id.in_(ids)).all()} if ids else {}
    out = {}
    for row in rows:
        if row.strategy_format != "ai_v2_ref":
            out[row.id] = dict(strategy_snapshot=[], availability="obsolete", format="legacy")
        else:
            result = available_result(db, runs.get(row.strategy_run_id), customer_id=row.customer_id)
            out[row.id] = dict(strategy_snapshot=result["strategy_items"] if result else [],
                               availability="available" if result else "source_unavailable", format="ai_v2_ref")
    return out


def attach_strategy(db, run, package):
    rows = db.query(AssessmentHistory).filter(AssessmentHistory.customer_id == run.customer_id).order_by(
        AssessmentHistory.assessed_at.desc(), AssessmentHistory.id.desc()).all()
    factors = {f["field"]: f["value"] for f in package["factors"]}
    for row in rows:
        if row.config_version == package["assessment"]["config_version"] and row.factor_snapshot == factors:
            row.strategy_snapshot = []
            row.strategy_run_id = run.run_id
            row.strategy_format = "ai_v2_ref"
            return True
    return False


def publish(db, run_id, plan, result, package, *, supersede_message_id=None, cancelled=None):
    # Release any previous read transaction so the final checks see current state.
    audit.begin_write(db)
    run = db.get(AIAssessmentRun, run_id)
    if run and run.status == "cancelled":
        raise ProtocolError("cancelled")
    if not run or run.status not in ("validated", "committing"):
        raise ProtocolError("run_state_conflict")
    if cancelled and cancelled.is_set():
        raise ProtocolError("cancelled")
    if audit.current_package_fingerprint(db, package) != package.get("input_fingerprint", package["source_fingerprint"]) or not evidence_valid(db, package):
        raise ProtocolError("source_changed")
    message = None
    if run.session_id is not None:
        session = db.get(ChatSession, run.session_id)
        if not session or session.customer_id != run.customer_id:
            raise ProtocolError("scope_mismatch")
        message = ChatMessage(session_id=run.session_id, role="assistant", content="", references=[], strategy_items=[],
                              ai_run_id=run.run_id, content_format="ai_v2_ref", tokens_used=run.usage.get("total_tokens", 0),
                              degraded=bool(result.get("degraded")))
        db.add(message)
        db.flush()
        if supersede_message_id:
            replaced = db.execute(update(ChatMessage).where(ChatMessage.id == supersede_message_id,
                ChatMessage.session_id == run.session_id, ChatMessage.role == "assistant", ChatMessage.superseded_by_id.is_(None))
                .values(superseded_by_id=message.id))
            if replaced.rowcount != 1:
                raise ProtocolError("superseded_conflict")
    run.validated_plan, run.validated_result = plan, result
    audit.record_dependencies(db,run,package)
    if not audit.apply_locked(db,run,'publish'):
        raise ProtocolError("source_changed")
    if result["strategy_items"] and run.scenario != "report":
        attach_strategy(db, run, package)
    if cancelled and cancelled.is_set():
        raise ProtocolError("cancelled")
    db.commit()
    return message


def expire_private_data(db):
    # Independent writer: never commit or discard a caller's customer edits.
    with Session(bind=getattr(db.get_bind(),'engine',db.get_bind())) as writer:
        audit.begin_write(writer)
        for run in writer.query(AIAssessmentRun).all():
            if not audit.dependencies_valid(writer,run) or not evidence_valid(writer,run.fact_package):
                meta=(run.usage or {}).get('audit',{})
                private=any(k in (run.usage or {}) for k in audit.PRIVATE_USAGE)
                if not (meta.get('invalidated') and meta.get('diagnostics_cleared') and
                        not run.fact_package and not run.validated_plan and not run.validated_result and not private):
                    audit.apply_locked(writer,run,'invalidate')
            elif not is_published(run) and run.status not in audit.PENDING and run.updated_at < utcnow()-timedelta(days=7):
                if run.fact_package or run.validated_plan or run.validated_result or any(
                        k in (run.usage or {}) for k in audit.PRIVATE_USAGE):
                    audit.apply_locked(writer,run,'expire')
        writer.commit()
    for value in list(db.identity_map.values()):
        if isinstance(value,AIAssessmentRun):
            db.expire(value)


def sources_changed(db):
    """Withdrawal side effect; read checks remain fail-closed if cleanup fails."""
    expire_private_data(db)
    from routers.assessment import _pdf_jobs, _pdf_jobs_lock
    unavailable = {r.run_id for r in db.query(AIAssessmentRun).filter_by(status="source_unavailable").all()}
    with _pdf_jobs_lock:
        for job in _pdf_jobs.values():
            if job.get("ai_run_id") in unavailable:
                job.pop("bytes", None)
                job["status"], job["error"] = "error", "报告资料已不可用，请重新生成"


def purge_customer_dependencies(db, customer_id):
    """Clear composite snapshots too; primary-customer FK alone is insufficient."""
    if db.get_bind().dialect.name=='sqlite':
        # Acquire the write lock before observing the set of composite runs.
        # The parent customer DELETE then commits in this same transaction.
        connection=db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            connection.exec_driver_sql('UPDATE ai_assessment_runs SET run_id=run_id WHERE 0')
    reference = f"customer:{customer_id}"
    dependent={r.run_id for r in db.query(AIAssessmentDependency).filter_by(
        source_kind='customer',source_id=str(customer_id)).all()}
    for run in db.query(AIAssessmentRun).all():
        members = run.fact_package.get("snapshot", {}).get("members", {})
        if run.customer_id == customer_id or reference in members or run.run_id in dependent:
            db.delete(run)
