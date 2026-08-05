"""Step 7 (M5) 报告整合测试：AI 策略建议 / 知识溯源 / 健康分趋势 进 PDF。

覆盖：LLM 不可用降级、LLM 可用时结构化策略解析、趋势曲线渲染、include_ai=False 回退。
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from database import Base, get_db
from models import AssessmentHistory, Customer
from services.ai import llm_adapter
from services.ai.llm_adapter import LLMUnavailableError
from services.pdf_report import PdfReportGenerator
from services.report_builder import build_report_data


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(bind=eng)


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


class _FakeAdapter:
    def __init__(self, chunks=None, error=None, available=True, model="fake-chat"):
        self.chunks = chunks if chunks is not None else ["你好。"]
        self.error = error
        self.available = available
        self.model = model
        self.calls = []

    def status(self):
        return {"available": self.available, "model": self.model}

    def stream_chat_completion(self, messages, *, temperature=None, max_tokens=None, extra=None, on_usage=None):
        self.calls.append(list(messages))
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk


@pytest.fixture(autouse=True)
def _reset_adapters():
    llm_adapter.reset_adapters()
    yield
    llm_adapter.reset_adapters()


@pytest.fixture()
def fake_llm():
    adapter = _FakeAdapter()
    llm_adapter.set_chat_adapter(adapter)
    return adapter


@pytest.fixture()
def offline_llm():
    adapter = _FakeAdapter(available=False, error=LLMUnavailableError("未配置 API Key"))
    llm_adapter.set_chat_adapter(adapter)
    return adapter


_STRATEGY_PAYLOAD = (
    "以下是为该客户生成的策略建议：\n\n"
    "```json\n"
    "[\n"
    '  {"priority": "recommended", "title": "立即安排高层回访", "urgency": "high",\n'
    '   "reason": "长期未联系且竞品介入", "action": "本月内拜访", "expected_outcome": "重建联系", "reference": "客户分级标准"},\n'
    '  {"priority": "long_term", "title": "扩大合作面", "urgency": "low",\n'
    '   "reason": "增长潜力低", "action": "挖掘新需求", "expected_outcome": "提升商业价值", "reference": ""}\n'
    "]\n"
    "```\n\n请参考执行。"
)


def _pdf_starts_ok(pdf_bytes: bytes) -> bool:
    return pdf_bytes[:4] == b"%PDF" and len(pdf_bytes) > 500


# ── 数据聚合 ───────────────────────────────────────────────────────────────

def test_build_report_data_offline_uses_degraded_strategies(db, customer, offline_llm):
    report = build_report_data(db, customer, include_ai=True)
    assert report.has_ai is True
    assert report.degraded is True
    assert report.strategy_items, "降级时仍应产出规则引擎建议"
    # 降级建议带规则引擎溯源标记
    assert any("规则引擎" in (i.get("reference") or "") for i in report.strategy_items)


def test_build_report_data_with_ai_parses_strategy(db, customer, fake_llm):
    llm_adapter.set_chat_adapter(_FakeAdapter(chunks=[_STRATEGY_PAYLOAD]))
    report = build_report_data(db, customer, include_ai=True)
    assert report.degraded is False
    assert report.has_ai is True
    titles = [i["title"] for i in report.strategy_items]
    assert "立即安排高层回访" in titles
    assert "扩大合作面" in titles
    # 优先级被规整
    by_title = {i["title"]: i for i in report.strategy_items}
    assert by_title["立即安排高层回访"]["priority"] == "recommended"
    assert by_title["扩大合作面"]["priority"] == "long_term"
    assert report.trend is not None


def test_build_report_data_ai_off_does_not_call_llm(db, customer, fake_llm):
    report = build_report_data(db, customer, include_ai=False)
    assert report.has_ai is False
    assert report.strategy_items == []
    assert fake_llm.calls == []  # 不应触发任何 LLM 调用


def test_build_report_data_error_falls_back_to_degraded(db, customer, fake_llm):
    llm_adapter.set_chat_adapter(_FakeAdapter(error=RuntimeError("boom")))
    report = build_report_data(db, customer, include_ai=True)
    assert report.degraded is True
    assert report.has_ai is True
    assert report.error and "boom" in report.error
    assert report.strategy_items, "异常后仍应有兜底建议"


# ── PDF 生成 ───────────────────────────────────────────────────────────────

def test_pdf_with_ai_strategies(db, customer, fake_llm):
    llm_adapter.set_chat_adapter(_FakeAdapter(chunks=[_STRATEGY_PAYLOAD]))
    report = build_report_data(db, customer, include_ai=True)
    gen = PdfReportGenerator()
    pdf = gen.generate(
        report.assessment,
        strategy_items=report.strategy_items,
        references=report.references,
        trend=report.trend,
        degraded=report.degraded,
        ai_error=report.error,
    )
    assert _pdf_starts_ok(pdf)


def test_pdf_offline_degraded_note(db, customer, offline_llm):
    report = build_report_data(db, customer, include_ai=True)
    gen = PdfReportGenerator()
    pdf = gen.generate(
        report.assessment,
        strategy_items=report.strategy_items,
        references=report.references,
        trend=report.trend,
        degraded=report.degraded,
        ai_error=report.error,
    )
    assert _pdf_starts_ok(pdf)


def test_pdf_include_ai_false_backward_compatible(db, customer):
    report = build_report_data(db, customer, include_ai=False)
    gen = PdfReportGenerator()
    pdf = gen.generate(
        report.assessment,
        strategy_items=report.strategy_items,
        references=report.references,
        trend=report.trend,
    )
    assert _pdf_starts_ok(pdf)


def test_pdf_with_trend_chart(db, customer):
    """插入两次历史评估，使趋势点 >= 2，触发 matplotlib 趋势图渲染。"""
    now = datetime.datetime.now()
    for days_ago, score in ((10, 18.5), (0, 42.0)):
        db.add(AssessmentHistory(
            customer_id=customer.id,
            total_score=score,
            max_score=100,
            level="风险",
            level_color="#ef4444",
            dimensions=[],
            risk_alerts=[],
            factor_snapshot={},
            assessed_at=now - datetime.timedelta(days=days_ago),
        ))
    db.commit()

    report = build_report_data(db, customer, include_ai=False)
    assert report.trend is not None
    assert len(report.trend.points) >= 2

    gen = PdfReportGenerator()
    pdf = gen.generate(
        report.assessment,
        strategy_items=report.strategy_items,
        references=report.references,
        trend=report.trend,
    )
    assert _pdf_starts_ok(pdf)
