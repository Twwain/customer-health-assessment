"""报告数据聚合、规则降级与 PDF 评分、行动和趋势展示。"""

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from reportlab.platypus import KeepTogether, Paragraph, Table

from database import Base
from models import AssessmentHistory, Customer
from schemas import AssessmentResponse, DimensionScore, FactorScoreItem
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
        customer_satisfaction=3,
        contract_amount=50,
        growth_potential="低",
        custom_fields={},
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@pytest.fixture(autouse=True)
def _reset_adapters():
    llm_adapter.reset_adapters()
    yield
    llm_adapter.reset_adapters()


@pytest.fixture()
def offline_llm():
    def unavailable(*args, **kwargs):
        raise LLMUnavailableError("未配置 API Key")
    llm_adapter.set_chat_adapter(SimpleNamespace(chat_completion_once=unavailable))


def _pdf_starts_ok(pdf_bytes: bytes) -> bool:
    return pdf_bytes[:4] == b"%PDF" and len(pdf_bytes) > 500


# ── 数据聚合 ───────────────────────────────────────────────────────────────

def test_build_report_data_offline_uses_degraded_strategies(db, customer, offline_llm):
    report = build_report_data(db, customer, include_ai=True)
    assert report.has_ai is True
    assert report.degraded is True
    assert report.strategy_items, "降级时仍应产出规则引擎建议"
    assert all(i["reason"] == "规则兜底建议。" for i in report.strategy_items)
    assert not report.references
    assert any("规则兜底" in warning for warning in report.warnings)


def test_build_report_data_ai_off_does_not_call_llm(db, customer):
    def unexpected_call(*args, **kwargs):
        pytest.fail("include_ai=False 不应调用模型")
    llm_adapter.set_chat_adapter(SimpleNamespace(chat_completion_once=unexpected_call))
    report = build_report_data(db, customer, include_ai=False)
    assert report.has_ai is False
    assert report.strategy_items == []


# ── PDF 生成 ───────────────────────────────────────────────────────────────


def test_pdf_offline_degraded_note(db, customer, offline_llm):
    report = build_report_data(db, customer, include_ai=True)
    gen = PdfReportGenerator()
    pdf = gen.generate(
        report.assessment,
        strategy_items=report.strategy_items,
        references=report.references,
        trend=report.trend,
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


def _collect_flow_text(flows) -> str:
    """递归收集 reportlab flowable 树里的所有段落文本（含 Table 单元格）。"""
    texts: list[str] = []
    for flow in flows:
        if isinstance(flow, Paragraph):
            texts.append(flow.text)
        elif isinstance(flow, Table):
            for row in flow._cellvalues:
                texts.append(_collect_flow_text(row))
        elif isinstance(flow, KeepTogether):
            texts.append(_collect_flow_text(flow._content))
    return "".join(texts)


def test_factor_status_text_strips_score_suffix():
    """PDF 维度卡的因子明细：去得分后缀与基准值，统一为「名称：现状」。"""
    f = PdfReportGenerator._factor_status_text
    # 数值型（已填写）：基准不展示
    assert f("已识别占比 92%（100%）：+0.21分") == "已识别占比：92%"
    assert f("产品线覆盖 4 条（≥6条）：+0.0分") == "产品线覆盖：4 条"
    assert f("90天内主动分享 5 次（≥3次）：+0.32分") == "90天内主动分享：5 次"
    # 档级描述是现状的一部分，保留
    assert f("客户满意度 3/10（较低）：+0.09分") == "客户满意度：3/10（较低）"
    # 定性（「」→ 冒号；空值 → 未填写）
    assert f("年均合作金额趋势「下降」：+0.14分") == "年均合作金额趋势：下降"
    assert f("经济决策者识别度「」：+0分") == "经济决策者识别度：未填写"
    # 数值型未填写 → 未填写
    assert f("已识别占比 %（100%）：+0.21分") == "已识别占比：未填写"
    assert f("90天内主动分享 次（≥3次）：+0.32分") == "90天内主动分享：未填写"
    assert f("客情等级均值 （≥2）：+0.21分") == "客情等级均值：未填写"
    # 模板 "{value} 次" 空值残留双空格
    assert f("半年培训赋能  次（≥4次）：+0.135分") == "半年培训赋能：未填写"
    assert f("半年培训赋能 4 次（≥4次）：+0.14分") == "半年培训赋能：4 次"
    assert f("") == ""


def test_pdf_cover_shows_customer_industry_and_date(db, customer):
    """封面展示客户名称、行业与评估日期（不展示内部模型版本）。"""
    assessment = build_report_data(db, customer, include_ai=False).assessment
    gen = PdfReportGenerator()

    cover = gen._cover(assessment, industry=customer.industry)
    text = _collect_flow_text(cover)

    assert customer.customer_name in text
    assert customer.industry in text
    assert "评估日期" in text
    assert "评估模型" not in text


def test_pdf_dimension_cards_render_grouped_factors(db, customer):
    """维度因子带 sub_dimension 时走按二级维度分组渲染分支（非旧快照兜底）。"""
    assessment = AssessmentResponse(
        customer_id=customer.id,
        customer_name=customer.customer_name,
        total_score=60.0,
        max_score=100,
        level="亚健康",
        level_color="#DD5B00",
        dimensions=[
            DimensionScore(
                key="kcr",
                name="客户关系网络",
                score=12.0,
                max_score=20.0,
                details=["已识别占比：92%"],
                factors=[
                    FactorScoreItem(
                        field="kcr_01",
                        label="已识别决策链人数占比",
                        sub_dimension="决策链覆盖度",
                        detail="已识别占比：92%",
                        score=2.1,
                    ),
                    FactorScoreItem(
                        field="kcr_02",
                        label="关键人客情等级",
                        sub_dimension="决策链覆盖度",
                        detail="关键人等级：高",
                        score=2.1,
                    ),
                    FactorScoreItem(
                        field="kcr_05",
                        label="高层关系",
                        sub_dimension="信息互通",
                        detail="高层关系：已建立",
                        score=1.5,
                    ),
                ],
            )
        ],
        risk_alerts=[],
        alerts=[],
        suggestions=[],
        config_version="2026.08",
        assessed_at=datetime.datetime.now(datetime.timezone.utc),
    )
    gen = PdfReportGenerator()
    cards = gen._dimension_cards(assessment)
    text = _collect_flow_text(cards)
    assert "决策链覆盖度" in text and "信息互通" in text
    assert "已识别决策链人数占比" in text and "关键人客情等级" in text

    pdf = gen.generate(assessment)
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
    # 直接渲染趋势图：防止异常被章节内 try/except 吞掉而漏图（如 hexval 颜色格式问题）
    img = gen._render_trend_chart(report.trend)
    assert img is not None

    pdf = gen.generate(
        report.assessment,
        strategy_items=report.strategy_items,
        references=report.references,
        trend=report.trend,
    )
    assert _pdf_starts_ok(pdf)
