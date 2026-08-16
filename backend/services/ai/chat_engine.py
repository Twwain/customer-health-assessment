"""AI 对话编排。

职责：
- 会话与消息的持久化（多轮上下文窗口按条数 + 字符预算双重限制）
- 把量化评估结果注入 Prompt（context_builder），按场景选模板（prompt_templates）
- 以事件流驱动 SSE 流式输出（首字延迟 < 2s 由 LLM 侧保证，这里不做额外缓冲）
- LLM 不可用时**自动降级**为规则引擎回复，对话不中断

事件协议（SSE ``event:`` 名 → ``data:`` JSON）：

| event      | 时机           | data |
|------------|----------------|------|
| start      | 开始生成       | session_id / scenario / degraded / model / user_message_id |
| context    | 有客户上下文时 | assessment / trend（前端 HealthCard 直接渲染） |
| delta      | 每个增量       | text |
| replace    | 草稿需替换     | text / reason（工具续写或精炼时清空旧草稿） |
| strategy   | 策略解析完成   | items（结构化策略条目） |
| references | 有知识引用时   | items |
| warning    | 生成中断等     | message |
| done       | 结束           | message（落库后的完整消息）/ tokens_used / latency_ms / degraded |
| error      | 不可恢复错误   | message |
"""

from __future__ import annotations

import datetime
import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from sqlalchemy.orm import Session

import config
from database import SessionLocal, utcnow
from models import ChatMessage, ChatSession, Customer
from services import assessment_history
from services.scoring import get_scoring_strategy

from . import guardrails
from .context_builder import ChatContext, build_context
from .fallback import build_degraded_reply
from .llm_adapter import (
    LLMError,
    LLMMessage,
    LLMUnavailableError,
    estimate_tokens,
    get_chat_adapter,
)
from .prompt_templates import DEFAULT_SCENARIO, SCENARIOS, get_template
from .strategy import generate, split_strategy_payload
from .tools import MAX_TOOL_ROUNDS, TOOL_SCHEMAS, append_tool_results

logger = logging.getLogger(__name__)


DEFAULT_TITLE = "新对话"
TITLE_MAX_CHARS = 16

# 进程内"正在生成"的会话注册表（单进程部署假设）：
# 相比把标记写进数据库，内存集合没有"崩溃/杀进程后永久滞留"的脏状态问题，
# 也天然支持同一会话并发生成的互斥判定。会话详情 / 列表接口据此返回 streaming。
_STREAMING_SESSION_IDS: set[int] = set()
_STREAMING_LOCK = threading.Lock()
AGENT_CANCEL_JOIN_SECONDS = 0.25


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


# 引用标题归一化：去掉常见书刊引号/括号包装后做精确比较，
# 避免"风险"这类短词子串匹配把无关知识误标为"已引用"。
_REF_WRAP_CHARS = "《》「」“”‘’\"'（）()【】<>〈〉，。、"
_REF_WRAP_STRIP_RE = re.compile(f"^[{re.escape(_REF_WRAP_CHARS)}]+|[{re.escape(_REF_WRAP_CHARS)}]+$")


def _normalize_ref_title(title: str | Any) -> str:
    return _REF_WRAP_STRIP_RE.sub("", str(title or "").strip())

# 快捷入口（AI 评估 / 策略 / 预警）没有用户输入时使用的默认提问：
# 落库展示与「重新生成」重放共用同一份文案
_DEFAULT_QUICK_QUESTIONS = {
    "assessment": "请为该客户生成综合评估结论",
    "strategy": "请为该客户生成客情维护策略建议",
    "alert_analysis": "请解读该客户当前的风险预警",
}

# 对话内摘要提示：完整因子明细 / 风险预警 / 策略细则见生成报告
# （生成 PDF 含分维度因子明细、风险提示与建议、AI 智能策略建议）
_SCENARIO_SUMMARY_HINTS = {
    "assessment": "本页面仅显示评估摘要，完整报告请点击「生成报告」。",
    "strategy": "本页面仅显示策略摘要，完整报告请点击「生成报告」。",
    "alert_analysis": "本页面仅显示风险摘要，完整报告请点击「生成报告」。",
}

# M4：走 Agent Loop（检索→推理→自批判→精炼）的场景；其余走直接流式
AGENT_SCENARIOS = {"assessment", "strategy", "alert_analysis"}


class ChatSessionNotFound(LookupError):
    pass


@dataclass
class TurnEvent:
    type: str
    data: dict = field(default_factory=dict)


# ══════════════════════════ 会话 CRUD ═══════════════════════════════════════


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

    session = ChatSession(
        title=title[:200],
        customer_id=customer.id if customer else None,
        scenario=scenario,
        system_prompt=system_prompt,
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


# ══════════════════════════ 上下文窗口 ══════════════════════════════════════


def _history_messages(db: Session, session: ChatSession, exclude_id: int | None = None) -> list[LLMMessage]:
    """取最近 N 条历史消息，并按字符预算从旧到新裁剪（控制 Token 成本）。"""
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id, ChatMessage.role.in_(("user", "assistant")))
        .order_by(ChatMessage.id.desc())
        .limit(config.CHAT_MAX_CONTEXT_MESSAGES)
        .all()
    )
    rows = [r for r in reversed(rows) if exclude_id is None or r.id != exclude_id]

    budget = config.CHAT_CONTEXT_CHAR_BUDGET
    picked: list[ChatMessage] = []
    for row in reversed(rows):  # 从最新往回收，保证近端上下文优先
        cost = len(row.content or "")
        if cost > budget and picked:
            break
        budget -= cost
        picked.append(row)
    picked.reverse()

    return [LLMMessage(role=r.role, content=r.content or "") for r in picked]


def _build_llm_messages(
    db: Session,
    session: ChatSession,
    *,
    scenario: str,
    question: str,
    ctx: ChatContext,
    exclude_message_id: int | None = None,
) -> tuple[list[LLMMessage], Any]:
    """system(模板+护栏) + 历史消息 + 本轮(模板注入最新上下文)。

    历史消息保留用户原话，只有**当前轮**携带上下文，避免旧上下文反复占 Token 且过期。
    """
    template = get_template(scenario)
    today = datetime.date.today().isoformat()

    system_text = template.render_system(today=today)
    if session.system_prompt:
        system_text = f"{system_text}\n\n【会话附加设定】\n{session.system_prompt}"

    user_text = template.render_user(
        today=today,
        customer_context=ctx.customer_text,
        knowledge_context=ctx.knowledge_text,
        alert_context=ctx.alert_text,
        question=question,
    )

    messages = [LLMMessage(role="system", content=system_text)]
    messages += _history_messages(db, session, exclude_id=exclude_message_id)
    messages.append(LLMMessage(role="user", content=user_text))
    return messages, template


# ══════════════════════════ 一轮对话 ════════════════════════════════════════


def _resolve_customer(db: Session, session: ChatSession, customer_id: int | None) -> Customer | None:
    target_id = customer_id or session.customer_id
    if not target_id:
        return None
    return db.get(Customer, target_id)


def _auto_title(session: ChatSession, question: str, customer: Customer | None, scenario: str) -> None:
    if session.title and session.title != DEFAULT_TITLE:
        return
    if customer and scenario in ("assessment", "strategy", "alert_analysis"):
        session.title = f"{customer.customer_name} · {'AI 评估' if scenario == 'assessment' else '策略建议' if scenario == 'strategy' else '预警解读'}"
    elif question:
        cleaned = question.strip().replace("\n", " ")
        session.title = cleaned[:TITLE_MAX_CHARS] + ("…" if len(cleaned) > TITLE_MAX_CHARS else "")
    elif customer:
        session.title = f"{customer.customer_name} · 客情分析"


def _message_payload(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "session_id": message.session_id,
        "role": message.role,
        "content": message.content,
        "references": message.references or [],
        "strategy_items": message.strategy_items or [],
        "tokens_used": message.tokens_used,
        "feedback": message.feedback or "",
        "degraded": bool(message.degraded),
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _merge_references(*groups: list[dict] | None) -> list[dict]:
    """合并多组知识引用并按 id 去重（预取 + 工具检索）。"""
    seen: set[str] = set()
    merged: list[dict] = []
    for group in groups:
        for ref in group or []:
            rid = ref.get("id") or ""
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(ref)
    return merged


def _message_chars(messages: list[LLMMessage | dict]) -> int:
    return sum(
        len(str(m.content if isinstance(m, LLMMessage) else m.get("content", "")))
        for m in messages
    )


class _VisibleAgentStream:
    """过滤策略机器 JSON，并对可见 Agent 增量做跨 chunk 脱敏。"""

    def __init__(self) -> None:
        self._pending = ""
        self._inside_json = False
        self._sanitizer = guardrails.StreamingOutputSanitizer()

    def feed(self, text: str) -> str:
        self._pending += text or ""
        visible: list[str] = []
        while self._pending:
            if self._inside_json:
                end = self._pending.find("```")
                if end < 0:
                    self._pending = self._pending[-2:]
                    break
                self._pending = self._pending[end + 3:]
                self._inside_json = False
                continue
            match = re.search(r"```json\s*", self._pending, re.IGNORECASE)
            if match is None:
                keep = min(6, len(self._pending))
                if len(self._pending) > keep:
                    visible.append(self._pending[:-keep])
                    self._pending = self._pending[-keep:]
                break
            visible.append(self._pending[:match.start()])
            self._pending = self._pending[match.end():]
            self._inside_json = True
        return self._sanitizer.feed("".join(visible))

    def flush(self) -> str:
        visible = "" if self._inside_json else self._pending
        self._pending = ""
        self._inside_json = False
        return self._sanitizer.feed(visible) + self._sanitizer.flush()


def run_turn(
    db: Session,
    session: ChatSession,
    *,
    content: str = "",
    scenario: str | None = None,
    customer_id: int | None = None,
    persist_user: bool = True,
    exclude_message_id: int | None = None,
) -> Iterator[TurnEvent]:
    """执行一轮对话，产出事件流。异常一律转成 error 事件，不向上抛。"""
    started = time.monotonic()
    timings: dict[str, Any] = {
        "scoring_ms": 0, "context_ms": 0, "rag_ms": 0,
        "embedding_ms": 0, "vector_query_ms": 0, "rerank_ms": 0,
        "generation_ms": 0, "tool_ms": 0, "persist_ms": 0,
        "time_to_first_client_delta_ms": None,
        "llm_calls": [], "llm_call_count": 0,
        "tool_rounds": 0, "refine_rounds": 0,
    }
    scenario = scenario if scenario in SCENARIOS else (session.scenario or DEFAULT_SCENARIO)
    question, hits = guardrails.sanitize_input(content)
    if not question and persist_user and scenario in ("assessment", "strategy", "alert_analysis"):
        # 快捷入口（AI 评估 / 策略 / 预警）没有用户输入：落一条默认提问，
        # 便于历史展示与「重新生成」重放
        question = _DEFAULT_QUICK_QUESTIONS[scenario]

    customer = _resolve_customer(db, session, customer_id)
    if customer and not session.customer_id:
        session.customer_id = customer.id

    persist_started = time.monotonic()
    user_message: ChatMessage | None = None
    if persist_user and question:
        user_message = ChatMessage(session_id=session.id, role="user", content=question)
        db.add(user_message)
        db.flush()

    _auto_title(session, question, customer, scenario)
    db.commit()
    timings["persist_ms"] = int((time.monotonic() - persist_started) * 1000)

    adapter = get_chat_adapter()
    yield TurnEvent(
        "start",
        {
            "session_id": session.id,
            "scenario": scenario,
            "user_message_id": user_message.id if user_message else None,
            "model": getattr(adapter, "model", ""),
            "degraded": not getattr(adapter, "available", False),
            "sanitized": hits,
        },
    )

    # ① 量化评估引擎 —— 先算分，再让 AI 解读
    assessment = None
    scoring_started = time.monotonic()
    if customer is not None:
        assessment = get_scoring_strategy().evaluate(customer)
        if scenario == "assessment":
            # AI 评估同时留痕，趋势曲线才有数据点（去重逻辑避免刷屏）
            assessment_history.record_assessment(
                db, customer, assessment, assessed_by="ai", trigger="ai_assessment"
            )
    timings["scoring_ms"] = int((time.monotonic() - scoring_started) * 1000)

    # Agent 场景由 Agent Loop 自行检索（tools.rag_retrieve），这里不重复检索：
    # 避免同一轮对话双重 embedding 调用与知识 hit_count 重复计数
    context_started = time.monotonic()
    ctx = build_context(
        db,
        customer,
        assessment=assessment,
        query=question,
        retrieve_knowledge=scenario not in AGENT_SCENARIOS,
        rag_timings=timings,
    )
    timings["context_ms"] = int((time.monotonic() - context_started) * 1000)
    if ctx.assessment is not None:
        yield TurnEvent(
            "context",
            {
                "assessment": json.loads(ctx.assessment.model_dump_json()),
                "trend": json.loads(ctx.trend.model_dump_json()) if ctx.trend else None,
            },
        )

    # ③ 生成：agent 场景走 M4 Agent Loop，其余走直接流式
    # 本轮提问已经落库，要从历史窗口里剔除，否则会和模板化的当前轮重复一次
    exclude_id = exclude_message_id or (user_message.id if user_message else None)

    chunks: list[str] = []
    tool_refs: list[dict[str, Any]] = []
    # 工具循环有多轮流式请求：每轮取最后一次 usage 再累计到总 token 数，
    # 既避免只统计最后一轮，也避免同一轮内 usage 重复出现时虚高。
    used_tokens = 0
    round_tokens = 0
    round_usage: dict[str, Any] = {}

    def _collect_usage(u: dict[str, Any]) -> None:
        nonlocal round_tokens
        round_tokens = int(u.get("total_tokens") or 0)
        round_usage.update(u)

    degraded_items: list[dict] = []
    degraded = False
    warnings: list[str] = []
    agent_refs: list[dict] | None = None
    agent_displayed_text: str | None = None
    generation_started = time.monotonic()

    def _mark_client_delta() -> None:
        if timings["time_to_first_client_delta_ms"] is None:
            timings["time_to_first_client_delta_ms"] = int((time.monotonic() - started) * 1000)

    if scenario in AGENT_SCENARIOS:
        # M4：评估 / 策略 / 预警解读走 Agent Loop（检索→推理→自批判→精炼）
        event_queue: queue.Queue[tuple[str, dict] | None] = queue.Queue()
        agent_result: dict[str, Any] = {}
        agent_cancelled = threading.Event()
        customer_id_for_worker = customer.id if customer is not None else None

        def _run_agent() -> None:
            # 使用同一 Engine 创建独立 Session，既保持测试/多数据库绑定一致，
            # 又避免跨线程复用调用方的 Session。
            worker_db = Session(bind=db.get_bind())
            try:
                worker_customer = (
                    worker_db.get(Customer, customer_id_for_worker)
                    if customer_id_for_worker is not None else None
                )
                worker_ctx = replace(ctx, customer=worker_customer)
                agent_result["value"] = generate(
                    scenario, worker_ctx, worker_customer, worker_db,
                    adapter=adapter, question=question,
                    tools_enabled=config.LLM_TOOLS_ENABLED,
                    thinking_enabled=config.LLM_CHAT_THINKING_ENABLED,
                    cancel_event=agent_cancelled,
                    on_event=lambda event_type, data: (
                        event_queue.put((event_type, data))
                        if not agent_cancelled.is_set() else None
                    ),
                )
            except BaseException as exc:  # 传回主生成器，沿用外层 SSE 兜底
                agent_result["error"] = exc
            finally:
                worker_db.close()
                event_queue.put(None)

        agent_thread = threading.Thread(
            target=_run_agent, name=f"chat-agent-{session.id}", daemon=True
        )
        agent_thread.start()
        visible_stream = _VisibleAgentStream()
        agent_displayed_text = ""
        try:
            while True:
                queued = event_queue.get()
                if queued is None:
                    break
                event_type, data = queued
                if event_type == "delta":
                    text = visible_stream.feed(str(data.get("text") or ""))
                    if text:
                        _mark_client_delta()
                        agent_displayed_text += text
                        yield TurnEvent("delta", {"text": text, "phase": data.get("phase")})
                elif event_type == "replace":
                    visible_stream = _VisibleAgentStream()
                    agent_displayed_text = ""
                    yield TurnEvent("replace", {**data, "text": ""})
        finally:
            agent_cancelled.set()
            agent_thread.join(timeout=AGENT_CANCEL_JOIN_SECONDS)
        if agent_thread.is_alive():
            return
        tail = visible_stream.flush()
        if tail:
            _mark_client_delta()
            agent_displayed_text += tail
            yield TurnEvent("delta", {"text": tail, "phase": "final"})
        if "error" in agent_result:
            raise agent_result["error"]
        gen = agent_result["value"]
        degraded = gen.degraded
        degraded_items = gen.degraded_items
        agent_refs = gen.references
        timings.update(gen.timings)
        chunks.append(gen.text)
        warnings.extend(gen.warnings)
        for w in gen.warnings:
            yield TurnEvent("warning", {"message": w})
    else:
        messages, template = _build_llm_messages(
            db, session, scenario=scenario, question=question, ctx=ctx, exclude_message_id=exclude_id
        )
        try:
            tool_rounds = 0
            exclude_ids = {ref.get("id") for ref in ctx.references}
            tools_enabled = config.LLM_TOOLS_ENABLED
            while True:
                round_tokens = 0
                round_usage.clear()
                tool_calls: list[dict[str, Any]] = []

                def _collect_tool_calls(items: list[dict[str, Any]]) -> None:
                    tool_calls.extend(items)

                call_started = time.monotonic()
                call_ttft_ms: int | None = None
                call_output_chars = 0
                visible_sanitizer = guardrails.StreamingOutputSanitizer()
                stream = adapter.stream_chat_completion(
                    messages,
                    temperature=template.temperature,
                    max_tokens=min(template.max_tokens, config.LLM_MAX_TOKENS),
                    on_usage=_collect_usage,
                    tools=TOOL_SCHEMAS if tools_enabled else None,
                    on_tool_calls=_collect_tool_calls,
                    extra={"thinking": {
                        "type": "enabled" if config.LLM_CHAT_THINKING_ENABLED else "disabled"
                    }},
                )
                try:
                    for delta in stream:
                        if call_ttft_ms is None:
                            call_ttft_ms = int((time.monotonic() - call_started) * 1000)
                        call_output_chars += len(delta)
                        chunks.append(delta)
                        visible_delta = visible_sanitizer.feed(delta)
                        if visible_delta:
                            _mark_client_delta()
                            yield TurnEvent("delta", {"text": visible_delta})
                    visible_tail = visible_sanitizer.flush()
                    if visible_tail:
                        _mark_client_delta()
                        yield TurnEvent("delta", {"text": visible_tail})
                finally:
                    duration_ms = int((time.monotonic() - call_started) * 1000)
                    completion_tokens = int(round_usage.get("completion_tokens") or 0)
                    timings["llm_calls"].append({
                        "phase": "direct" if tool_rounds == 0 else "tool_followup",
                        "ttft_ms": call_ttft_ms,
                        "duration_ms": duration_ms,
                        "message_count": len(messages),
                        "prompt_chars": _message_chars(messages),
                        "output_chars": call_output_chars,
                        "max_tokens": min(template.max_tokens, config.LLM_MAX_TOKENS),
                        "thinking_enabled": config.LLM_CHAT_THINKING_ENABLED,
                        "prompt_tokens": int(round_usage.get("prompt_tokens") or 0),
                        "completion_tokens": completion_tokens,
                        "total_tokens": int(round_usage.get("total_tokens") or 0),
                        "completion_tokens_per_second": (
                            round(completion_tokens * 1000 / duration_ms, 2)
                            if completion_tokens and duration_ms else None
                        ),
                    })
                used_tokens += round_tokens
                if not tool_calls:
                    break
                tool_rounds += 1
                timings["tool_rounds"] = tool_rounds
                tool_started = time.monotonic()
                messages, refs = append_tool_results(
                    messages,
                    tool_calls,
                    customer=customer,
                    db=db,
                    exclude_ids=exclude_ids,
                )
                timings["tool_ms"] += int((time.monotonic() - tool_started) * 1000)
                tool_refs.extend(refs)
                exclude_ids.update(ref["id"] for ref in refs)
                if tool_rounds >= MAX_TOOL_ROUNDS:
                    # 已回填本轮工具结果：不带工具收尾一次，避免空回复
                    warnings.append("工具调用次数已达上限，已基于已获取信息作答")
                    round_tokens = 0
                    round_usage.clear()
                    call_started = time.monotonic()
                    call_ttft_ms = None
                    call_output_chars = 0
                    visible_sanitizer = guardrails.StreamingOutputSanitizer()
                    stream = adapter.stream_chat_completion(
                        messages,
                        temperature=template.temperature,
                        max_tokens=min(template.max_tokens, config.LLM_MAX_TOKENS),
                        on_usage=_collect_usage,
                        tools=None,
                        extra={"thinking": {
                            "type": "enabled" if config.LLM_CHAT_THINKING_ENABLED else "disabled"
                        }},
                    )
                    try:
                        for delta in stream:
                            if call_ttft_ms is None:
                                call_ttft_ms = int((time.monotonic() - call_started) * 1000)
                            call_output_chars += len(delta)
                            chunks.append(delta)
                            visible_delta = visible_sanitizer.feed(delta)
                            if visible_delta:
                                _mark_client_delta()
                                yield TurnEvent("delta", {"text": visible_delta})
                        visible_tail = visible_sanitizer.flush()
                        if visible_tail:
                            _mark_client_delta()
                            yield TurnEvent("delta", {"text": visible_tail})
                    finally:
                        duration_ms = int((time.monotonic() - call_started) * 1000)
                        completion_tokens = int(round_usage.get("completion_tokens") or 0)
                        timings["llm_calls"].append({
                            "phase": "tool_final", "ttft_ms": call_ttft_ms,
                            "duration_ms": duration_ms,
                            "message_count": len(messages),
                            "prompt_chars": _message_chars(messages),
                            "output_chars": call_output_chars,
                            "max_tokens": min(template.max_tokens, config.LLM_MAX_TOKENS),
                            "thinking_enabled": config.LLM_CHAT_THINKING_ENABLED,
                            "prompt_tokens": int(round_usage.get("prompt_tokens") or 0),
                            "completion_tokens": completion_tokens,
                            "total_tokens": int(round_usage.get("total_tokens") or 0),
                            "completion_tokens_per_second": (
                                round(completion_tokens * 1000 / duration_ms, 2)
                                if completion_tokens and duration_ms else None
                            ),
                        })
                    used_tokens += round_tokens
                    break
        except LLMUnavailableError as exc:
            degraded = True
            chunks.clear()
            text, degraded_items = build_degraded_reply(scenario, ctx, question)
            yield TurnEvent("warning", {"message": f"LLM 不可用，已降级为规则引擎：{exc}"})
            for piece in _slice_text(text):
                _mark_client_delta()
                chunks.append(piece)
                yield TurnEvent("delta", {"text": piece})
        except LLMError as exc:  # 已经吐了一部分字，保留残文并提示
            warnings.append(f"生成中断：{exc}")
            yield TurnEvent("warning", {"message": f"生成中断：{exc}"})
        except Exception as exc:  # noqa: BLE001 - 兜底，保证前端一定收到结束事件
            degraded = True
            chunks.clear()
            text, degraded_items = build_degraded_reply(scenario, ctx, question)
            yield TurnEvent("warning", {"message": f"对话异常，已降级为规则引擎：{exc}"})
            for piece in _slice_text(text):
                _mark_client_delta()
                chunks.append(piece)
                yield TurnEvent("delta", {"text": piece})

    timings["generation_ms"] = int((time.monotonic() - generation_started) * 1000)
    timings["llm_call_count"] = len(timings["llm_calls"])
    raw_text = "".join(chunks)
    if warnings:
        raw_text = f"{raw_text}\n\n" + "\n\n".join(f"> ⚠️ {w}" for w in warnings)
    summary_hint = _SCENARIO_SUMMARY_HINTS.get(scenario)
    if summary_hint and ctx.assessment is not None:
        # 会话内只展示摘要；完整因子明细 / 风险预警 / 策略细则见生成报告
        raw_text = raw_text.rstrip() + f"\n\n> 💡 {summary_hint}"

    body, items = split_strategy_payload(guardrails.sanitize_output(raw_text))
    if agent_displayed_text is not None and agent_displayed_text != body:
        yield TurnEvent("replace", {"text": body, "reason": "final_sync"})
        _mark_client_delta()
    if degraded:
        items = degraded_items
    if items:
        yield TurnEvent("strategy", {"items": items})
        if customer is not None:
            assessment_history.attach_strategy_snapshot(db, customer.id, items)

    # 知识溯源：agent 场景用其检索结果，其余用上下文注入的引用
    base_refs = agent_refs if agent_refs is not None else ctx.references
    final_refs = _merge_references(base_refs, tool_refs)
    # 标记「已引用」：策略条目的 reference 或正文「参考：标题」实际引用到的知识。
    # 归一化后做精确匹配，长标题（>=6 字）才允许子串匹配，避免短词误标。
    used_titles: set[str] = set()
    for it in items or []:
        u = _normalize_ref_title(it.get("reference"))
        if u:
            used_titles.add(u)
    for m in re.findall(r"参考[:：]\s*([^）)]+)", raw_text or ""):
        t = _normalize_ref_title(m)
        if t:
            used_titles.add(t)
    for ref in final_refs:
        t = _normalize_ref_title(ref.get("title"))
        if not t:
            ref["used"] = False
            continue
        matched = any(
            u == t or (len(u) >= 6 and u in t)
            for u in used_titles
        )
        ref["used"] = matched
    if final_refs:
        yield TurnEvent("references", {"items": final_refs})

    tokens = used_tokens
    if scenario in AGENT_SCENARIOS:
        tokens = sum(
            int(call.get("total_tokens") or 0)
            for call in timings.get("llm_calls", [])
        )
    if not tokens and not degraded:
        tokens = estimate_tokens(raw_text)
    persist_started = time.monotonic()
    assistant = ChatMessage(
        session_id=session.id,
        role="assistant",
        content=body,
        references=final_refs,
        strategy_items=items,
        tokens_used=tokens,
        degraded=degraded,
    )
    db.add(assistant)
    session.updated_at = utcnow()
    db.commit()
    db.refresh(assistant)
    timings["persist_ms"] += int((time.monotonic() - persist_started) * 1000)
    timings["total_ms"] = int((time.monotonic() - started) * 1000)
    logger.info("chat_turn_timing %s", json.dumps(
        {"session_id": session.id, "scenario": scenario, **timings},
        ensure_ascii=False, default=str,
    ))

    yield TurnEvent(
        "done",
        {
            "message": _message_payload(assistant),
            "tokens_used": tokens,
            "degraded": degraded,
            "latency_ms": timings["total_ms"],
            "timings": timings,
            "model": "" if degraded else getattr(adapter, "model", ""),
        },
    )


def _slice_text(text: str, size: int | None = None) -> Iterator[str]:
    """把整段文本切片模拟流式，保证降级时前端渲染体验一致。"""
    size = size or config.CHAT_DEGRADED_CHUNK_SIZE
    for start in range(0, len(text), size):
        yield text[start : start + size]


# ══════════════════════════ 对外入口 ════════════════════════════════════════


def sse(event: TurnEvent) -> str:
    payload = json.dumps(event.data, ensure_ascii=False, default=str)
    return f"event: {event.type}\ndata: {payload}\n\n"


def stream_sse(
    session_id: int,
    *,
    content: str = "",
    scenario: str | None = None,
    customer_id: int | None = None,
    regenerate: bool = False,
) -> Iterator[str]:
    """SSE 生成器。

    **自己开 DB Session**：FastAPI 0.106+ 的 ``Depends(get_db)`` 会在响应体开始
    推流前就执行清理，流式过程中不能再用请求级 Session。
    """
    db = SessionLocal()
    session: ChatSession | None = None
    claimed = False
    try:
        session = db.get(ChatSession, session_id)
        if session is None:
            yield sse(TurnEvent("error", {"message": "会话不存在"}))
            return

        exclude_id = None
        if regenerate:
            content, exclude_id = prepare_regenerate(db, session, scenario=scenario)
            if not content:
                yield sse(TurnEvent("error", {"message": "没有可重新生成的消息"}))
                return

        if not _claim_streaming(session.id):
            yield sse(TurnEvent("error", {"message": "该会话正在生成中，请稍候"}))
            return
        claimed = True
        for event in run_turn(
            db,
            session,
            content=content,
            scenario=scenario,
            customer_id=customer_id,
            persist_user=not regenerate,
            exclude_message_id=exclude_id,
        ):
            yield sse(event)
    except Exception as exc:  # noqa: BLE001
        yield sse(TurnEvent("error", {"message": str(exc)}))
    finally:
        if claimed:
            _release_streaming(session.id if session is not None else session_id)
        db.close()


def prepare_regenerate(
    db: Session, session: ChatSession, scenario: str | None = None
) -> tuple[str, int | None]:
    """删掉最后一条 assistant 回复，取回最后一条用户提问用于重跑。

    仅当最后一条 assistant 回复晚于最后一条用户提问（即它是对该提问的应答）时
    才删除；若上一轮在持久化 assistant 前失败，last_assistant 属于更早的轮次，
    删除会造成历史消息丢失。快捷场景（评估/策略/预警）历史上可能没有用户提问，
    此时按场景兜底一个默认提问用于重放。
    """
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
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
            db.delete(last_assistant)
            db.commit()
            return content, last_assistant.id
        return "", None
    if last_assistant is not None and last_assistant.id > last_user.id:
        db.delete(last_assistant)
        db.commit()
        return last_user.content, last_user.id
    return last_user.content, last_user.id


def complete_turn(
    db: Session,
    session: ChatSession,
    *,
    content: str = "",
    scenario: str | None = None,
    customer_id: int | None = None,
    persist_user: bool = True,
    exclude_message_id: int | None = None,
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
    try:
        for event in run_turn(
            db,
            session,
            content=content,
            scenario=scenario,
            customer_id=customer_id,
            persist_user=persist_user,
            exclude_message_id=exclude_message_id,
        ):
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
            elif event.type == "done":
                result["message"] = event.data.get("message")
                result["degraded"] = bool(event.data.get("degraded"))
                result["tokens_used"] = event.data.get("tokens_used", 0)
                result["latency_ms"] = event.data.get("latency_ms", 0)
                result["timings"] = event.data.get("timings") or {}
    finally:
        _release_streaming(session.id)

    return result
