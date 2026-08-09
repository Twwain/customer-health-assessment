"""AI 对话接口。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST   | /api/chat/sessions                       | 创建会话 |
| GET    | /api/chat/sessions                       | 会话列表 |
| GET    | /api/chat/sessions/{id}                  | 会话详情（含消息） |
| DELETE | /api/chat/sessions/{id}                  | 删除会话 |
| POST   | /api/chat/sessions/{id}/messages         | 发送消息（默认 SSE 流式） |
| POST   | /api/chat/sessions/{id}/evaluate         | 快捷评估 |
| POST   | /api/chat/sessions/{id}/strategy         | 快捷策略生成 |
| POST   | /api/chat/sessions/{id}/alert-analysis   | 预警 AI 解读 |
| POST   | /api/chat/sessions/{id}/regenerate       | 重新生成上一条回复（原型「🔄 重新生成」） |
| POST   | /api/chat/messages/{id}/feedback         | 消息点赞/点踩 |
| GET    | /api/chat/status                         | LLM 可用性（降级提示条数据源） |

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
from services.ai import chat_engine, llm_adapter
from services.ai.prompt_templates import PromptTemplateError, load_prompt_templates

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

    # 每个会话的消息数 + 最大消息 id（一次 GROUP BY 查询）
    stats_rows = (
        db.query(
            ChatMessage.session_id,
            func.count(ChatMessage.id),
            func.max(ChatMessage.id),
        )
        .filter(ChatMessage.session_id.in_(session_ids))
        .group_by(ChatMessage.session_id)
        .all()
    )
    count_by_sid = {sid: cnt for sid, cnt, _ in stats_rows}
    last_msg_ids = [max_id for _, _, max_id in stats_rows if max_id is not None]

    # 最近一条消息内容（一次 IN 查询）
    last_content_by_mid: dict[int, str] = {}
    if last_msg_ids:
        for mid, content in (
            db.query(ChatMessage.id, ChatMessage.content)
            .filter(ChatMessage.id.in_(last_msg_ids))
            .all()
        ):
            last_content_by_mid[mid] = content or ""
    last_content_by_sid = {
        sid: last_content_by_mid.get(max_id, "")
        for sid, _, max_id in stats_rows
        if max_id is not None
    }

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
            streaming=chat_engine.is_session_streaming(s.id),
            message_count=count_by_sid.get(s.id, 0),
            last_message=(last_content_by_sid.get(s.id, "") or "")[:60],
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in sessions
    ]


def _run(db: Session, session: ChatSession, payload: ChatRequest, scenario: str):
    """按 payload.stream 选择 SSE 流式或一次性 JSON。"""
    if payload.stream:
        generator = chat_engine.stream_sse(
            session.id,
            content=payload.content,
            scenario=scenario,
            customer_id=payload.customer_id,
        )
        return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)

    result = chat_engine.complete_turn(
        db,
        session,
        content=payload.content,
        scenario=scenario,
        customer_id=payload.customer_id,
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
        messages=[ChatMessageItem.model_validate(m) for m in session.messages],
    )


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
    # 一键评估/策略/预警仍走 /evaluate、/strategy、/alert-analysis 场景端点。
    return _run(db, session, payload, payload.scenario or "free_qa")


@router.post("/sessions/{session_id}/evaluate")
def evaluate(session_id: int, payload: ChatRequest | None = None, db: Session = Depends(get_db)):
    """快捷评估：量化评分 → 写入评估历史 → AI 综合评估结论。"""
    session = _get_session(db, session_id)
    payload = payload or ChatRequest()
    if not (payload.customer_id or session.customer_id):
        raise HTTPException(status_code=400, detail="该会话未关联客户，无法生成评估")
    return _run(db, session, payload, "assessment")


@router.post("/sessions/{session_id}/strategy")
def strategy(session_id: int, payload: ChatRequest | None = None, db: Session = Depends(get_db)):
    """快捷策略生成：输出推荐 / 备选 / 长期三层策略（含结构化条目）。"""
    session = _get_session(db, session_id)
    payload = payload or ChatRequest()
    if not (payload.customer_id or session.customer_id):
        raise HTTPException(status_code=400, detail="该会话未关联客户，无法生成策略")
    return _run(db, session, payload, "strategy")


@router.post("/sessions/{session_id}/alert-analysis")
def alert_analysis(session_id: int, payload: ChatRequest | None = None, db: Session = Depends(get_db)):
    """预警 AI 解读：预警成因 + 趋势判断 + 止损动作。"""
    session = _get_session(db, session_id)
    payload = payload or ChatRequest()
    if not (payload.customer_id or session.customer_id):
        raise HTTPException(status_code=400, detail="该会话未关联客户，无法解读预警")
    return _run(db, session, payload, "alert_analysis")


@router.post("/sessions/{session_id}/regenerate")
def regenerate(session_id: int, payload: ChatRequest | None = None, db: Session = Depends(get_db)):
    """重新生成上一条 AI 回复（删除旧回复后按同一提问重跑）。"""
    session = _get_session(db, session_id)
    payload = payload or ChatRequest()
    scenario = payload.scenario or session.scenario or "free_qa"

    if payload.stream:
        generator = chat_engine.stream_sse(
            session.id, scenario=scenario, customer_id=payload.customer_id, regenerate=True
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
    )
    return ChatTurnResponse(**result)


# ── 反馈与状态 ──────────────────────────────────────────────────────────────


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
        templates = load_prompt_templates()
        status["prompt_version"] = templates.version
        status["scenarios"] = templates.scenarios
    except PromptTemplateError as exc:
        status["reason"] = (status.get("reason") or "") + f"（Prompt 模板异常：{exc}）"
    return LLMStatusResponse(**status)
