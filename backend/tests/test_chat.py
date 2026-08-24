"""AI 对话引擎测试。

覆盖：Prompt 模板加载与热更新、安全护栏、上下文注入、策略结构化解析、
多轮上下文窗口、LLM 降级兜底、SSE 事件协议、会话与反馈接口。
"""

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
from seed_factors import GOOD_FACTORS


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
        customer_satisfaction=3,
        contract_amount=50,
        growth_potential="低",
        custom_fields={"risk_07": "是但可控"},
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
        self.tool_args: list = []
        self.extra_args: list = []

    def status(self):
        return {
            "provider": "fake",
            "model": self.model,
            "base_url": "http://fake",
            "available": self.available,
            "reason": "" if self.available else "测试用不可用适配器",
        }

    def stream_chat_completion(
        self, messages, *, temperature=None, max_tokens=None, extra=None, on_usage=None, tools=None, on_tool_calls=None
    ):
        self.calls.append(list(messages))
        self.tool_args.append(tools)
        self.extra_args.append(extra)
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


def test_streaming_sanitizer_masks_values_split_across_chunks():
    sanitizer = guardrails.StreamingOutputSanitizer()
    visible = "".join(
        sanitizer.feed(chunk)
        for chunk in ("请联系 13812", "345678，密钥: sk-abc", "def1234567890 后续处理。")
    ) + sanitizer.flush()
    assert "13812345678" not in visible
    assert "sk-abcdef1234567890" not in visible
    assert "138****5678" in visible
    assert "***" in visible


# ══════════════════════════ 上下文注入 ══════════════════════════════════════


def test_context_contains_quantitative_facts(db, customer):
    ctx = context_builder.build_context(db, customer)
    assert ctx.assessment is not None
    assert "示例汽车集团" in ctx.customer_text
    assert "10" in ctx.customer_text            # 当前 V3.0 基础客情分
    assert "KCR 关键客户关系" in ctx.customer_text
    assert "RISK 竞争态势与风险信号" in ctx.customer_text
    assert "未检索到相关知识" in ctx.knowledge_text


def test_context_excludes_personal_phone(db, customer):
    ctx = context_builder.build_context(db, customer)
    assert customer.contact_phone not in ctx.customer_text


def test_context_excludes_removed_legacy_fields(db, customer):
    ctx = context_builder.build_context(db, customer)
    for label in ("最近联系：", "回款状态：", "竞品介入："):
        assert label not in ctx.customer_text

    profile = tools.profile_query(customer)
    for field in ("last_contact_date", "payment_status", "competitor_involvement", "risk_signals"):
        assert field not in profile


def test_context_without_customer_is_generic(db):
    ctx = context_builder.build_context(db, None)
    assert ctx.assessment is None
    assert "未关联具体客户" in ctx.customer_text


def test_alert_context_lists_alerts(db, customer):
    ctx = context_builder.build_context(db, customer)
    assert "待解读的预警" in ctx.alert_text
    assert "RISK维度" in ctx.alert_text


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


def test_split_strategy_payload_recovers_fenced_json_with_trailing_note():
    """未闭合 json 围栏 + 尾部说明文字：解析出策略项且正文不暴露原始 JSON。"""
    text = (
        "**数据缺口说明**：未检索到相关知识。\n"
        "```json\n"
        '{"strategies": [{"priority": "recommended", "title": "策略A", "urgency": "high", '
        '"reason": "r", "action": "a", "expected_outcome": "o"}]}\n'
        "本页面仅显示策略摘要，完整报告请点击「生成报告」。"
    )
    body, items = strategy.split_strategy_payload(text)
    assert len(items) == 1
    assert items[0]["title"] == "策略A"
    assert "```json" not in body
    assert "数据缺口说明" in body
    # 围栏后的说明文字应保留（与闭合围栏路径行为一致）
    assert "本页面仅显示策略摘要，完整报告请点击「生成报告」" in body


def test_split_strategy_payload_recovers_compact_fence_without_newline():
    """紧凑写法 ```json{...}（围栏后无换行）不丢 JSON 首字符。"""
    text = (
        '```json{"strategies": [{"priority": "recommended", "title": "策略B", '
        '"urgency": "medium", "reason": "r", "action": "a", "expected_outcome": "o"}]}'
    )
    body, items = strategy.split_strategy_payload(text)
    assert len(items) == 1
    assert items[0]["title"] == "策略B"
    assert "```json" not in body


def test_split_strategy_payload_strips_empty_unclosed_fence():
    """截断在围栏处（围栏后无内容）：正文不残留 ```json 字样。"""
    body, items = strategy.split_strategy_payload("正常回答\n```json")
    assert items == []
    assert "正常回答" in body
    assert "```json" not in body


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
    assert "原因：" not in markdown
    assert "预期：" not in markdown


def test_alert_consumers_include_complete_v3_set(db, customer):
    customer.custom_fields = {
        "kcr_02": "[3,2,-1,1]",
        "risk_08b": "高(<60%)",
        "risk_08c": "危机",
        "risk_07": "是且进展顺利",
        "risk_05": "≥2人变动",
        "risk_06": "下降≥20%",
        "kcr_08": "无",
        "kcr_07": "<20%支持",
        "or_02": "0次",
        "ci_03": "0项",
        "svc_03": "",
    }
    db.commit()

    assessment = tools.score_query(customer)
    assert {alert.id for alert in assessment.alerts} == {
        "key_person_opposition",
        "kcr_dimension_low",
        "risk_dimension_low",
        "ci_dimension_low",
        "ces_critical",
        "customer_business_risk_critical",
    }

    ctx = context_builder.build_context(db, customer, assessment=assessment)
    assert ctx.alert_text.count("（规则 id：") == 6
    assert "预警未展开" not in ctx.alert_text

    items = strategy.build_degraded_strategies(assessment)
    assert len(items) <= strategy.MAX_DEGRADED_STRATEGIES
    assert {item["priority"] for item in items} <= set(strategy.PRIORITIES)
    assert any(item["priority"] == "recommended" for item in items)


def test_degraded_strategies_for_healthy_customer(db):
    from services.scoring import get_scoring_strategy

    healthy = Customer(
        customer_name="某省政务云",
        cooperation_years=6,
        contact_frequency="每周",
        customer_satisfaction=9,
        contract_amount=800,
        growth_potential="高",
        custom_fields=dict(GOOD_FACTORS),
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
    assert result["assessment"]["total_score"] == 10.0

    messages = db.query(ChatMessage).order_by(ChatMessage.id).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "这个客户风险大吗？"


def test_turn_injects_quantitative_context_into_prompt(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    chat_engine.complete_turn(db, session, content="怎么办？")

    sent = fake_llm.calls[-1]
    assert sent[0].role == "system"
    assert "客情评估智能体" in sent[0].content
    assert "10" in sent[-1].content              # 基础客情分进了本轮 user prompt
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
    assert record.total_score == 10.0


@pytest.mark.parametrize(
    ("scenario", "keyword"),
    [
        ("assessment", "评估摘要"),
        ("strategy", "策略摘要"),
        ("alert_analysis", "风险摘要"),
    ],
)
def test_scenario_summary_hint_appended(db, customer, fake_llm, scenario, keyword):
    session = chat_engine.create_session(db, customer_id=customer.id, scenario=scenario)
    result = chat_engine.complete_turn(db, session, content="", scenario=scenario)

    content = result["message"]["content"]
    assert f"本页面仅显示{keyword}" in content
    assert "完整报告请点击「生成报告」" in content


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


# ══════════════════════════ 降级═══════════════════════════════════


def test_degrades_to_rule_engine_when_llm_unavailable(db, customer, offline_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    result = chat_engine.complete_turn(db, session, content="帮我分析一下", scenario="assessment")

    assert result["degraded"] is True
    content = result["message"]["content"]
    assert "规则引擎兜底结果" in content
    assert "10 / 100" in content
    assert "KCR 关键客户关系" in content
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


def test_regenerate_falls_back_to_default_question_without_user_message(db, customer, fake_llm):
    """快捷场景（如 AI 评估）历史只有 assistant 回复、没有用户提问时，
    regenerate 用场景默认提问重放，并删除待重生成的那条回复。"""
    session = chat_engine.create_session(db, customer_id=customer.id)
    assistant = ChatMessage(session_id=session.id, role="assistant", content="评估结论")
    db.add(assistant)
    db.commit()

    content, exclude_id = chat_engine.prepare_regenerate(db, session, scenario="assessment")

    assert content == "请为该客户生成综合评估结论"
    assert exclude_id == assistant.id
    assert db.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 0


def test_regenerate_without_messages_returns_empty(db, customer, fake_llm):
    """会话没有任何消息时，regenerate 返回空、不误删数据。"""
    session = chat_engine.create_session(db, customer_id=customer.id)

    content, exclude_id = chat_engine.prepare_regenerate(db, session, scenario="assessment")

    assert content == ""
    assert exclude_id is None


def test_set_feedback(db, customer, fake_llm):
    session = chat_engine.create_session(db, customer_id=customer.id)
    result = chat_engine.complete_turn(db, session, content="问题")
    message = chat_engine.set_feedback(db, result["message"]["id"], "up")
    assert message.feedback == "up"


def test_chat_session_streaming_cleared_after_complete_turn(db, customer, fake_llm):
    """生成完成后进程内 streaming 标记应复位（异常路径由 finally 兜底）。"""
    session = chat_engine.create_session(db, customer_id=customer.id, scenario="assessment")
    assert chat_engine.is_session_streaming(session.id) is False
    chat_engine.complete_turn(db, session, content="", scenario="assessment")
    assert chat_engine.is_session_streaming(session.id) is False


def test_streaming_registry_claim_and_release():
    """进程内注册表：抢占互斥、生成结束后释放。"""
    sid = 10_001
    chat_engine._release_streaming(sid)  # 兜底清理，避免失败用例污染
    assert chat_engine.is_session_streaming(sid) is False
    assert chat_engine._claim_streaming(sid) is True
    assert chat_engine.is_session_streaming(sid) is True
    # 同一会话并发生成被拒绝
    assert chat_engine._claim_streaming(sid) is False
    chat_engine._release_streaming(sid)
    assert chat_engine.is_session_streaming(sid) is False


def test_normalize_ref_title():
    """引用标题归一化：剥掉书刊引号/括号包装与尾部标点。"""
    from services.ai.chat_engine import _normalize_ref_title

    assert _normalize_ref_title("《客户健康评估方法论》") == "客户健康评估方法论"
    assert _normalize_ref_title("「风险预警」。") == "风险预警"
    assert _normalize_ref_title(" 制造业白皮书 ") == "制造业白皮书"
    assert _normalize_ref_title("") == ""


# ══════════════════════════ 接口层═══════════════════════════════


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
    assert context["assessment"]["total_score"] == 10.0
    assert context["trend"]["level"] == "高危"

    done = events[-1][1]
    assert done["degraded"] is False
    assert done["message"]["role"] == "assistant"
    timings = done["timings"]
    assert timings["total_ms"] == done["latency_ms"]
    assert timings["llm_call_count"] >= 1
    assert timings["llm_calls"][0]["phase"] in {"reason", "direct"}
    assert timings["llm_calls"][0]["prompt_chars"] > 0
    assert timings["llm_calls"][0]["max_tokens"] == 2500
    assert timings["time_to_first_client_delta_ms"] is not None
    assert all(
        key in timings
        for key in ("scoring_ms", "context_ms", "rag_ms", "generation_ms", "persist_ms")
    )


def test_send_message_non_stream_returns_json(client, customer, fake_llm):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    response = client.post(
        f"/api/chat/sessions/{session_id}/messages",
        json={"content": "非流式", "stream": False},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["message"]["content"] == "你好，这是测试回复。"
    assert body["assessment"]["level"] == "高危"
    assert body["timings"]["total_ms"] == body["latency_ms"]
    assert fake_llm.extra_args[-1]["thinking"]["type"] == "disabled"


def test_free_qa_stream_masks_sensitive_values_across_chunks(db, customer):
    adapter = FakeAdapter(chunks=["联系电话 13812", "345678，密钥 sk-abc", "def1234567890"])
    llm_adapter.set_chat_adapter(adapter)
    session = chat_engine.create_session(db, customer_id=customer.id, scenario="free_qa")
    events = list(chat_engine.run_turn(db, session, content="联系方式"))
    visible = "".join(e.data["text"] for e in events if e.type == "delta")
    assert "13812345678" not in visible
    assert "sk-abcdef1234567890" not in visible
    assert "138****5678" in visible
    assert "***" in visible


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
    assert body["chat_thinking_enabled"] is False
    assert body["report_thinking_enabled"] is True


def test_status_endpoint_reports_degraded(client, offline_llm):
    body = client.get("/api/chat/status").json()
    assert body["available"] is False
    assert body["degraded"] is True
    assert body["reason"]


# ══════════════════════════ 适配层 ══════════════════════════════════════════


def test_adapter_unavailable_without_api_key(monkeypatch):
    # adapter 先检查 BASE_URL 再检查 API Key，mock 一个有效地址让测试不依赖本机 .env
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:9999/v1")
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


# ── 大模型兼容协议解析（用假 httpx 驱动真实适配器）───────────────────────────


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
    adapter = llm_adapter.CompatAdapter(
        name="test-provider",
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="test-model",
        max_retries=kwargs.pop("max_retries", 1),
        retry_backoff=0,
        **kwargs,
    )
    return adapter, fake


def test_chat_completion_parses_compat_payload(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(
                json_data={
                    "model": "test-model",
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


def test_chat_completion_removes_unsupported_thinking(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(status_code=400, text="unknown field thinking"),
            _FakeResponse(json_data={"choices": [{"message": {"content": "兼容成功"}}]}),
        ],
    )
    result = adapter.chat_completion(
        [{"role": "user", "content": "hi"}],
        extra={"thinking": {"type": "disabled"}},
    )
    assert result.content == "兼容成功"
    assert "thinking" in fake.requests[0]["json"]
    assert "thinking" not in fake.requests[1]["json"]


def test_chat_completion_compat_fallback_works_with_zero_retries(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(status_code=400, text="unknown field thinking"),
            _FakeResponse(json_data={"choices": [{"message": {"content": "ok"}}]}),
        ],
        max_retries=0,
    )
    result = adapter.chat_completion(
        [{"role": "user", "content": "hi"}],
        extra={"thinking": {"type": "disabled"}},
    )
    assert result.content == "ok"
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


def test_stream_removes_unsupported_thinking(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(status_code=400, text="unknown field thinking"),
            _FakeResponse(lines=['data: {"choices": [{"delta": {"content": "ok"}}]}']),
        ],
        stream_usage=False,
    )
    chunks = adapter.stream_chat_completion(
        [{"role": "user", "content": "hi"}],
        extra={"thinking": {"type": "disabled"}},
    )
    assert "".join(chunks) == "ok"
    assert "thinking" in fake.requests[0]["json"]
    assert "thinking" not in fake.requests[1]["json"]


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


# ══════════════════════════ M4 Agent Loop═══════════════════


def _agent_chunk(document_id=1, item_id=1, title="客户健康度评估方法", category="methodology"):
    return RetrievedChunk(
        document_id=document_id,
        chunk_index=0,
        item_id=item_id,
        item_title=title,
        category=category,
        content="KCR 关键客户关系衡量与客户决策链关键岗位的关系深度与支持度。",
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
    visible = "".join(e.data["text"] for e in events if e.type == "delta")
    assert "```json" not in visible
    assert '"strategies"' not in visible
    done = next(e for e in events if e.type == "done")
    assert done.data["tokens_used"] == 128


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

    streamed_events = []
    result = generate(
        "strategy",
        context_builder.build_context(db, customer),
        customer,
        db,
        adapter=llm_adapter.get_chat_adapter(),
        question="给点建议",
        on_event=lambda event_type, data: streamed_events.append((event_type, data)),
    )
    assert result.iterations == 2  # reason + 1 次 refine
    assert result.degraded is False
    assert result.text  # 至少返回了文本，不空转
    assert streamed_events[0][0] == "delta"
    assert any(event_type == "replace" for event_type, _ in streamed_events)
    assert result.timings["llm_calls"][0]["prompt_chars"] > 0
    assert result.timings["llm_calls"][0]["max_tokens"] == 8000


class _EmptyThenContentAdapter(FakeAdapter):
    """前 ``empty_rounds`` 次调用返回空，之后返回预设内容（模拟模型偶发空响应）。"""

    def __init__(self, empty_rounds=1, chunks=None):
        super().__init__(chunks=chunks or [])
        self._empty_rounds = empty_rounds
        self._round = 0

    def stream_chat_completion(self, messages, **kwargs):
        self.calls.append(list(messages))
        self.tool_args.append(kwargs.get("tools"))
        if kwargs.get("on_usage"):
            kwargs["on_usage"](self.usage)
        if self._round < self._empty_rounds:
            self._round += 1
            return
        self._round += 1
        for chunk in self.chunks:
            yield chunk


def test_agent_loop_retries_when_first_draft_empty(db, customer, monkeypatch):
    """首次生成返回空草稿时自动不带工具重试一次，避免偶发空响应直接失败。"""
    monkeypatch.setattr(tools, "rag_retrieve", lambda *a, **k: [])
    payload = (
        "### ✅ 推荐策略\n1. **高层拜访**\n\n"
        '```json\n{"strategies": [{"priority": "recommended", "title": "高层拜访", '
        '"urgency": "high", "reason": "竞品介入", "action": "VP 级拜访", '
        '"expected_outcome": "稳固关系"}]}\n```'
    )
    adapter = _EmptyThenContentAdapter(chunks=[payload])
    result = generate(
        "strategy",
        context_builder.build_context(db, customer),
        customer,
        db,
        adapter=adapter,
        question="给点建议",
    )

    assert result.degraded is False
    assert "高层拜访" in result.text
    assert any("首次生成返回为空" in w for w in result.warnings)
    # 重试请求不带工具，避免工具循环吞掉正文
    assert adapter.tool_args[-1] is None


def test_agent_loop_degrades_when_draft_stays_empty(db, customer, monkeypatch):
    """重试后仍为空时降级为规则引擎，保证策略条目仍然可见。"""
    monkeypatch.setattr(tools, "rag_retrieve", lambda *a, **k: [])
    llm_adapter.set_chat_adapter(FakeAdapter(chunks=[]))

    result = generate(
        "strategy",
        context_builder.build_context(db, customer),
        customer,
        db,
        adapter=llm_adapter.get_chat_adapter(),
        question="给点建议",
    )

    assert result.degraded is True
    assert result.text
    assert result.degraded_items
    assert any("返回空内容" in w for w in result.warnings)


def test_generate_uses_real_store_for_references(db, customer, fake_llm):
    store = InMemoryVectorStore()
    store.add(
        ids=["c1"],
        documents=["KCR 关键客户关系衡量与客户决策链关键岗位的关系深度与支持度，是健康度的核心维度。"],
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
        question="KCR 关键客户关系怎么衡量",
        embed_func=_embed,
        store=store,
    )
    assert result.references
    assert result.references[0]["title"] == "方法"
    assert result.references[0]["document_id"] == 1
    assert result.references[0]["score"] > 0.5


# ── 客户对比工具与 function calling ──────────────────────────────────────


def _make_customer(db, name, industry, satisfaction=5, **kwargs):
    c = Customer(
        customer_name=name,
        industry=industry,
        contact_person="",
        contact_phone="",
        cooperation_years=kwargs.get("cooperation_years", 1.0),
        contact_frequency=kwargs.get("contact_frequency", "每月"),
        customer_satisfaction=satisfaction,
        contract_amount=kwargs.get("contract_amount", 100),
        growth_potential=kwargs.get("growth_potential", "中"),
        custom_fields={},
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_customer_compare_returns_all_customers(db, customer):
    _make_customer(db, "能源甲", "能源", satisfaction=9)
    _make_customer(db, "能源乙", "能源", satisfaction=4)
    result = tools.customer_compare(db=db)
    assert result["count"] == 3
    assert result["avg_score"] is not None
    assert all("dimensions" in row for row in result["customers"])
    assert any(row["industry"] == "能源" for row in result["customers"])


def test_customer_compare_filters_industry_and_exclude(db, customer):
    _make_customer(db, "能源甲", "能源")
    _make_customer(db, "金融甲", "金融")
    result = tools.customer_compare(db=db, industry="能源", exclude_customer_id=customer.id)
    assert result["scope"] == "能源"
    assert result["customers"]
    assert all(row["industry"] == "能源" for row in result["customers"])
    assert all(row["id"] != customer.id for row in result["customers"])


def test_execute_tool_unknown_name_returns_error(db):
    result = tools.execute_tool("not_exist", {}, db=db)
    assert "error" in result


def test_chat_completion_parses_tool_calls(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(
                json_data={
                    "model": "test-model",
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "customer_compare",
                                            "arguments": '{"industry": "能源"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"total_tokens": 50},
                }
            )
        ],
    )
    result = adapter.chat_completion(
        [{"role": "user", "content": "hi"}], tools=tools.TOOL_SCHEMAS
    )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "customer_compare"
    assert "能源" in result.tool_calls[0]["arguments"]
    assert fake.requests[0]["json"].get("tools") == tools.TOOL_SCHEMAS


def test_stream_parses_tool_calls(monkeypatch):
    adapter, _ = _adapter(
        monkeypatch,
        [
            _FakeResponse(
                lines=[
                    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"customer_compare","arguments":"{\\"industry\\": \\"能源\\"}"}}]}}]}',
                    "data: [DONE]",
                ]
            )
        ],
    )
    got: list[list[dict]] = []
    list(adapter.stream_chat_completion([{"role": "user", "content": "hi"}], on_tool_calls=got.append))
    assert len(got) == 1
    assert got[0][0]["name"] == "customer_compare"
    assert "能源" in got[0][0]["arguments"]


class ToolCallingFakeAdapter(FakeAdapter):
    """前 ``tool_rounds`` 轮请求工具，后续轮输出正文，用于验证 function calling 循环。"""

    def __init__(self, tool_calls, final_chunks, tool_rounds=1, **kwargs):
        super().__init__(chunks=[], **kwargs)
        self.tool_calls = tool_calls
        self.final_chunks = final_chunks
        self.tool_rounds = tool_rounds
        self.rounds = 0

    def stream_chat_completion(
        self, messages, *, temperature=None, max_tokens=None, extra=None, on_usage=None, tools=None, on_tool_calls=None
    ):
        self.calls.append(list(messages))
        self.tool_args.append(tools)
        self.rounds += 1
        if self.rounds <= self.tool_rounds:
            if on_tool_calls:
                on_tool_calls(self.tool_calls)
            if on_usage:
                on_usage(self.usage)
            return
        for chunk in self.final_chunks:
            yield chunk
        if on_usage:
            on_usage(self.usage)


def test_free_qa_runs_customer_compare_tool(db, customer):
    tool_calls = [
        {
            "id": "call_1",
            "name": "customer_compare",
            "arguments": json.dumps({"exclude_customer_id": customer.id}),
        }
    ]
    adapter = ToolCallingFakeAdapter(tool_calls=tool_calls, final_chunks=["对比结论：满意度最高。"])
    llm_adapter.set_chat_adapter(adapter)
    session = chat_engine.create_session(db, title="", customer_id=customer.id, scenario="free_qa")
    events = list(chat_engine.run_turn(db, session, content="和其他客户对比有什么优势"))
    done = next(e for e in events if e.type == "done")
    assert done.data["message"]["content"] == "对比结论：满意度最高。"
    second = adapter.calls[1]
    roles = [m["role"] for m in second if isinstance(m, dict)]
    assert "tool" in roles
    tool_msg = next(m for m in second if isinstance(m, dict) and m["role"] == "tool")
    assert '"count"' in tool_msg["content"] and '"customers"' in tool_msg["content"]


def test_free_qa_discards_text_emitted_with_tool_call(db, customer):
    """工具调用轮伴随文本时，应清除临时草稿并只持久化工具后的正式回答。"""

    class DraftingToolAdapter(ToolCallingFakeAdapter):
        def stream_chat_completion(self, messages, **kwargs):
            self.calls.append(list(messages))
            self.tool_args.append(kwargs.get("tools"))
            self.rounds += 1
            if self.rounds <= self.tool_rounds:
                on_tool_calls = kwargs.get("on_tool_calls")
                if on_tool_calls:
                    on_tool_calls(self.tool_calls)
                yield "工具调用前的临时草稿"
                return
            yield from self.final_chunks

    adapter = DraftingToolAdapter(
        tool_calls=[{"id": "call_draft", "name": "customer_compare", "arguments": "{}"}],
        final_chunks=["基于工具结果的最终结论"],
    )
    llm_adapter.set_chat_adapter(adapter)
    session = chat_engine.create_session(
        db, title="", customer_id=customer.id, scenario="free_qa"
    )

    events = list(chat_engine.run_turn(db, session, content="对比"))
    done = next(event for event in events if event.type == "done")

    assert any(
        event.type == "replace" and event.data.get("reason") == "tool_followup"
        for event in events
    )
    assert done.data["message"]["content"] == "基于工具结果的最终结论"


def test_knowledge_search_tool_dedups_and_returns_refs(db, monkeypatch):
    from services.rag.retriever import RetrievedChunk as RC

    def fake_retrieve(
        query, *, customer=None, category=None, top_k=8, window=None, status="canonical", db=None, **kwargs
    ):
        return [
            RC(
                document_id=1, chunk_index=0, item_id=1, item_title="规范A",
                category="内部规范", content="内容A", score=0.9, metadata={},
            ),
            RC(
                document_id=2, chunk_index=1, item_id=2, item_title="指标B",
                category="内部指标", content="内容B", score=0.8, metadata={},
            ),
        ]

    monkeypatch.setattr("services.rag.retriever.retrieve_knowledge", fake_retrieve)
    result = tools.knowledge_search(db=db, query="规范", top_k=8)
    assert result["count"] == 2

    tc = [{"id": "call_k", "name": "knowledge_search", "arguments": json.dumps({"query": "规范"})}]
    messages, refs = tools.append_tool_results(
        [{"role": "user", "content": "q"}], tc, db=db, exclude_ids={"1:0"}
    )
    payload = json.loads(messages[-1]["content"])
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == "2:1"
    assert [r["id"] for r in refs] == ["2:1"]
    assert refs[0]["snippet"] == "内容B"


def test_free_qa_knowledge_search_tool_adds_references(db, customer, monkeypatch):
    from services.rag.retriever import RetrievedChunk as RC

    def fake_retrieve(
        query, *, customer=None, category=None, top_k=8, window=None, status="canonical", db=None, **kwargs
    ):
        if query == "规范":
            return [
                RC(
                    document_id=9, chunk_index=2, item_id=9, item_title="规范",
                    category="内部规范", content="知识内容", score=0.9, metadata={},
                )
            ]
        return [
            RC(
                document_id=8, chunk_index=1, item_id=8, item_title="预取",
                category="内部规范", content="预取内容", score=0.7, metadata={},
            )
        ]

    monkeypatch.setattr("services.rag.retriever.retrieve_knowledge", fake_retrieve)
    tool_calls = [
        {"id": "call_k", "name": "knowledge_search", "arguments": json.dumps({"query": "规范"})}
    ]
    adapter = ToolCallingFakeAdapter(tool_calls=tool_calls, final_chunks=["基于知识作答。"])
    llm_adapter.set_chat_adapter(adapter)
    session = chat_engine.create_session(db, title="", customer_id=customer.id, scenario="free_qa")
    events = list(chat_engine.run_turn(db, session, content="公司对回款有什么规范"))
    done = next(e for e in events if e.type == "done")
    refs = done.data["message"].get("references") or []
    assert any(r.get("id") == "9:2" for r in refs)


def test_chat_completion_retries_without_tools_on_400(monkeypatch):
    """非流式：供应商不支持 function calling 时去掉 tools 重试，避免整条对话降级。"""
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(status_code=400, text="tools unsupported"),
            _FakeResponse(
                json_data={
                    "model": "test-model",
                    "choices": [{"message": {"content": "正常回答"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 12},
                }
            ),
        ],
    )
    result = adapter.chat_completion(
        [{"role": "user", "content": "hi"}], tools=tools.TOOL_SCHEMAS
    )
    assert result.content == "正常回答"
    assert "tools" in fake.requests[0]["json"]
    assert "tools" not in fake.requests[1]["json"]


def test_stream_retries_without_tools_on_400(monkeypatch):
    """流式：供应商不支持 function calling 时去掉 tools 重试。"""
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(status_code=400, text="function calling unsupported"),
            _FakeResponse(
                lines=[
                    'data: {"choices":[{"delta":{"content":"流式回答"}}]}',
                    "data: [DONE]",
                ]
            ),
        ],
    )
    got = "".join(
        adapter.stream_chat_completion(
            [{"role": "user", "content": "hi"}], tools=tools.TOOL_SCHEMAS
        )
    )
    assert got == "流式回答"
    assert "tools" in fake.requests[0]["json"]
    assert "tools" not in fake.requests[1]["json"]


def test_tools_disabled_by_config(db, customer, monkeypatch):
    """LLM_TOOLS_ENABLED=false 时 free_qa 不携带 tools，仍正常生成。"""
    monkeypatch.setattr(config, "LLM_TOOLS_ENABLED", False)
    adapter = FakeAdapter(chunks=["普通回复"])
    llm_adapter.set_chat_adapter(adapter)
    session = chat_engine.create_session(db, title="", customer_id=customer.id, scenario="free_qa")
    events = list(chat_engine.run_turn(db, session, content="你好"))
    done = next(e for e in events if e.type == "done")
    assert done.data["message"]["content"] == "普通回复"
    assert adapter.tool_args == [None]


def test_free_qa_tool_round_cap_finishes_with_final_call(db, customer):
    """工具轮数达到上限后：回填本轮结果并以不带工具的收尾请求完成回答。"""
    tool_calls = [
        {
            "id": "call_cap",
            "name": "customer_compare",
            "arguments": json.dumps({"exclude_customer_id": customer.id}),
        }
    ]
    adapter = ToolCallingFakeAdapter(
        tool_calls=tool_calls, final_chunks=["基于对比数据给出结论。"], tool_rounds=5
    )
    llm_adapter.set_chat_adapter(adapter)
    session = chat_engine.create_session(db, title="", customer_id=customer.id, scenario="free_qa")
    events = list(chat_engine.run_turn(db, session, content="连续对比"))
    done = next(e for e in events if e.type == "done")
    assert "基于对比数据给出结论" in done.data["message"]["content"]
    assert "工具调用次数已达上限" in done.data["message"]["content"]
    # 5 轮工具 + 1 次收尾，共 6 次流式请求；收尾不带 tools
    assert len(adapter.calls) == 6
    assert adapter.tool_args[-1] is None
    assert all(t is not None for t in adapter.tool_args[:5])
    # usage 按轮累计（每轮 128）
    assert done.data["tokens_used"] == 128 * 6
    # 第 5 轮工具结果已回填给收尾请求
    last_round = adapter.calls[-1]
    assert any(isinstance(m, dict) and m.get("role") == "tool" for m in last_round)


def test_knowledge_search_tool_truncates_long_content(db, monkeypatch):
    """工具返回的知识正文按 TOOL_CONTENT_MAX_CHARS 截断，防止上下文撑爆。"""
    from services.rag.retriever import RetrievedChunk as RC

    def fake_retrieve(
        query, *, customer=None, category=None, top_k=8, window=None, status="canonical", db=None, **kwargs
    ):
        return [
            RC(
                document_id=1, chunk_index=0, item_id=1, item_title="长文",
                category="内部规范", content="x" * 5000, score=0.9, metadata={},
            )
        ]

    monkeypatch.setattr("services.rag.retriever.retrieve_knowledge", fake_retrieve)
    result = tools.knowledge_search(db=db, query="长文")
    assert len(result["results"][0]["content"]) <= tools.TOOL_CONTENT_MAX_CHARS
    assert result["results"][0]["snippet"] == "x" * 200


def test_graph_builder_tool_round_cap_finishes_with_final_call(db, customer):
    """graph_builder 工具循环超限后同样以不带工具的收尾请求完成回答。"""
    from services.ai.graph_builder import AssessmentStrategyAgent
    from services.ai.prompt_templates import get_template

    tool_calls = [{"id": "call_g", "name": "customer_compare", "arguments": "{}"}]
    adapter = ToolCallingFakeAdapter(
        tool_calls=tool_calls, final_chunks=["最终结论"], tool_rounds=5
    )
    agent = AssessmentStrategyAgent(adapter=adapter)
    messages = [
        llm_adapter.LLMMessage(role="system", content="sys"),
        llm_adapter.LLMMessage(role="user", content="问"),
    ]
    text, _ = agent._call_llm_with_tools(
        messages, get_template("assessment"), customer=customer, db=db
    )
    assert text == "最终结论"
    assert len(adapter.calls) == 6
    assert adapter.tool_args[-1] is None
    assert agent._tool_warning


def test_graph_builder_tool_round_cap_discards_tool_call_draft(db, customer):
    """模型边输出草稿边请求工具时，最终答案不得重新拼回已清除的草稿。"""
    from services.ai.graph_builder import AssessmentStrategyAgent
    from services.ai.prompt_templates import get_template

    class DraftingToolAdapter(ToolCallingFakeAdapter):
        def stream_chat_completion(self, messages, **kwargs):
            self.calls.append(list(messages))
            self.tool_args.append(kwargs.get("tools"))
            self.rounds += 1
            if self.rounds <= self.tool_rounds:
                on_tool_calls = kwargs.get("on_tool_calls")
                if on_tool_calls:
                    on_tool_calls(self.tool_calls)
                yield "工具调用前的临时草稿"
                return
            yield from self.final_chunks

    adapter = DraftingToolAdapter(
        tool_calls=[{"id": "call_draft", "name": "customer_compare", "arguments": "{}"}],
        final_chunks=["基于工具结果的最终结论"],
        tool_rounds=5,
    )
    agent = AssessmentStrategyAgent(adapter=adapter)
    messages = [
        llm_adapter.LLMMessage(role="system", content="sys"),
        llm_adapter.LLMMessage(role="user", content="问"),
    ]

    text, _ = agent._call_llm_with_tools(
        messages, get_template("assessment"), customer=customer, db=db
    )

    assert text == "基于工具结果的最终结论"


def test_usage_counts_last_value_per_round(db, customer):
    """同一轮流内 usage 重复出现时只累计最后一次，避免 tokens 虚高。"""

    class MultiUsageAdapter(ToolCallingFakeAdapter):
        def stream_chat_completion(
            self, messages, *, temperature=None, max_tokens=None, extra=None, on_usage=None, tools=None, on_tool_calls=None
        ):
            self.calls.append(list(messages))
            self.tool_args.append(tools)
            self.rounds += 1
            if self.rounds <= self.tool_rounds:
                if on_tool_calls:
                    on_tool_calls(self.tool_calls)
                if on_usage:
                    on_usage({"total_tokens": 30})
                    on_usage(self.usage)  # 同一轮重复出现，取最后一次 128
                return
            for chunk in self.final_chunks:
                yield chunk
            if on_usage:
                on_usage({"total_tokens": 30})
                on_usage(self.usage)

    tool_calls = [{"id": "call_u", "name": "customer_compare", "arguments": "{}"}]
    adapter = MultiUsageAdapter(tool_calls=tool_calls, final_chunks=["结论"], tool_rounds=1)
    llm_adapter.set_chat_adapter(adapter)
    session = chat_engine.create_session(db, title="", customer_id=customer.id, scenario="free_qa")
    events = list(chat_engine.run_turn(db, session, content="对比"))
    done = next(e for e in events if e.type == "done")
    # 1 轮工具 + 1 次收尾，每轮取最后一次 usage（128）；若按出现次数累加会虚高
    assert done.data["tokens_used"] == 128 * 2
