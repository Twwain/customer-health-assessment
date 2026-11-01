"""Current AI turn transport, session persistence and bounded conversation history."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator
from sqlalchemy.orm import Session
import config
from database import SessionLocal
from models import ChatMessage, ChatSession, Customer
from . import guardrails, metrics as chat_metrics
from .llm_adapter import LLMMessage

DEFAULT_TITLE = "新对话"
TITLE_MAX_CHARS = 16
DEFAULT_SCENARIO = "free_qa"
SCENARIOS = ("assessment", "free_qa")
_DEFAULT_QUICK_QUESTIONS = {"assessment": "请为该客户生成综合评估结论"}
_STREAMING_SESSION_IDS: set[int] = set()
_STREAMING_LOCK = threading.Lock()

def is_session_streaming(session_id: int) -> bool:
    """当前进程内该会话是否正在生成（SSE 或完整轮次均登记）。"""
    return session_id in _STREAMING_SESSION_IDS

def _claim_streaming(session_id: int) -> bool:
    """登记会话为"正在生成"；已被占用（并发生成）时返回 False。"""
    with _STREAMING_LOCK:
        if session_id in _STREAMING_SESSION_IDS:
            return False
        _STREAMING_SESSION_IDS.add(session_id)
        return True

def _release_streaming(session_id: int) -> None:
    """生成结束（含异常 / 客户端断开）后释放登记。"""
    with _STREAMING_LOCK:
        _STREAMING_SESSION_IDS.discard(session_id)

class ChatSessionNotFound(LookupError):
    pass

@dataclass
class TurnEvent:
    type: str
    data: dict = field(default_factory=dict)

def create_session(
    db: Session,
    *,
    title: str = "",
    customer_id: int | None = None,
    scenario: str = DEFAULT_SCENARIO,
    system_prompt: str = "",
) -> ChatSession:
    scenario = scenario if scenario in SCENARIOS else DEFAULT_SCENARIO
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer_id and customer is None:
        raise ValueError("关联的客户不存在")

    if not title:
        title = f"{customer.customer_name} · AI 评估" if customer else DEFAULT_TITLE

    safe_system_prompt, _ = guardrails.sanitize_for_outbound(
        system_prompt, max_chars=2000
    )
    session = ChatSession(
        title=title[:200],
        customer_id=customer.id if customer else None,
        scenario=scenario,
        system_prompt=safe_system_prompt,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session

def list_sessions(db: Session, *, customer_id: int | None = None, limit: int = 50) -> list[ChatSession]:
    query = db.query(ChatSession)
    if customer_id:
        query = query.filter(ChatSession.customer_id == customer_id)
    return query.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc()).limit(limit).all()

def get_session(db: Session, session_id: int) -> ChatSession:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise ChatSessionNotFound(f"会话 {session_id} 不存在")
    return session

def delete_session(db: Session, session_id: int) -> None:
    session = get_session(db, session_id)
    db.delete(session)
    db.commit()

def set_feedback(db: Session, message_id: int, feedback: str) -> ChatMessage:
    message = db.get(ChatMessage, message_id)
    if message is None:
        raise LookupError(f"消息 {message_id} 不存在")
    message.feedback = feedback
    db.commit()
    db.refresh(message)
    return message

def _history_messages(db: Session, session: ChatSession, exclude_id: int | None = None) -> list[LLMMessage]:
    """取最近 N 条历史消息，并按字符预算从旧到新裁剪（控制 Token 成本）。"""
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session.id,
            ChatMessage.role.in_(("user", "assistant")),
            ChatMessage.superseded_by_id.is_(None),
        )
        .order_by(ChatMessage.id.desc())
        .limit(config.CHAT_MAX_CONTEXT_MESSAGES)
        .all()
    )
    rows = [r for r in reversed(rows) if exclude_id is None or r.id != exclude_id]
    from services.ai.repository import resolve_messages
    resolved = resolve_messages(db, rows, "model_history")
    budget = config.CHAT_CONTEXT_CHAR_BUDGET
    picked = []
    for row in reversed(resolved):
        safe = guardrails.sanitize_for_outbound(row["content"])[0]
        if budget <= 0:
            break
        safe = safe[:budget]
        budget -= len(safe)
        picked.append(LLMMessage(role=row["role"], content=safe))
    picked.reverse()
    return picked

def _resolve_customer(db: Session, session: ChatSession, customer_id: int | None) -> Customer | None:
    if customer_id and session.customer_id and customer_id != session.customer_id:
        raise ValueError(
            f"当前会话已绑定客户 {session.customer_id}，不能改用客户 {customer_id}；"
            "请为目标客户新建会话"
        )
    target_id = customer_id or session.customer_id
    if not target_id:
        return None
    return db.get(Customer, target_id)

def _auto_title(session: ChatSession, question: str, customer: Customer | None, scenario: str) -> None:
    if session.title and session.title != DEFAULT_TITLE:
        return
    if customer and scenario == "assessment":
        session.title = f"{customer.customer_name} · AI 评估"
    elif question:
        cleaned = question.strip().replace("\n", " ")
        session.title = cleaned[:TITLE_MAX_CHARS] + ("…" if len(cleaned) > TITLE_MAX_CHARS else "")
    elif customer:
        session.title = f"{customer.customer_name} · 客情分析"

def run_turn(
    db: Session,
    session: ChatSession,
    *,
    content: str = "",
    scenario: str | None = None,
    customer_id: int | None = None,
    persist_user: bool = True,
    exclude_message_id: int | None = None,
    supersede_message_id: int | None = None,
    idempotency_key=None,
    request_identity=None,
) -> Iterator[TurnEvent]:
    """执行一轮对话，产出事件流。异常一律转成 error 事件，不向上抛。"""
    from services.ai.chat import run_turn as run_current
    yield from run_current(
        db, session, content=content, scenario=scenario, customer_id=customer_id,
        persist_user=persist_user, exclude_message_id=exclude_message_id,
        supersede_message_id=supersede_message_id,
        idempotency_key=idempotency_key, request_identity=request_identity,
    )


def sse(event: TurnEvent) -> str:
    payload = json.dumps(event.data, ensure_ascii=False, default=str)
    return f"event: {event.type}\ndata: {payload}\n\n"

def _regeneration_error(event: TurnEvent, retained_message_id: int | None) -> TurnEvent:
    """错误不提交草稿；向客户端明确指出仍然有效的原回复。"""
    if event.type != "error" or retained_message_id is None:
        return event
    data = dict(event.data)
    data["retained_message_id"] = retained_message_id
    if "已保留原答案" not in str(data.get("message") or ""):
        data["message"] = "重新生成未完成，已保留原答案。" + str(data.get("message") or "")
    return TurnEvent("error", data)

def stream_sse(
    session_id: int,
    *,
    content: str = "",
    scenario: str | None = None,
    customer_id: int | None = None,
    regenerate: bool = False,
    idempotency_key: str | None = None,
    request_identity=None,
) -> Iterator[str]:
    """SSE 生成器。

    **自己开 DB Session**：FastAPI 0.106+ 的 ``Depends(get_db)`` 会在响应体开始
    推流前就执行清理，流式过程中不能再用请求级 Session。
    """
    db = SessionLocal()
    session: ChatSession | None = None
    claimed = False
    observer: chat_metrics.TurnMetricObserver | None = None
    exclude_id: int | None = None
    try:
        session = db.get(ChatSession, session_id)
        if session is None:
            yield sse(TurnEvent("error", {"message": "会话不存在"}))
            return

        if not _claim_streaming(session.id):
            yield sse(TurnEvent("error", {"message": "该会话正在生成中，请稍候"}))
            return
        claimed = True
        exclude_id = None
        if regenerate:
            content, exclude_id = prepare_regenerate(db, session, scenario=scenario)
            if not content:
                yield sse(TurnEvent("error", {"message": "没有可重新生成的消息"}))
                return
        observer = chat_metrics.TurnMetricObserver(
            session_id=session.id,
            scenario=scenario if scenario in SCENARIOS else (session.scenario or DEFAULT_SCENARIO),
        )
        for event in run_turn(
            db,
            session,
            content=content,
            scenario=scenario,
            customer_id=customer_id,
            persist_user=not regenerate,
            exclude_message_id=exclude_id,
            supersede_message_id=exclude_id if regenerate else None,
            idempotency_key=idempotency_key,
            request_identity=request_identity,
        ):
            event = _regeneration_error(event, exclude_id if regenerate else None)
            observer.consume(event)
            yield sse(event)
    except Exception as exc:  # noqa: BLE001
        event = _regeneration_error(
            TurnEvent("error", {"message": str(exc)}), exclude_id if regenerate else None,
        )
        if observer is not None:
            observer.consume(event)
        yield sse(event)
    finally:
        # 流式取消或异常可能发生在策略/引用事件之后。指标使用独立提交，
        # 因此先撤销尚未随完整消息提交的业务变更，避免指标顺带保存草稿策略。
        db.rollback()
        if observer is not None:
            observer.persist(db)
        if claimed:
            _release_streaming(session.id if session is not None else session_id)
        db.close()

def prepare_regenerate(
    db: Session, session: ChatSession, scenario: str | None = None
) -> tuple[str, int | None]:
    """定位待替换回复与原提问；旧回复保持不可变，直到新回复成功。

    仅当最后一条 assistant 回复晚于最后一条用户提问（即它是对该提问的应答）时
    才选择为替换目标；若上一轮在持久化 assistant 前失败，last_assistant 属于更早的轮次，
    替换会造成历史消息丢失。快捷场景（评估/策略/预警）历史上可能没有用户提问，
    此时按场景兜底一个默认提问用于重放。
    """
    messages = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == session.id,
            ChatMessage.superseded_by_id.is_(None),
        )
        .order_by(ChatMessage.id.desc())
        .limit(6)
        .all()
    )
    last_user = next((m for m in messages if m.role == "user"), None)
    last_assistant = next((m for m in messages if m.role == "assistant"), None)
    if last_user is None:
        # 历史上没有用户提问（如快捷评估）：按场景兜底一个默认提问用于重放
        content = _DEFAULT_QUICK_QUESTIONS.get(scenario or session.scenario or "", "")
        if content and last_assistant is not None:
            return content, last_assistant.id
        return "", None
    if last_assistant is not None and last_assistant.id > last_user.id:
        return last_user.content, last_assistant.id
    return last_user.content, None

def complete_turn(
    db: Session,
    session: ChatSession,
    *,
    content: str = "",
    scenario: str | None = None,
    customer_id: int | None = None,
    persist_user: bool = True,
    exclude_message_id: int | None = None,
    supersede_message_id: int | None = None,
    idempotency_key: str | None = None,
    request_identity=None,
) -> dict:
    """非流式执行一轮对话，返回汇总结果（测试、报告整合、批处理使用）。"""
    result: dict[str, Any] = {
        "session_id": session.id,
        "message": None,
        "assessment": None,
        "trend": None,
        "strategy_items": [],
        "references": [],
        "degraded": False,
        "tokens_used": 0,
        "latency_ms": 0,
        "timings": {},
        "warnings": [],
        "error": "",
    }

    if not _claim_streaming(session.id):
        result["error"] = "该会话正在生成中，请稍候"
        return result
    observer = chat_metrics.TurnMetricObserver(
        session_id=session.id,
        scenario=scenario if scenario in SCENARIOS else (session.scenario or DEFAULT_SCENARIO),
    )
    try:
        for event in run_turn(
            db,
            session,
            content=content,
            scenario=scenario,
            customer_id=customer_id,
            persist_user=persist_user,
            exclude_message_id=exclude_message_id,
            supersede_message_id=supersede_message_id,
            idempotency_key=idempotency_key,
            request_identity=request_identity,
        ):
            event = _regeneration_error(event, supersede_message_id)
            observer.consume(event)
            if event.type == "context":
                result["assessment"] = event.data.get("assessment")
                result["trend"] = event.data.get("trend")
            elif event.type == "strategy":
                result["strategy_items"] = event.data.get("items") or []
            elif event.type == "references":
                result["references"] = event.data.get("items") or []
            elif event.type == "warning":
                result["warnings"].append(event.data.get("message", ""))
            elif event.type == "error":
                result["error"] = event.data.get("message", "")
                result["error_code"] = event.data.get("error_code", "")
                result["status"] = event.data.get("status", "error")
                result["run_id"] = event.data.get("run_id")
            elif event.type == "done":
                result["run_id"] = event.data.get("run_id")
                result["status"] = "degraded" if event.data.get("degraded") else "succeeded"
                result["message"] = event.data.get("message")
                result["degraded"] = bool(event.data.get("degraded"))
                result["tokens_used"] = event.data.get("tokens_used", 0)
                result["latency_ms"] = event.data.get("latency_ms", 0)
                result["timings"] = event.data.get("timings") or {}
    except Exception as exc:
        event = _regeneration_error(TurnEvent("error", {"message": str(exc)}), supersede_message_id)
        observer.consume(event)
        if supersede_message_id is None:
            raise
        result["error"] = event.data["message"]
    finally:
        db.rollback()
        observer.persist(db)
        _release_streaming(session.id)

    return result
