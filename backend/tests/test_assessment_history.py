"""评估历史与趋势测试。"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import AssessmentHistory, ChatMessage, ChatSession, Customer, KnowledgeChunk
from services import assessment_history
from services.scoring import get_scoring_strategy
from routers.assessment import get_overview
from routers.customers import list_customers


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def customer(db):
    c = Customer(
        customer_name="示例汽车集团",
        industry="制造",
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


# ── 快照写入 ──────────────────────────────────────────────────────────────


def test_record_assessment_writes_snapshot(db, customer):
    record = assessment_history.record_assessment(db, customer, assessed_by="pytest")
    assert record is not None
    assert record.customer_id == customer.id
    assert record.total_score == 12.5           # 空因子最低分口径（仅满意度 3 → 0.09）
    assert record.level == "高危"
    assert record.assessed_by == "pytest"
    assert len(record.dimensions) == 7
    assert record.risk_alerts


def test_factor_snapshot_covers_all_registered_factors(db, customer):
    record = assessment_history.record_assessment(db, customer)
    snapshot = record.factor_snapshot
    for field in (
        "customer_satisfaction",
        "kcr_01",
        "kcr_03b",
        "er_01",
        "or_01",
        "ci_01",
        "his_01",
        "his_09b",
        "risk_01",
        "risk_08b",
        "risk_08c",
        "svc_01",
        "svc_06b",               # 新增 custom_fields 因子也会入快照
    ):
        assert field in snapshot
    assert snapshot["kcr_01"] is None  # 未填报的 custom_fields 因子以 None 入快照


def test_unchanged_assessment_is_not_duplicated(db, customer):
    assert assessment_history.record_assessment(db, customer) is not None
    assert assessment_history.record_assessment(db, customer) is None
    assert db.query(AssessmentHistory).count() == 1


def test_overview_uses_latest_snapshot_without_live_rescoring(db, customer, monkeypatch):
    record = assessment_history.record_assessment(db, customer)
    expected_score = record.total_score

    class SnapshotOnlyStrategy:
        config = get_scoring_strategy().config

        def evaluate(self, _customer):
            raise AssertionError("已有快照的客户不应在 overview 中实时重算")

    monkeypatch.setattr("routers.assessment.get_scoring_strategy", lambda: SnapshotOnlyStrategy())
    result = get_overview(db)

    assert result.total_customers == 1
    assert result.avg_score == expected_score
    assert result.level_distribution[record.level] == 1


def test_level_filter_uses_latest_snapshot_and_database_pagination(db, customer):
    assessment_history.record_assessment(db, customer)
    other = Customer(customer_name="健康客户", industry="制造", custom_fields={})
    db.add(other)
    db.commit()
    db.refresh(other)
    assessment_history.record_assessment(db, other)
    latest_other = (
        db.query(AssessmentHistory)
        .filter(AssessmentHistory.customer_id == other.id)
        .order_by(AssessmentHistory.id.desc())
        .first()
    )
    latest_other.level = "健康"
    latest_other.total_score = 88
    db.commit()

    result = list_customers(search="", industry="", level="健康", page=1, page_size=1, db=db)

    assert result.total == 1
    assert [item.id for item in result.items] == [other.id]


def test_force_record_when_unchanged(db, customer):
    assessment_history.record_assessment(db, customer)
    assessment_history.record_assessment(db, customer, skip_if_unchanged=False)
    assert db.query(AssessmentHistory).count() == 2


def test_changed_factor_creates_new_record(db, customer):
    assessment_history.record_assessment(db, customer)
    customer.customer_satisfaction = 9
    db.commit()
    record = assessment_history.record_assessment(db, customer, trigger="factor_update")
    assert record is not None
    assert record.total_score > 12.5
    assert db.query(AssessmentHistory).count() == 2


# ── 趋势 ─────────────────────────────────────────────────────────────────


def test_trend_arrow_up_after_improvement(db, customer):
    assessment_history.record_assessment(db, customer)
    customer.customer_satisfaction = 9
    customer.payment_status = "正常"
    db.commit()
    assessment_history.record_assessment(db, customer)

    trend = assessment_history.build_trend(db, customer)
    assert trend.trend == "up"
    assert trend.delta > 0
    assert len(trend.points) == 2
    assert trend.points[0].total_score < trend.points[1].total_score
    # 快照时间戳统一为 UTC（database.utcnow），标签也应按 UTC 日期断言
    assert trend.points[0].label == datetime.datetime.now(datetime.UTC).strftime("%m-%d")
    assert "KCR 关键客户关系" in trend.points[0].dimensions


def test_trend_arrow_down_after_deterioration(db, customer):
    customer.customer_satisfaction = 9
    db.commit()
    assessment_history.record_assessment(db, customer)
    customer.customer_satisfaction = 2
    db.commit()
    assessment_history.record_assessment(db, customer)

    trend = assessment_history.build_trend(db, customer)
    assert trend.trend == "down"
    assert trend.delta < 0


def test_trend_flat_with_single_record(db, customer):
    assessment_history.record_assessment(db, customer)
    trend = assessment_history.build_trend(db, customer)
    assert trend.trend == "flat"
    assert trend.previous_score is None
    assert trend.delta == 0


def test_trend_without_history_falls_back_to_live_score(db, customer):
    trend = assessment_history.build_trend(db, customer)
    assert trend.points == []
    assert trend.latest_score == get_scoring_strategy().evaluate(customer).total_score
    assert trend.level == "高危"


def test_trend_level_lines_exclude_zero_threshold(db, customer):
    trend = assessment_history.build_trend(db, customer)
    assert [lv.name for lv in trend.level_lines] == ["健康", "亚健康", "风险"]
    assert [lv.min_score for lv in trend.level_lines] == [80, 60, 40]


def test_history_is_ordered_desc_and_limited(db, customer):
    for score in (4, 5, 6, 7):
        customer.customer_satisfaction = score
        db.commit()
        assessment_history.record_assessment(db, customer)

    records = assessment_history.list_history(db, customer.id, limit=2)
    assert len(records) == 2
    assert records[0].total_score > records[1].total_score


# ── 新增模型可用性────────────────────────────────────────────────


def test_chat_models_persist_and_cascade(db, customer):
    session = ChatSession(title="示例汽车集团 · AI 评估", customer_id=customer.id, scenario="evaluation")
    session.messages.append(ChatMessage(role="user", content="帮我评估示例汽车集团"))
    session.messages.append(
        ChatMessage(
            role="assistant",
            content="综合评估如下…",
            references=[{"item_id": 1, "title": "客户分级标准"}],
            strategy_items=[{"priority": "recommended", "title": "高层拜访"}],
            tokens_used=1280,
        )
    )
    db.add(session)
    db.commit()

    loaded = db.query(ChatSession).one()
    assert [m.role for m in loaded.messages] == ["user", "assistant"]
    assert loaded.messages[1].references[0]["title"] == "客户分级标准"
    assert loaded.messages[1].feedback == ""

    db.delete(loaded)
    db.commit()
    assert db.query(ChatMessage).count() == 0


def test_knowledge_chunk_metadata_field_is_usable(db):
    """`metadata` 是 SQLAlchemy 保留字，模型里映射为 chunk_metadata。"""
    from models import KnowledgeDocument

    doc = KnowledgeDocument(title="客户分级标准", category="内部规范")
    doc.chunks.append(
        KnowledgeChunk(content="S 级客户…", vector_id="vec-1", chunk_metadata={"industry": "金融"})
    )
    db.add(doc)
    db.commit()

    chunk = db.query(KnowledgeChunk).one()
    assert chunk.chunk_metadata["industry"] == "金融"
    assert chunk.document.title == "客户分级标准"


def test_deleting_customer_cascades_history(db, customer):
    assessment_history.record_assessment(db, customer)
    db.delete(customer)
    db.commit()
    assert db.query(AssessmentHistory).count() == 0
