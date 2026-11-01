"""AI 对话接口。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST   | /api/chat/sessions                       | 创建会话 |
| GET    | /api/chat/sessions                       | 会话列表 |
| GET    | /api/chat/sessions/{id}                  | 会话详情（含消息） |
| DELETE | /api/chat/sessions/{id}                  | 删除会话 |
| POST   | /api/chat/sessions/{id}/messages         | 发送消息（默认 SSE 流式） |
| POST   | /api/chat/sessions/{id}/evaluate         | 快捷评估 |
| POST   | /api/chat/sessions/{id}/regenerate       | 重新生成上一条回复（原型「🔄 重新生成」） |
| POST   | /api/chat/messages/{id}/feedback         | 消息点赞/点踩 |
| GET    | /api/chat/status                         | LLM 可用性（降级提示条数据源） |
| GET    | /api/chat/metrics                        | 对话运行指标分布（不含业务正文） |

流式响应统一 ``text/event-stream``，事件协议见 ``services.ai.chat_engine`` 文档。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from models import ChatMessage, ChatSession, Customer, MESSAGE_FEEDBACKS
from schemas import (
    ChatMessageItem,
    ChatRequest,
    ChatSessionCreate,
    ChatSessionDetail,
    ChatSessionItem,
    ChatSessionListResponse,
    ChatTurnResponse,
    LLMStatusResponse,
    MessageFeedbackRequest,
    MessageFeedbackResponse,
)
from services.ai import chat_engine, llm_adapter, metrics as chat_metrics
from services.ai.protocol import ProtocolError, load_runtime_assets

router = APIRouter(prefix="/chat", tags=["AI 对话"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关闭 Nginx 缓冲，否则流式会被攒成一整块（首字延迟指标会崩）
    "X-Accel-Buffering": "no",
}


# ── 工具 ────────────────────────────────────────────────────────────────────


def _get_session(db: Session, session_id: int) -> ChatSession:
    try:
        return chat_engine.get_session(db, session_id)
    except chat_engine.ChatSessionNotFound:
        raise HTTPException(status_code=404, detail="会话不存在")


def _to_item(db: Session, session: ChatSession) -> ChatSessionItem:
    return _to_items(db, [session])[0]


def _to_items(db: Session, sessions: list[ChatSession]) -> list[ChatSessionItem]:
    """批量组装会话摘要：消息统计与最近消息、客户名各一次查询，避免 N+1。"""
    if not sessions:
        return []
    from sqlalchemy import func

    session_ids = [s.id for s in sessions]
    from services.ai.execution import recover_pending
    recover_pending(db, session_ids)
    from models import AIAssessmentRun
    running_sids = {sid for (sid,) in db.query(AIAssessmentRun.session_id).filter(
        AIAssessmentRun.session_id.in_(session_ids), AIAssessmentRun.status.in_(
            ("capturing", "facts_ready", "request_resolved", "planning", "validating", "repairing", "validated", "committing"))).distinct().all()}
    latest_times = db.query(AIAssessmentRun.session_id.label("sid"), func.max(AIAssessmentRun.created_at).label("created")).filter(
        AIAssessmentRun.session_id.in_(session_ids)).group_by(AIAssessmentRun.session_id).subquery()
    latest_runs = {r.session_id: r for r in db.query(AIAssessmentRun).join(latest_times,
        (AIAssessmentRun.session_id == latest_times.c.sid) & (AIAssessmentRun.created_at == latest_times.c.created)).all()}

    # 每个会话的消息数 + 最大消息 id（一次 GROUP BY 查询）
    stats_rows = (
        db.query(
            ChatMessage.session_id,
            func.count(ChatMessage.id),
            func.max(ChatMessage.id),
        )
        .filter(
            ChatMessage.session_id.in_(session_ids),
            ChatMessage.superseded_by_id.is_(None),
        )
        .group_by(ChatMessage.session_id)
        .all()
    )
    count_by_sid = {sid: cnt for sid, cnt, _ in stats_rows}
    last_msg_ids = [max_id for _, _, max_id in stats_rows if max_id is not None]

    # 最近一条消息内容（一次 IN 查询）
    last_content_by_mid: dict[int, str] = {}
    last_role_by_mid = {}
    if last_msg_ids:
        from services.ai.repository import resolve_messages
        rows = db.query(ChatMessage).filter(ChatMessage.id.in_(last_msg_ids)).all()
        for row in resolve_messages(db, rows, "preview"):
            last_content_by_mid[row["id"]] = row["content"]
            last_role_by_mid[row["id"]] = row["role"]
    last_content_by_sid = {
        sid: last_content_by_mid.get(max_id, "")
        for sid, _, max_id in stats_rows
        if max_id is not None
    }
    last_role_by_sid = {sid: last_role_by_mid.get(mid) for sid, _, mid in stats_rows}
    notices = {}
    for sid, run in latest_runs.items():
        if run.error_code in ("executor_lost", "timeout") and run.status in ("provider_error", "timeout"):
            notices[sid] = "上次生成已中断，已保留原答案。" if last_role_by_sid.get(sid) == "assistant" else "上次生成已中断，请重试。"

    # 客户名（一次 IN 查询）
    customer_ids = {s.customer_id for s in sessions if s.customer_id}
    name_by_cid: dict[int, str] = {}
    if customer_ids:
        name_by_cid = dict(
            db.query(Customer.id, Customer.customer_name)
            .filter(Customer.id.in_(customer_ids))
            .all()
        )

    return [
        ChatSessionItem(
            id=s.id,
            title=s.title,
            customer_id=s.customer_id,
            customer_name=name_by_cid.get(s.customer_id, "") if s.customer_id else "",
            scenario=s.scenario,
            streaming=chat_engine.is_session_streaming(s.id) or s.id in running_sids,
            run_notice=notices.get(s.id, ""),
            message_count=count_by_sid.get(s.id, 0),
            last_message=(last_content_by_sid.get(s.id, "") or "")[:60],
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in sessions
    ]


def _run(db: Session, session: ChatSession, payload: ChatRequest, scenario: str):
    """按 payload.stream 选择 SSE 流式或一次性 JSON。"""
    if scenario not in chat_engine.SCENARIOS:
        raise HTTPException(status_code=400, detail="仅支持综合评估和自由问答")
    if payload.idempotency_key:
        from models import AIAssessmentRun
        from services.ai.protocol import fingerprint
        from services.ai.guardrails import sanitize_input
        question = sanitize_input(payload.content)[0] or chat_engine._DEFAULT_QUICK_QUESTIONS.get(scenario, "")
        scope = fingerprint([session.id, None, scenario, payload.idempotency_key])
        run = db.query(AIAssessmentRun).filter_by(idempotency_key=scope).first()
        expected = fingerprint(dict(customer_id=payload.customer_id or session.customer_id, scenario=scenario,
                                    question=question, base_contract=None, supersede_message_id=None))
        if run and run.payload_hash != expected:
            raise HTTPException(status_code=409, detail="幂等键对应另一项请求")
    if payload.stream:
        generator = chat_engine.stream_sse(
            session.id,
            content=payload.content,
            scenario=scenario,
            customer_id=payload.customer_id,
            idempotency_key=payload.idempotency_key,
        )
        return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)

    result = chat_engine.complete_turn(
        db,
        session,
        content=payload.content,
        scenario=scenario,
        customer_id=payload.customer_id,
        idempotency_key=payload.idempotency_key,
    )
    return ChatTurnResponse(**result)


# ── 会话 ────────────────────────────────────────────────────────────────────


@router.post("/sessions", response_model=ChatSessionItem, status_code=201)
def create_session(payload: ChatSessionCreate, db: Session = Depends(get_db)):
    try:
        session = chat_engine.create_session(
            db,
            title=payload.title,
            customer_id=payload.customer_id,
            scenario=payload.scenario,
            system_prompt=payload.system_prompt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _to_item(db, session)


@router.get("/sessions", response_model=ChatSessionListResponse)
def list_sessions(
    customer_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    sessions = chat_engine.list_sessions(db, customer_id=customer_id, limit=limit)
    return ChatSessionListResponse(items=_to_items(db, sessions), total=len(sessions))


@router.get("/sessions/{session_id}", response_model=ChatSessionDetail)
def get_session(session_id: int, db: Session = Depends(get_db)):
    session = _get_session(db, session_id)
    base = _to_item(db, session)
    return ChatSessionDetail(
        **base.model_dump(),
        system_prompt=session.system_prompt,
        messages=_resolved_detail(db, session),
    )


def _resolved_detail(db, session):
    from services.ai.repository import resolve_messages
    rows = [m for m in session.messages if m.superseded_by_id is None]
    return [ChatMessageItem.model_validate(m) for m in resolve_messages(db, rows)]


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, db: Session = Depends(get_db)):
    _get_session(db, session_id)
    chat_engine.delete_session(db, session_id)


# ── 对话 ────────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/messages")
def send_message(session_id: int, payload: ChatRequest, db: Session = Depends(get_db)):
    """发送消息。默认返回 SSE 流；``stream=false`` 时返回 ChatTurnResponse。"""
    session = _get_session(db, session_id)
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    # 会话内自由提问默认按 free_qa 跟随用户问题（带客户上下文与历史），
    # 不继承会话创建时的场景，避免在评估会话里追问时反复输出整份评估报告。
    # 一键综合评估走 /evaluate。
    return _run(db, session, payload, payload.scenario or "free_qa")


@router.post("/sessions/{session_id}/evaluate")
def evaluate(session_id: int, payload: ChatRequest | None = None, db: Session = Depends(get_db)):
    """快捷评估：量化评分 → 写入评估历史 → AI 综合评估结论。"""
    session = _get_session(db, session_id)
    payload = payload or ChatRequest()
    if not (payload.customer_id or session.customer_id):
        raise HTTPException(status_code=400, detail="该会话未关联客户，无法生成评估")
    return _run(db, session, payload, "assessment")


@router.post("/sessions/{session_id}/regenerate")
def regenerate(session_id: int, payload: ChatRequest | None = None, db: Session = Depends(get_db)):
    """重新生成上一条 AI 回复，成功后替换原回复。"""
    session = _get_session(db, session_id)
    payload = payload or ChatRequest()
    from services.ai.chat import replay_regeneration
    identity = dict(customer_id=payload.customer_id or session.customer_id, scenario=payload.scenario, content=payload.content)
    replay = replay_regeneration(db, session, payload.idempotency_key, identity)
    if replay is not None:
        if payload.stream:
            return StreamingResponse(iter([chat_engine.sse(chat_engine.TurnEvent("done" if replay.get("message") else "error", replay))]),
                                     media_type="text/event-stream", headers=SSE_HEADERS)
        return ChatTurnResponse(**replay)
    scenario = payload.scenario or session.scenario or "free_qa"
    if scenario not in chat_engine.SCENARIOS:
        scenario = "assessment"

    if payload.stream:
        generator = chat_engine.stream_sse(
            session.id, scenario=scenario, customer_id=payload.customer_id, regenerate=True, idempotency_key=payload.idempotency_key,
            request_identity=identity,
        )
        return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)

    content, exclude_id = chat_engine.prepare_regenerate(db, session, scenario=scenario)
    if not content:
        raise HTTPException(status_code=400, detail="没有可重新生成的消息")
    result = chat_engine.complete_turn(
        db,
        session,
        content=content,
        scenario=scenario,
        customer_id=payload.customer_id,
        persist_user=False,          # 提问已在库里，重跑不再重复落库
        exclude_message_id=exclude_id,
        supersede_message_id=exclude_id,
        idempotency_key=payload.idempotency_key,
        request_identity=identity,
    )
    return ChatTurnResponse(**result)


# ── 反馈与状态 ──────────────────────────────────────────────────────────────


@router.get("/metrics")
def chat_metrics_distribution(
    hours: int = Query(default=168, ge=1, le=2160),
    db: Session = Depends(get_db),
):
    """查看最近 1 小时至 90 天的 token、耗时、降级和资源边界分布。"""
    return chat_metrics.summarize(db, hours=hours)


@router.get("/sessions/{session_id}/runs/{run_id}")
def get_ai_run(session_id: int, run_id: str, db: Session = Depends(get_db)):
    from models import AIAssessmentRun
    from services.ai.repository import available_result
    session = _get_session(db, session_id)
    from services.ai.execution import recover_pending
    recover_pending(db, [session_id])
    run = db.get(AIAssessmentRun, run_id)
    if not run or run.session_id != session_id or run.customer_id != session.customer_id:
        raise HTTPException(status_code=404, detail="运行不存在")
    result = available_result(db, run, customer_id=session.customer_id, session_id=session_id)
    return dict(run_id=run_id, status=run.status, error_code=run.error_code, result=result)


@router.post("/sessions/{session_id}/runs/{run_id}/cancel")
def cancel_ai_run(session_id: int, run_id: str, db: Session = Depends(get_db)):
    from services.ai.chat import cancel_run
    _get_session(db, session_id)
    try:
        run = cancel_run(db, session_id, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="运行不存在")
    return dict(run_id=run_id, status=run.status)


@router.post("/messages/{message_id}/feedback", response_model=MessageFeedbackResponse)
def message_feedback(
    message_id: int, payload: MessageFeedbackRequest, db: Session = Depends(get_db)
):
    """点赞/点踩，回流策略质量与采纳率。"""
    if payload.feedback not in MESSAGE_FEEDBACKS:
        raise HTTPException(status_code=400, detail="feedback 仅支持 up / down / 空")
    try:
        message = chat_engine.set_feedback(db, message_id, payload.feedback)
    except LookupError:
        raise HTTPException(status_code=404, detail="消息不存在")
    return MessageFeedbackResponse(id=message.id, feedback=message.feedback)


@router.get("/status", response_model=LLMStatusResponse)
def llm_status():
    """LLM 可用性探测：前端据此显示"AI 就绪"或"已降级为规则引擎"。"""
    status = llm_adapter.llm_status()
    try:
        assets = load_runtime_assets()
        status["prompt_version"] = assets.manifest["prompts_version"]
        status["scenarios"] = list(chat_engine.SCENARIOS)
    except ProtocolError as exc:
        status["reason"] = (status.get("reason") or "") + f"（Prompt 模板异常：{exc}）"
    return LLMStatusResponse(**status)
