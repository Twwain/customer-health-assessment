"""真实接口冒烟测试：显式 --run-live，数据均为合成，不使用业务库。"""

import json
import math
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import config
from database import Base
from models import ChatMessage, Customer, KnowledgeChunk
from services.ai import chat_engine, llm_adapter
from services.ai.llm_adapter import LLMMessage
from services.rag import vector_store
from services.rag.embeddings import make_embedding_func
from services.rag.reranker import MetadataReranker
from services.rag.retriever import KnowledgeRetriever
from services.rag.vector_store import InMemoryVectorStore

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def isolated_adapters(monkeypatch):
    # 恢复单例，避免在线与离线测试相互污染；限制失败重试成本。
    monkeypatch.setattr(llm_adapter, "_chat_adapter", None)
    monkeypatch.setattr(llm_adapter, "_embedding_adapter", None)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)


@pytest.fixture
def live_chat():
    missing = [name for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL") if not getattr(config, name)]
    if missing or not config.LLM_ENABLED or config.LLM_API_KEY.startswith("enc:"):
        pytest.fail("在线 Chat 配置缺失、未解密或未启用：检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / LLM_ENABLED", pytrace=False)
    return llm_adapter.get_chat_adapter()


@pytest.fixture
def live_embed():
    missing = [name for name in ("EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL") if not getattr(config, name)]
    if missing or not config.EMBEDDING_ENABLED or config.EMBEDDING_API_KEY.startswith("enc:"):
        pytest.fail("在线 Embedding 配置缺失、未解密或未启用：检查 LLM_EMBEDDING_* / EMBEDDING_ENABLED", pytrace=False)
    return make_embedding_func()


@pytest.fixture
def limits(request):
    first = request.config.getoption("--live-first-token-seconds")
    total = request.config.getoption("--live-total-seconds")
    assert math.isfinite(first) and math.isfinite(total) and 0 < first <= total
    return first, total


def record(record_property, **metrics):
    for name, value in metrics.items():
        record_property(name, value)
    print("LIVE_METRICS " + json.dumps(metrics, ensure_ascii=False))


def test_live_structured_response_latency(live_chat, limits, record_property):
    total_limit = limits[1]
    started = time.monotonic()
    result = live_chat.chat_completion_once(
        [LLMMessage("user", '请用一句中文解释客户健康度，返回JSON对象，字段answer。')],
        max_tokens=128, deadline=started + total_limit,
    )
    elapsed = time.monotonic() - started
    record(record_property, total_seconds=elapsed)
    assert json.loads(result.content).get("answer"), "真实模型未返回正文"
    assert elapsed <= total_limit


def test_live_session_turn(live_chat, limits, monkeypatch, record_property):
    # 此项只测会话，不要求 Embedding Key；RAG 在独立用例验证。
    monkeypatch.setattr(config, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(config, "KNOWLEDGE_VECTOR_STORE", "memory")
    monkeypatch.setattr(vector_store, "_MEMORY_STORE", InMemoryVectorStore())
    monkeypatch.setattr(config, "CHAT_TURN_TIMEOUT_SECONDS", limits[1])
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            customer = Customer(customer_name="在线合成客户", custom_fields={})
            db.add(customer)
            db.commit()
            session = chat_engine.create_session(db, customer_id=customer.id, scenario="assessment")
            events = list(chat_engine.run_turn(db, session, content="请用一句话说明为什么要关注客户续约。"))
            assert not any(event.type in {"error", "warning"} for event in events), "会话发生错误或降级警告"
            assert events[-1].type == "done"
            done = events[-1].data
            assert done["degraded"] is False, "规则引擎降级不能算在线测试通过"
            assert done["message"]["content"].strip()
            timings = done["timings"]
            record(record_property, session_total_ms=done["latency_ms"])
            assert timings["llm_call_count"] >= 1
            assert done["latency_ms"] <= limits[1] * 1000
            assert db.query(ChatMessage).filter_by(session_id=session.id, role="assistant").count() == 1
    finally:
        engine.dispose()


@pytest.fixture
def live_retriever(live_embed):
    texts = [
        "回款制度：客户账款超过付款期限，应由财务负责人在36小时内联系客户确认付款计划。",
        "续约制度：合同到期前60天，由客户经理启动续签沟通。",
        "设备制度：服务器每周日凌晨进行备份，备份保留14天。",
        "未审核回款草稿：逾期款项一律等待999天再处理。",
    ]
    vectors = live_embed(texts)
    assert len(vectors) == len(texts)
    assert all(len(v) == config.EMBEDDING_DIM and all(math.isfinite(x) for x in v) and any(v) for v in vectors)
    store = InMemoryVectorStore()
    store.add(
        [f"{i}:0" for i in range(1, 5)], texts, vectors,
        [{"document_id": i, "item_id": i, "chunk_index": 0, "title": f"合成制度{i}",
          "category": "内部规范", "status": "canonical" if i < 4 else "proposed"} for i in range(1, 5)],
    )
    return KnowledgeRetriever(store=store, embed_func=live_embed, reranker=MetadataReranker())


@pytest.mark.parametrize("question,expected", [
    ("客户一直拖欠货款，谁应该在多久之内跟进？", 1),
    ("合约快到期了，提前多久开始商谈继续合作？", 2),
    ("系统数据副本能保存几个星期？", 3),
])
def test_live_rag_retrieval(live_retriever, question, expected, limits, record_property):
    started = time.monotonic()
    timings = {}
    hits = live_retriever.retrieve(question, top_k=1, window=0, timings=timings)
    elapsed = time.monotonic() - started
    record(record_property, retrieval_seconds=elapsed, **timings)
    assert hits, "真实 Embedding 检索失败或降级为空"
    assert hits[0].document_id == expected, "语义检索未命中预期知识"
    assert hits[0].metadata["status"] == "canonical"
    assert elapsed <= limits[1]


def test_live_rag_grounded_answer(live_chat, live_retriever):
    hits = live_retriever.retrieve("账款逾期由谁处理，时限是多少？", top_k=1, window=0)
    assert hits and hits[0].document_id == 1
    result = live_chat.chat_completion_once(
        [LLMMessage("system", '仅依据给定资料回答，输出纯JSON，字段为owner、hours（整数）、source_id（整数）。'),
         LLMMessage("user", f"资料 source_id={hits[0].document_id}：{hits[0].content}\n问题：账款逾期由谁处理，时限是多少？")],
        max_tokens=256,
    )
    answer = json.loads(result.content)
    assert answer["hours"] == 36
    assert "财务" in answer["owner"]
    assert answer["source_id"] == 1


@pytest.fixture
def live_business_data(live_embed, monkeypatch, tmp_path):
    """真实上传/索引合成知识，供正式 Agent、工具和报告共用。"""
    from models import Customer
    from seed_factors import GOOD_FACTORS
    from services.rag import knowledge_base, reranker

    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(config, "KNOWLEDGE_VECTOR_STORE", "memory")
    monkeypatch.setattr(vector_store, "_MEMORY_STORE", InMemoryVectorStore())
    monkeypatch.setattr(reranker, "get_reranker", lambda: MetadataReranker())
    # retriever 已导入工厂；固定本地重排算法，不引入额外模型下载。
    monkeypatch.setattr("services.rag.retriever.get_reranker", lambda: MetadataReranker())
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            customer = Customer(customer_name="在线测试合成客户", industry="制造",
                                custom_fields=dict(GOOD_FACTORS))
            db.add(customer)
            db.commit()
            kb = knowledge_base.KnowledgeBaseService(db, embed_func=live_embed)
            doc = kb.create_from_upload(
                title="合成回款制度", category="内部规范", filename="policy.txt",
                raw="账款逾期后，财务负责人应在36小时内完成单据核验，在72小时内取得付款排期。".encode(),
                status="canonical",
            )
            assert doc.index_status == "indexed"
            yield db, customer, doc
    finally:
        engine.dispose()


def test_live_report_ai(live_chat, live_business_data, record_property, tmp_path, monkeypatch):
    from services.report_builder import build_report_data
    from seed_factors import RISKY_FACTORS

    db, customer, doc = live_business_data
    # 引用验收需要制度实际适用：原夹具为满分、零合同的新客户，
    # 生成一般经营建议而不引用逾期制度符合生产契约。
    customer.custom_fields = dict(RISKY_FACTORS)
    customer.contract_amount = 100
    customer.cooperation_years = 3
    customer.notes = "当前已有一笔合同应收款逾期，尚未完成单据核验与付款排期确认。"
    db.commit()
    started = time.monotonic()
    report = build_report_data(db, customer, include_ai=True)
    evidence = tmp_path / "report-ai-evidence.json"
    evidence.write_text(json.dumps({
        "customer_notes": customer.notes,
        "strategy_items": report.strategy_items, "references": report.references,
        "warnings": report.warnings, "error": report.error, "timings": report.timings,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    record(record_property, report_seconds=time.monotonic() - started,
           report_llm_calls=len(report.timings.get("llm_calls", [])), report_artifact=str(evidence))
    assert report.has_ai and not report.degraded, report.error
    assert report.error is None, report.error
    assert report.strategy_items and all(i["title"] and i["action"] for i in report.strategy_items)
    for reference in report.references:
        chunk = db.get(KnowledgeChunk, reference["chunk_id"])
        assert chunk is not None and chunk.document_id == doc.id
    assert report.analysis["summary"]
    assert report.timings["llm_call_count"] >= 1
