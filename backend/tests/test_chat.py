"""AI 对话引擎测试（SOW §3.2 M2 / §6.1 / §7 降级）。

覆盖：Prompt 模板加载与热更新、安全护栏、上下文注入、策略结构化解析、
多轮上下文窗口、LLM 降级兜底、SSE 事件协议、会话与反馈接口。
"""

import datetime
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from database import Base, get_db
from models import AssessmentHistory, ChatMessage, ChatSession, Customer
from services import assessment_history
from services.ai import chat_engine, context_builder, guardrails, llm_adapter, strategy
from services.ai.llm_adapter import LLMError, LLMUnavailableError
from services.ai.prompt_templates import (
    PromptTemplateError,
    clear_prompt_cache,
    load_prompt_templates,
    parse_prompt_templates,
)
from services.ai import tools
from services.ai.strategy import generate
from services.rag.retriever import RetrievedChunk
from services.rag.vector_store import InMemoryVectorStore


# ══════════════════════════ 夹具 ════════════════════════════════════════════


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def customer(db):
    c = Customer(
        customer_name="示例汽车集团",
        industry="制造",
        contact_person="王工",
        contact_phone="13812345678",
        cooperation_years=1.2,
        contact_frequency="不定期",
        last_contact_date=datetime.date.today() - datetime.timedelta(days=200),
        customer_satisfaction=3,
        contract_amount=50,
        payment_status="严重逾期",
        risk_signals="长期未联系；友商已介入",
        competitor_involvement=True,
        growth_potential="低",
        custom_fields={},
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


class FakeAdapter:
    """可控的假 LLM：按预设分片吐字，或抛出指定异常。"""

    def __init__(self, chunks=None, usage=None, error=None, available=True, model="fake-chat"):
        self.chunks = chunks if chunks is not None else ["你好", "，", "这是测试回复。"]
        self.usage = usage or {"total_tokens": 128}
        self.error = error
        self.available = available
        self.model = model
        self.calls: list[list] = []

    def status(self):
        return {
            "provider": "fake",
            "model": self.model,
            "base_url": "http://fake",
            "available": self.available,
            "reason": "" if self.available else "测试用不可用适配器",
        }

    def stream_chat_completion(self, messages, *, temperature=None, max_tokens=None, extra=None, on_usage=None):
        self.calls.append(list(messages))
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk
        if on_usage:
            on_usage(self.usage)


@pytest.fixture(autouse=True)
def _reset_adapters():
    llm_adapter.reset_adapters()
    yield
    llm_adapter.reset_adapters()


@pytest.fixture()
def fake_llm():
    adapter = FakeAdapter()
    llm_adapter.set_chat_adapter(adapter)
    return adapter


@pytest.fixture()
def offline_llm():
    adapter = FakeAdapter(available=False, error=LLMUnavailableError("未配置 API Key"))
    llm_adapter.set_chat_adapter(adapter)
    return adapter


# ══════════════════════════ Prompt 模板 ═════════════════════════════════════


def test_prompt_templates_cover_all_scenarios():
    templates = load_prompt_templates()
    assert templates.version
    for scenario in ("free_qa", "assessment", "strategy", "alert_analysis"):
        assert scenario in templates.templates


def test_guardrails_are_injected_into_system_prompt():
    template = load_prompt_templates().get("assessment")
    assert "输出护栏" in template.system
    assert "{{guardrails}}" not in template.system


def test_render_replaces_placeholders_and_drops_unknown():
    template = load_prompt_templates().get("free_qa")
    text = template.render_user(customer_context="【客户上下文】", question="这个客户怎么样？")
    assert "【客户上下文】" in text
    assert "这个客户怎么样？" in text
    assert "{{" not in text


def test_unknown_scenario_falls_back_to_free_qa():
    templates = load_prompt_templates()
    assert templates.get("not_exists").key == "free_qa"


def test_strategy_template_declares_json_schema():
    template = load_prompt_templates().get("strategy")
    assert "expected_outcome" in template.system
    assert "long_term" in template.system


def test_invalid_template_raises():
    with pytest.raises(PromptTemplateError):
        parse_prompt_templates({"templates": {"x": {"user": "只有 user"}}})
    with pytest.raises(PromptTemplateError):
        parse_prompt_templates({"templates": {}})


def test_prompt_templates_hot_reload(tmp_path):
    path = tmp_path / "prompt_templates.yaml"
    path.write_text(
        'version: "t1"\ntemplates:\n  free_qa:\n    system: "v1"\n    user: "{{question}}"\n',
        encoding="utf-8",
    )
    clear_prompt_cache()
    assert load_prompt_templates(str(path)).version == "t1"

    import os
    import time

    time.sleep(0.01)
    path.write_text(
        'version: "t2"\ntemplates:\n  free_qa:\n    system: "v2"\n    user: "{{question}}"\n',
        encoding="utf-8",
    )
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 1))
    assert load_prompt_templates(str(path)).version == "t2"
    clear_prompt_cache()


# ══════════════════════════ 安全护栏 ════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected_hit",
    [
        ("数据库密码: Abc123456", "credential"),
        ("这是我的 key sk-abcdef1234567890", "api_key"),
        ("身份证 11010119900307721X", "id_card"),
        ("卡号 6222020200112345678", "bank_card"),
        ("联系电话 13812345678", "phone"),
    ],
)
def test_sanitize_masks_sensitive_values(raw, expected_hit):
    cleaned, hits = guardrails.sanitize_input(raw)
    assert expected_hit in hits
    for token in ("Abc123456", "sk-abcdef1234567890", "11010119900307721X", "6222020200112345678"):
        assert token not in cleaned
    assert "13812345678" not in cleaned


def test_sanitize_input_truncates_long_text():
    cleaned, hits = guardrails.sanitize_input("很长" * 5000)
    assert "truncated" in hits
    assert len(cleaned) <= config.CHAT_MAX_INPUT_CHARS + 20


def test_sanitize_keeps_normal_business_text():
    text = "示例汽车集团合同金额 50 万元，满意度 3 分"
    cleaned, hits = guardrails.sanitize_input(text)
    assert cleaned == text
    assert hits == []


# ══════════════════════════ 上下文注入 ══════════════════════════════════════


def test_context_contains_quantitative_facts(db, customer):
    ctx = context_builder.build_context(db, customer)
    assert ctx.assessment is not None
    assert "示例汽车集团" in ctx.customer_text
    assert "18.5" in ctx.customer_text          # 基础客情分
    assert "关系紧密度" in ctx.customer_text
    assert "竞品已介入" in ctx.customer_text
    assert "未检索到相关知识" in ctx.knowledge_text


def test_context_excludes_personal_phone(db, customer):
    ctx = context_builder.build_context(db, customer)
    assert customer.contact_phone not in ctx.customer_text


def test_context_without_customer_is_generic(db):
    ctx = context_builder.build_context(db, None)
    assert ctx.assessment is None
    assert "未关联具体客户" in ctx.customer_text


def test_alert_context_lists_alerts(db, customer):
    ctx = context_builder.build_context(db, customer)
    assert "待解读的预警" in ctx.alert_text
    assert "竞品" in ctx.alert_text


# ══════════════════════════ 策略解析与降级 ══════════════════════════════════


def test_split_strategy_payload_extracts_items():
    text = (
        "### ✅ 推荐策略\n1. **高层拜访**\n\n"
        '```json\n{"strategies": [{"priority": "recommended", "title": "高层拜访", '
        '"urgency": "high", "reason": "竞品介入", "action": "VP 级拜访", '
        '"expected_outcome": "稳固关系", "reference": "案例集 4-2"}]}\n```'
    )
    body, items = strategy.split_strategy_payload(text)
    assert "```json" not in body
    assert "高层拜访" in body
    assert items[0]["priority"] == "recommended"
    assert items[0]["urgency"] == "high"
    assert items[0]["reference"] == "案例集 4-2"


def test_split_strategy_payload_normalizes_chinese_aliases():
    text = '```json\n{"strategies": [{"priority": "长期", "title": "共建实验室", "urgency": "低"}]}\n```'
    _, items = strategy.split_strategy_payload(text)
    assert items[0]["priority"] == "long_term"
    assert items[0]["urgency"] == "low"


def test_split_strategy_payload_tolerates_broken_json():
    text = "正常回答内容\n```json\n{不是合法 json}\n```"
    body, items = strategy.split_strategy_payload(text)
    assert items == []
    assert "正常回答内容" in body


def test_strategy_items_sorted_by_priority():
    text = (
        '```json\n{"strategies": ['
        '{"priority": "long_term", "title": "C"},'
        '{"priority": "recommended", "title": "A"},'
        '{"priority": "alternative", "title": "B"}]}\n```'
    )
    _, items = strategy.split_strategy_payload(text)
    assert [i["title"] for i in items] == ["A", "B", "C"]


def test_degraded_strategies_from_rule_engine(db, customer):
    from services.scoring import get_scoring_strategy

    assessment = get_scoring_strategy().evaluate(customer)
    items = strategy.build_degraded_strategies(assessment)
    assert items
    assert any(i["priority"] == "recommended" for i in items)
    assert all(i["reference"].startswith("规则引擎") for i in items)

    markdown = strategy.render_strategies_markdown(items)
    assert "✅ 推荐策略" in markdown
    assert "原因" in markdown


def test_degraded_strategies_for_healthy_customer(db):
    from services.scoring import get_scoring_strategy

    healthy = Customer(
        customer_name="某省政务云",
        cooperation_years=6,
        contact_frequency="每周",
        last_contact_date=datetime.date.today(),
        customer_satisfaction=9,
        contract_amount=800,
        payment_status="正常",
        growth_potential="高",
        custom_fields={},
    )
    db.add(healthy)
    db.commit()

    items = strategy.build_degraded_strategies(get_scoring_strategy().evaluate(healthy))
    assert items
    assert all(i["priority"] == "long_term" for i in items)


# ══════════════════════════ 会话与一轮对话 ══════════════════════════════════


def test_create_session_titles_from_customer(db, customer):
    session = chat_engine.create_session(db, customer_id=customer.id, scenario="assessment")
    assert session.title == "示例汽车集团 · AI 评估"
    assert session.customer_id == customer.id


def test_create_session_rejects_unknown_customer(db):
    with pytest.raises(ValueError):
        chat_engine.create_session(db, customer_id=9999)


def test_complete_turn_persists_messages(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    result = chat_engine.complete_turn(db, session, content="这个客户风险大吗？")

    assert result["degraded"] is False
    assert result["tokens_used"] == 128
    assert result["message"]["content"] == "你好，这是测试回复。"
    assert result["assessment"]["total_score"] == 18.5

    messages = db.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "这个客户风险大吗？"


def test_turn_injects_quantitative_context_into_prompt(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    chat_engine.complete_turn(db, session, content="怎么办？")

    sent = fake_llm.calls[-1]
    assert sent[0].role == "system"
    assert "客情评估智能体" in sent[0].content
    assert "18.5" in sent[-1].content            # 基础客情分进了本轮 user prompt
    assert "未检索到相关知识" in sent[-1].content  # 防幻觉提示


def test_multi_turn_keeps_history_window(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    chat_engine.complete_turn(db, session, content="第一问")
    chat_engine.complete_turn(db, session, content="第二问")

    roles = [m.role for m in fake_llm.calls[-1]]
    assert roles == ["system", "user", "assistant", "user"]
    assert fake_llm.calls[-1][1].content == "第一问"


def test_context_window_respects_message_limit(db, customer, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "CHAT_MAX_CONTEXT_MESSAGES", 2)
    session = chat_engine.create_session(db, customer_id=customer.id)
    for i in range(3):
        chat_engine.complete_turn(db, session, content=f"第{i}问")

    history = [m for m in fake_llm.calls[-1] if m.role != "system"]
    assert len(history) <= 3  # 2 条历史 + 本轮


def test_user_input_is_sanitized_before_persist(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    chat_engine.complete_turn(db, session, content="登录密码: SuperSecret123")

    stored = db.query(ChatMessage).filter(ChatMessage.role == "user").one()
    assert "SuperSecret123" not in stored.content
    assert "***" in stored.content


def test_assessment_scenario_records_history(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id, scenario="assessment")
    chat_engine.complete_turn(db, session, content="", scenario="assessment")

    record = db.query(AssessmentHistory).one()
    assert record.trigger == "ai_assessment"
    assert record.assessed_by == "ai"
    assert record.total_score == 18.5


def test_strategy_scenario_parses_and_snapshots_items(db, customer):
    payload = (
        "### ✅ 推荐策略\n1. **启动分级评审**\n\n"
        '```json\n{"strategies": [{"priority": "recommended", "title": "启动分级评审", '
        '"urgency": "high", "reason": "健康分低于 25", "action": "15 个工作日内召集评审", '
        '"expected_outcome": "完成挽留/退出决策"}]}\n```'
    )
    llm_adapter.set_chat_adapter(FakeAdapter(chunks=[payload]))
    assessment_history.record_assessment(db, customer)

    session = chat_engine.create_session(db, customer_id=customer.id, scenario="strategy")
    result = chat_engine.complete_turn(db, session, content="", scenario="strategy")

    assert result["strategy_items"][0]["title"] == "启动分级评审"
    assert "```json" not in result["message"]["content"]

    record = db.query(AssessmentHistory).order_by(AssessmentHistory.id.desc()).first()
    assert record.strategy_snapshot[0]["title"] == "启动分级评审"


# ══════════════════════════ 降级（SOW §7）═══════════════════════════════════


def test_degrades_to_rule_engine_when_llm_unavailable(db, customer, offline_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    result = chat_engine.complete_turn(db, session, content="帮我分析一下", scenario="assessment")

    assert result["degraded"] is True
    content = result["message"]["content"]
    assert "规则引擎兜底结果" in content
    assert "18.5" in content
    assert "关系紧密度" in content
    assert result["warnings"]

    stored = db.query(ChatMessage).filter(ChatMessage.role == "assistant").one()
    assert stored.degraded is True


def test_degraded_strategy_scenario_still_returns_items(db, customer, offline_llm):
    session = chat_engine.create_session(db, customer_id=customer.id, scenario="strategy")
    result = chat_engine.complete_turn(db, session, content="", scenario="strategy")

    assert result["degraded"] is True
    assert result["strategy_items"]
    assert result["strategy_items"][0]["priority"] == "recommended"


def test_degraded_alert_analysis_uses_trend(db, customer, offline_llm):
    assessment_history.record_assessment(db, customer)
    session = chat_engine.create_session(db, customer_id=customer.id, scenario="alert_analysis")
    result = chat_engine.complete_turn(db, session, content="", scenario="alert_analysis")

    content = result["message"]["content"]
    assert "预警解读" in content
    assert "趋势判断" in content


def test_mid_stream_error_keeps_partial_content(db, customer):
    class BreakingAdapter(FakeAdapter):
        def stream_chat_completion(self, messages, **kwargs):
            self.calls.append(list(messages))
            yield "前半段回答"
            raise LLMError("连接被重置")

    llm_adapter.set_chat_adapter(BreakingAdapter())
    session = chat_engine.create_session(db, customer_id=customer.id)
    result = chat_engine.complete_turn(db, session, content="继续")

    assert "前半段回答" in result["message"]["content"]
    assert "生成中断" in result["message"]["content"]
    assert result["degraded"] is False


def test_unexpected_exception_falls_back(db, customer):
    llm_adapter.set_chat_adapter(FakeAdapter(error=RuntimeError("boom")))
    session = chat_engine.create_session(db, customer_id=customer.id)
    result = chat_engine.complete_turn(db, session, content="出错了会怎样")

    assert result["degraded"] is True
    assert "规则引擎兜底结果" in result["message"]["content"]


# ══════════════════════════ 重新生成与反馈 ══════════════════════════════════


def test_regenerate_replaces_last_answer(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    chat_engine.complete_turn(db, session, content="第一问")

    content, exclude_id = chat_engine.prepare_regenerate(db, session)
    assert content == "第一问"
    assert db.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 0

    fake_llm.chunks = ["换个说法的回答"]
    chat_engine.complete_turn(
        db, session, content=content, persist_user=False, exclude_message_id=exclude_id
    )
    assistants = db.query(ChatMessage).filter(ChatMessage.role == "assistant").all()
    assert len(assistants) == 1
    assert assistants[0].content == "换个说法的回答"
    assert db.query(ChatMessage).filter(ChatMessage.role == "user").count() == 1


def test_regenerate_keeps_history_when_last_turn_failed(db, customer, fake_llm):
    """上一轮 assistant 写入前失败时，regenerate 不得误删更早轮次的正常回复。"""
    session = chat_engine.create_session(db, customer_id=customer.id)
    chat_engine.complete_turn(db, session, content="第一问")
    # 模拟第二轮在持久化 assistant 前失败：只剩 user 消息
    orphan = ChatMessage(session_id=session.id, role="user", content="第二问（未应答）")
    db.add(orphan)
    db.commit()
    assistants_before = db.query(ChatMessage).filter(ChatMessage.role == "assistant").count()

    content, _ = chat_engine.prepare_regenerate(db, session)

    assert content == "第二问（未应答）"
    # 第一问的 assistant 回复必须保留，不能被当成"待重生成"的消息删掉
    assert db.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == assistants_before


def test_set_feedback(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    result = chat_engine.complete_turn(db, session, content="问题")
    message = chat_engine.set_feedback(db, result["message"]["id"], "up")
    assert message.feedback == "up"


# ══════════════════════════ 接口层（SOW §6.1）═══════════════════════════════


@pytest.fixture()
def client(session_factory, monkeypatch):
    from routers import chat as chat_router

    monkeypatch.setattr(chat_engine, "SessionLocal", session_factory)

    app = FastAPI()
    app.include_router(chat_router.router, prefix="/api")

    def _override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        name, payload = "", "{}"
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
        if name:
            events.append((name, json.loads(payload)))
    return events


def test_session_crud_endpoints(client, customer, fake_llm):
    created = client.post("/api/chat/sessions", json={"customer_id": customer.id})
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert created.json()["customer_name"] == "示例汽车集团"

    listed = client.get("/api/chat/sessions")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    detail = client.get(f"/api/chat/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["messages"] == []

    assert client.delete(f"/api/chat/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/chat/sessions/{session_id}").status_code == 404


def test_send_message_streams_sse(client, customer, fake_llm):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    response = client.post(
        f"/api/chat/sessions/{session_id}/messages", json={"content": "这个客户怎么样？"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "start"
    assert "context" in names
    assert "delta" in names
    assert names[-1] == "done"

    text = "".join(data["text"] for name, data in events if name == "delta")
    assert text == "你好，这是测试回复。"

    context = next(data for name, data in events if name == "context")
    assert context["assessment"]["total_score"] == 18.5
    assert context["trend"]["level"] == "风险"

    done = events[-1][1]
    assert done["degraded"] is False
    assert done["message"]["role"] == "assistant"


def test_send_message_non_stream_returns_json(client, customer, fake_llm):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    response = client.post(
        f"/api/chat/sessions/{session_id}/messages",
        json={"content": "非流式", "stream": False},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["message"]["content"] == "你好，这是测试回复。"
    assert body["assessment"]["level"] == "风险"


def test_send_empty_message_rejected(client, customer, fake_llm):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    assert client.post(f"/api/chat/sessions/{session_id}/messages", json={"content": " "}).status_code == 400


def test_evaluate_requires_customer(client, fake_llm):
    session_id = client.post("/api/chat/sessions", json={}).json()["id"]
    response = client.post(f"/api/chat/sessions/{session_id}/evaluate", json={"stream": False})
    assert response.status_code == 400


def test_strategy_endpoint_returns_items(client, customer):
    payload = (
        '```json\n{"strategies": [{"priority": "recommended", "title": "启动分级评审", '
        '"urgency": "high"}]}\n```'
    )
    llm_adapter.set_chat_adapter(FakeAdapter(chunks=["策略如下：\n\n", payload]))
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]

    response = client.post(f"/api/chat/sessions/{session_id}/strategy", json={"stream": False})
    body = response.json()
    assert body["strategy_items"][0]["title"] == "启动分级评审"


def test_alert_analysis_endpoint_streams(client, customer, fake_llm):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    response = client.post(f"/api/chat/sessions/{session_id}/alert-analysis", json={})
    names = [name for name, _ in _parse_sse(response.text)]
    assert "done" in names


def test_regenerate_endpoint(client, customer, fake_llm):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    client.post(
        f"/api/chat/sessions/{session_id}/messages", json={"content": "第一问", "stream": False}
    )
    fake_llm.chunks = ["重新生成的回答"]

    response = client.post(f"/api/chat/sessions/{session_id}/regenerate", json={"stream": False})
    assert response.json()["message"]["content"] == "重新生成的回答"

    detail = client.get(f"/api/chat/sessions/{session_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_streaming_on_unknown_session_emits_error(client, fake_llm):
    response = client.post("/api/chat/sessions/999/messages", json={"content": "hi"})
    assert response.status_code == 404


def test_message_feedback_endpoint(client, customer, fake_llm):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    body = client.post(
        f"/api/chat/sessions/{session_id}/messages", json={"content": "问题", "stream": False}
    ).json()
    message_id = body["message"]["id"]

    ok = client.post(f"/api/chat/messages/{message_id}/feedback", json={"feedback": "down"})
    assert ok.json() == {"id": message_id, "feedback": "down"}

    bad = client.post(f"/api/chat/messages/{message_id}/feedback", json={"feedback": "hate"})
    assert bad.status_code == 400
    assert client.post("/api/chat/messages/9999/feedback", json={"feedback": "up"}).status_code == 404


def test_status_endpoint_reports_availability(client, fake_llm):
    body = client.get("/api/chat/status").json()
    assert body["available"] is True
    assert body["model"] == "fake-chat"
    assert "assessment" in body["scenarios"]
    assert body["prompt_version"]


def test_status_endpoint_reports_degraded(client, offline_llm):
    body = client.get("/api/chat/status").json()
    assert body["available"] is False
    assert body["degraded"] is True
    assert body["reason"]


# ══════════════════════════ 适配层 ══════════════════════════════════════════


def test_adapter_unavailable_without_api_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    llm_adapter.reset_adapters()
    adapter = llm_adapter.get_chat_adapter()
    assert adapter.available is False
    assert "API Key" in adapter.unavailable_reason()
    with pytest.raises(LLMUnavailableError):
        adapter.chat_completion([llm_adapter.LLMMessage(role="user", content="hi")])


def test_adapter_disabled_by_switch(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    llm_adapter.reset_adapters()
    assert llm_adapter.get_chat_adapter().available is False
    assert llm_adapter.llm_status()["degraded"] is True


def test_estimate_tokens_counts_cjk():
    assert llm_adapter.estimate_tokens("中文四个字") == 5
    assert llm_adapter.estimate_tokens("abcd") == 1


def test_as_messages_accepts_mixed_input():
    messages = llm_adapter.as_messages(
        [("system", "s"), {"role": "user", "content": "u"}, llm_adapter.LLMMessage("assistant", "a")]
    )
    assert [m.role for m in messages] == ["system", "user", "assistant"]


# ── OpenAI 兼容协议解析（用假 httpx 驱动真实适配器）─────────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, lines=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self._lines = lines or []
        self.text = text or json.dumps(self._json, ensure_ascii=False)

    def json(self):
        return self._json

    def read(self):
        return self.text.encode("utf-8")

    def iter_lines(self):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeHttpx:
    """够用即可的 httpx 替身：记录请求并按脚本返回响应。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []
        outer = self

        class _Client:
            def __init__(self, timeout=None):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, json=None):
                outer.requests.append({"url": url, "json": json})
                return outer._responses.pop(0)

            def stream(self, method, url, headers=None, json=None):
                outer.requests.append({"url": url, "json": json})
                return outer._responses.pop(0)

        self.Client = _Client

    @staticmethod
    def Timeout(*args, **kwargs):
        return None


def _adapter(monkeypatch, responses, **kwargs):
    fake = _FakeHttpx(responses)
    monkeypatch.setattr(llm_adapter, "httpx", fake)
    monkeypatch.setattr(llm_adapter.time, "sleep", lambda *_: None)
    adapter = llm_adapter.OpenAICompatibleAdapter(
        name="deepseek",
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="deepseek-v4-flash",
        max_retries=kwargs.pop("max_retries", 1),
        retry_backoff=0,
        **kwargs,
    )
    return adapter, fake


def test_chat_completion_parses_openai_payload(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(
                json_data={
                    "model": "deepseek-v4-flash",
                    "choices": [{"message": {"content": "评估结论"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 321},
                }
            )
        ],
    )
    result = adapter.chat_completion([llm_adapter.LLMMessage("user", "hi")])
    assert result.content == "评估结论"
    assert result.tokens_used == 321
    assert fake.requests[0]["url"] == "https://api.test/v1/chat/completions"
    assert fake.requests[0]["json"]["stream"] is False


def test_chat_completion_retries_on_server_error(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(status_code=500, text="upstream boom"),
            _FakeResponse(json_data={"choices": [{"message": {"content": "重试成功"}}]}),
        ],
    )
    assert adapter.chat_completion([{"role": "user", "content": "hi"}]).content == "重试成功"
    assert len(fake.requests) == 2


def test_chat_completion_does_not_retry_on_auth_error(monkeypatch):
    adapter, fake = _adapter(monkeypatch, [_FakeResponse(status_code=401, text="invalid key")])
    with pytest.raises(LLMUnavailableError):
        adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert len(fake.requests) == 1


def test_stream_parses_sse_and_usage(monkeypatch):
    lines = [
        'data: {"choices": [{"delta": {"content": "客户"}}]}',
        "",
        'data: {"choices": [{"delta": {"content": "健康度"}}]}',
        'data: {"choices": [], "usage": {"total_tokens": 42}}',
        "data: [DONE]",
    ]
    adapter, _ = _adapter(monkeypatch, [_FakeResponse(lines=lines)])

    usage = {}
    chunks = list(
        adapter.stream_chat_completion([{"role": "user", "content": "hi"}], on_usage=usage.update)
    )
    assert "".join(chunks) == "客户健康度"
    assert usage["total_tokens"] == 42


def test_stream_disables_stream_options_when_gateway_rejects(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(status_code=400, text="unknown field stream_options"),
            _FakeResponse(lines=['data: {"choices": [{"delta": {"content": "ok"}}]}']),
        ],
    )
    assert "".join(adapter.stream_chat_completion([{"role": "user", "content": "hi"}])) == "ok"
    assert "stream_options" in fake.requests[0]["json"]
    assert "stream_options" not in fake.requests[1]["json"]


def test_stream_failure_raises_unavailable_for_degrade(monkeypatch):
    adapter, _ = _adapter(
        monkeypatch,
        [_FakeResponse(status_code=503, text="overloaded"), _FakeResponse(status_code=503, text="overloaded")],
    )
    with pytest.raises(LLMUnavailableError):
        list(adapter.stream_chat_completion([{"role": "user", "content": "hi"}]))


def test_embedding_returns_vectors_in_index_order(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(
                json_data={
                    "data": [
                        {"index": 1, "embedding": [0.3, 0.4]},
                        {"index": 0, "embedding": [0.1, 0.2]},
                    ]
                }
            )
        ],
    )
    vectors = adapter.embed(["切片A", "切片B"], dimensions=2)
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert fake.requests[0]["url"].endswith("/embeddings")
    assert fake.requests[0]["json"]["dimensions"] == 2


# ══════════════════════════ M4 Agent Loop（SOW §3.4 / §4.2）═══════════════════


def _agent_chunk(document_id=1, item_id=1, title="客户健康度评估方法", category="methodology"):
    return RetrievedChunk(
        document_id=document_id,
        chunk_index=0,
        item_id=item_id,
        item_title=title,
        category=category,
        content="关系紧密度衡量客户与团队的日常互动频率与质量。",
        score=0.91,
        metadata={},
    )


def test_agent_loop_retrieves_then_traces_references(db, customer, fake_llm, monkeypatch):
    # 模拟 Agent 自带 RAG 检索命中（不依赖真实 embedding / chroma）
    monkeypatch.setattr(tools, "rag_retrieve", lambda *a, **k: [_agent_chunk()])

    # 草稿同时含 ```json 策略块 + 引用标记 → 通过自批判，不再精炼
    payload = (
        "根据知识库：\n📎 参考《客户健康度评估方法》\n\n"
        "### ✅ 推荐策略\n1. **高层拜访**\n\n"
        '```json\n{"strategies": [{"priority": "recommended", "title": "高层拜访", '
        '"urgency": "high", "reason": "竞品介入", "action": "VP 级拜访", '
        '"expected_outcome": "稳固关系"}]}\n```'
    )
    llm_adapter.set_chat_adapter(FakeAdapter(chunks=[payload]))

    session = chat_engine.create_session(db, customer_id=customer.id, scenario="strategy")
    events = list(chat_engine.run_turn(db, session, content="", scenario="strategy"))

    names = [e.type for e in events]
    assert "references" in names
    assert "strategy" in names

    refs = next(e.data["items"] for e in events if e.type == "references")
    assert refs[0]["title"] == "客户健康度评估方法"
    assert refs[0]["document_id"] == 1

    items = next(e.data["items"] for e in events if e.type == "strategy")
    assert items[0]["title"] == "高层拜访"


def test_agent_loop_degrades_when_llm_offline(db, customer, offline_llm):
    session = chat_engine.create_session(db, customer_id=customer.id, scenario="strategy")
    events = list(chat_engine.run_turn(db, session, content="", scenario="strategy"))

    done = events[-1]
    assert done.type == "done"
    assert done.data["degraded"] is True

    strategy_events = [e for e in events if e.type == "strategy"]
    assert strategy_events, "降级仍需产出策略条目"
    assert strategy_events[0].data["items"][0]["priority"] == "recommended"


def test_agent_loop_refines_when_missing_strategy_block(db, customer, fake_llm, monkeypatch):
    # 草稿缺 ```json → 自批判触发精炼
    monkeypatch.setattr(tools, "rag_retrieve", lambda *a, **k: [])
    llm_adapter.set_chat_adapter(FakeAdapter(chunks=["只是一段普通回答，不含策略块。"]))

    result = generate(
        "strategy",
        context_builder.build_context(db, customer),
        customer,
        db,
        adapter=llm_adapter.get_chat_adapter(),
        question="给点建议",
    )
    assert result.iterations == 2  # reason + 1 次 refine
    assert result.degraded is False
    assert result.text  # 至少返回了文本，不空转


def test_generate_uses_real_store_for_references(db, customer, fake_llm):
    store = InMemoryVectorStore()
    store.add(
        ids=["c1"],
        documents=["关系紧密度衡量客户与团队的日常互动频率与质量，是健康度的核心维度。"],
        embeddings=[[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
        metadatas=[{
            "document_id": 1, "item_id": 1, "chunk_index": 0,
            "title": "方法", "category": "methodology", "status": "canonical",
        }],
    )

    def _embed(texts, dimensions=None):
        return [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in texts]

    payload = (
        "📎 参考《方法》\n\n### ✅ 推荐策略\n1. **维系关系**\n\n"
        '```json\n{"strategies": [{"priority": "recommended", "title": "维系关系", '
        '"urgency": "medium"}]}\n```'
    )
    llm_adapter.set_chat_adapter(FakeAdapter(chunks=[payload]))

    result = generate(
        "strategy",
        context_builder.build_context(db, customer),
        customer,
        db,
        adapter=llm_adapter.get_chat_adapter(),
        question="关系紧密度怎么衡量",
        embed_func=_embed,
        store=store,
    )
    assert result.references
    assert result.references[0]["title"] == "方法"
    assert result.references[0]["document_id"] == 1
    assert result.references[0]["score"] > 0.5
