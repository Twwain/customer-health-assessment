"""Assessment and question-answer turns, published after validated commit."""
from __future__ import annotations

import threading
import time
import queue

from sqlalchemy.orm import Session

import config
from models import ChatMessage, AIAssessmentRun
from services.ai import chat_engine, guardrails
from .orchestrator import execute
from .repository import resolve_messages, available_result, is_published
from .protocol import ProtocolError, fingerprint

_cancellations = {}
_lock = threading.Lock()
_workers = threading.BoundedSemaphore(config.CHAT_LLM_GLOBAL_CONCURRENCY)


def cancel_run(db, session_id, run_id):
    from . import audit
    with Session(bind=getattr(db.get_bind(),'engine',db.get_bind())) as writer:
        audit.begin_write(writer)
        run=writer.get(AIAssessmentRun,run_id)
        if not run or run.session_id!=session_id:
            raise LookupError("run_missing")
        if not is_published(run) and run.status in audit.PENDING:
            audit.apply_locked(writer,run,'cancel',status='cancelled',error_code='cancelled')
            with _lock:
                event=_cancellations.get(run_id)
                if event:
                    event.set()
        writer.commit()
    current=db.get(AIAssessmentRun,run_id)
    if current:
        db.refresh(current)
    return current


def run_turn(db, session, *, content="", scenario=None, customer_id=None, persist_user=True,
             exclude_message_id=None, supersede_message_id=None, idempotency_key=None, request_identity=None):
    Event = chat_engine.TurnEvent
    started = time.monotonic()
    scenario = scenario or session.scenario or "free_qa"
    if scenario not in chat_engine.SCENARIOS:
        scenario = "assessment"
    try:
        customer = chat_engine._resolve_customer(db, session, customer_id)
        if not customer:
            raise ProtocolError("needs_clarification")
        question = guardrails.sanitize_input(content)[0] or chat_engine._DEFAULT_QUICK_QUESTIONS.get(scenario, "")
        session.customer_id = customer.id
        chat_engine._auto_title(session, question, customer, scenario)
        base_contract = None
        if supersede_message_id:
            old = db.get(ChatMessage, supersede_message_id)
            if not old or old.session_id != session.id:
                raise ProtocolError("scope_mismatch")
            old_run = db.get(AIAssessmentRun, old.ai_run_id) if old.ai_run_id else None
            if old_run and old_run.session_id == session.id and old_run.scenario in chat_engine.SCENARIOS:
                base_contract, scenario = old_run.request_contract, old_run.scenario
        existing = db.query(AIAssessmentRun).filter_by(idempotency_key=fingerprint([session.id, None,
            "regenerate" if request_identity is not None else scenario, idempotency_key])).first() if idempotency_key else None
        if persist_user and existing is None:
            user = ChatMessage(session_id=session.id, role="user", content=question)
            db.add(user)
            db.commit()
            exclude_message_id = user.id
        history = [{"role": m.role, "content": m.content} for m in chat_engine._history_messages(db, session, exclude_id=exclude_message_id)]
        yield Event("start", dict(session_id=session.id, scenario=scenario, degraded=False, model=config.LLM_MODEL))
        yield Event("stage", dict(message="正在冻结记录、选择计划并校验事实，完成后显示正文。"))
        events, finished, cancelled, box = queue.Queue(maxsize=8), threading.Event(), threading.Event(), {}
        if not _workers.acquire(blocking=False):
            raise ProtocolError("capacity_exceeded")
        bind, cid, sid = db.get_bind(), customer.id, session.id
        db.commit()

        def state(run_id, status):
            box["run_id"] = run_id
            with _lock:
                _cancellations[run_id] = cancelled
            try:
                events.put_nowait((run_id, status))
            except queue.Full:
                pass

        def worker():
            with Session(bind=bind) as worker_db:
                try:
                    run = execute(worker_db, cid, scenario, question=question, session_id=sid, history=history,
                        base_contract=base_contract, supersede_message_id=supersede_message_id,
                        idempotency_key=idempotency_key, cancelled=cancelled, on_state=state, request_identity=request_identity)
                    box["run_id"] = run.run_id
                except BaseException as exc:
                    box["error"] = exc
                finally:
                    with _lock:
                        _cancellations.pop(box.get("run_id"), None)
                    finished.set()
                    _workers.release()

        threading.Thread(target=worker, name="ai-v2-run", daemon=True).start()
        # Disconnecting SSE leaves the bounded run alive; reconnect resolves the
        # persisted run instead of paying for another plan.
        while not finished.is_set():
            try:
                run_id, status = events.get(timeout=.2)
                yield Event("stage", dict(run_id=run_id, status=status, message={"capturing": "正在冻结事实记录。", "planning": "正在选择并验证计划。"}.get(status, status)))
            except queue.Empty:
                continue
        if "error" in box:
            raise box["error"]
        db.expire_all()
        run = db.get(AIAssessmentRun, box["run_id"])
        if run is None:
            raise ProtocolError('run_missing')
        if run.status != "succeeded" and not run.validated_result.get("degraded"):
            yield Event("error", dict(message={"needs_clarification": "请明确要查询的客户、业务问题及数量。",
                                               "out_of_scope": "这个问题超出客情助手的回答范围。我可以帮助分析客户关系、业务推进、风险、续约和相关知识。",
                                               "source_changed": "生成期间来源发生变化，请重新评估。"}.get(run.error_code, "AI 评估未完成："+run.error_code),
                                      run_id=run.run_id, status=run.status, error_code=run.error_code))
            return
        result = available_result(db, run, customer_id=customer.id, session_id=session.id)
        if not result:
            raise ProtocolError("source_unavailable")
        message = db.query(ChatMessage).filter_by(ai_run_id=run.run_id, content_format="ai_v2_ref").one()
        resolved = resolve_messages(db, [message])[0]
        yield Event("context", dict(assessment=result["assessment"], trend=result["trend"]))
        yield Event("delta", dict(text=result["content"]))
        yield Event("strategy", dict(items=result["strategy_items"]))
        yield Event("references", dict(items=resolved["references"]))
        yield Event("done", dict(message=resolved, run_id=run.run_id, degraded=bool(result.get("degraded")), tokens_used=run.usage["total_tokens"],
            latency_ms=int((time.monotonic()-started)*1000), timings=dict(llm_calls=run.usage["calls"], llm_call_count=run.usage["llm_call_count"])))
    except ProtocolError as exc:
        db.rollback()
        yield Event("error", dict(message="AI 评估未完成："+exc.code, error_code=exc.code))


def replay_regeneration(db, session, key, identity):
    """Resolve an operation before selecting the now-mutable latest answer."""
    if not key:
        return None
    from fastapi import HTTPException
    from .execution import recover_pending
    recover_pending(db, [session.id])
    run = db.query(AIAssessmentRun).filter_by(idempotency_key=fingerprint([session.id, None, "regenerate", key])).first()
    if not run:
        return None
    if run.customer_id != session.customer_id or run.payload_hash != fingerprint(identity):
        raise HTTPException(status_code=409, detail="幂等键对应另一项重新生成请求")
    result = available_result(db, run, customer_id=session.customer_id, session_id=session.id)
    response = dict(session_id=session.id, run_id=run.run_id, status=run.status, error_code=run.error_code)
    if not result:
        response["error"] = "原操作尚未完成或结果已不可用，请查询运行状态。"
        response["message"] = None
        return response
    message = db.query(ChatMessage).filter_by(ai_run_id=run.run_id, content_format="ai_v2_ref").one()
    resolved = resolve_messages(db, [message])[0]
    response.update(message=resolved, assessment=result["assessment"], trend=result["trend"],
        strategy_items=result["strategy_items"], references=resolved["references"], degraded=bool(result.get("degraded")),
        tokens_used=run.usage.get("total_tokens", 0), timings=dict(llm_calls=run.usage.get("calls", []),
                                                               llm_call_count=run.usage.get("llm_call_count", 0)))
    return response
