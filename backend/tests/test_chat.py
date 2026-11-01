"""AI 对话引擎测试。

覆盖共享传输适配器、安全护栏、会话接口、并发登记与指标。
当前生成合同、来源生命周期与再生成覆盖见 test_ai_protocol 系列。
"""

import json
import datetime
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from database import Base, get_db, migrate_add_chat_message_superseded_column, utcnow
from models import (
    AssessmentHistory, ChatMessage, ChatSession, ChatTurnMetric, Customer,
    KnowledgeChunk, KnowledgeDocument, KnowledgeItem,
)
from services import assessment_history
from services.ai import chat_engine, guardrails, llm_adapter
from services.ai.llm_adapter import LLMError, LLMUnavailableError
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

    def status(self):
        return {
            "provider": "fake",
            "model": self.model,
            "base_url": "http://fake",
            "available": self.available,
            "reason": "" if self.available else "测试用不可用适配器",
        }

    def chat_completion_once(self, messages, **kwargs):
        self.calls.append(list(messages))
        if self.error:
            raise self.error
        return llm_adapter.ChatResult(content="".join(self.chunks), raw_usage=self.usage)


@pytest.fixture(autouse=True)
def _reset_adapters(monkeypatch):
    # 本模块检验模拟会话；避免 .env 的生产 Chroma 路径影响隔离和超时断言。
    monkeypatch.setattr(config, "KNOWLEDGE_VECTOR_STORE", "memory")
    monkeypatch.setattr("services.rag.vector_store._MEMORY_STORE", InMemoryVectorStore())
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


# ══════════════════════════ 安全护栏 ════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected_hit",
    [
        ("数据库密码: Abc123456", "credential"),
        ("这是我的 key sk-abcdef1234567890", "api_key"),
        ("身份证 11010119900307721X", "id_card"),
        ("卡号 6222020200112345678", "bank_card"),
        ("联系电话 13812345678", "phone"),
        ("联系电话13812345678", "phone"),
        ("身份证11010119900307721X", "id_card"),
        ("银行卡6222020200112345678", "bank_card"),
        ("密钥sk-abcdef1234567890", "api_key"),
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


@pytest.mark.parametrize("credential", [
    '“demo-472”', '‘demo-472’', 'demo-472', '“甲乙”', '‘甲乙’',
    '“ab。cd”', '‘ab。cd’', '“a b c”',
])
def test_credential_redaction_stops_at_chinese_sentence_boundary(credential):
    raw = f"内部口令是{credential}。P1中断必须45分钟内响应，由交付经理牵头。"
    cleaned, hits = guardrails.sanitize_for_outbound(raw)
    assert "credential" in hits and credential.strip('“”‘’') not in cleaned
    assert "P1中断必须45分钟内响应，由交付经理牵头。" in cleaned


def test_session_system_prompt_is_limited_and_dlp_masked(db):
    session = chat_engine.create_session(
        db, system_prompt="密钥sk-abcdef1234567890；回答简洁"
    )
    assert "sk-abcdef1234567890" not in session.system_prompt
    assert "回答简洁" in session.system_prompt


# ══════════════════════════ 上下文注入 ══════════════════════════════════════


# ══════════════════════════ 策略解析与降级 ══════════════════════════════════


# ══════════════════════════ 会话与一轮对话 ══════════════════════════════════


def test_create_session_titles_from_customer(db, customer):
    session = chat_engine.create_session(db, customer_id=customer.id, scenario="assessment")
    assert session.title == "示例汽车集团 · AI 评估"
    assert session.customer_id == customer.id


def test_create_session_rejects_unknown_customer(db):
    with pytest.raises(ValueError):
        chat_engine.create_session(db, customer_id=9999)


# ══════════════════════════ 降级═══════════════════════════════════


# ══════════════════════════ 重新生成与反馈 ══════════════════════════════════


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


def test_chat_metric_observer_records_timeout_and_queue_watermark(db):
    from services.ai.metrics import TurnMetricObserver, summarize

    observer = TurnMetricObserver(session_id=99, scenario="assessment")
    observer.consume(chat_engine.TurnEvent("start", {
        "scenario": "assessment", "model": "fake-chat",
    }))
    observer.consume(chat_engine.TurnEvent("error", {
        "message": "AI 生成超过总时限，已取消",
        "timings": {
            "total_ms": 120_001,
            "event_queue_capacity": 128,
            "event_queue_high_watermark": 128,
            "llm_call_count": 1,
        },
    }))
    observer.persist(db)

    body = summarize(db, hours=1)
    assert body["overall"]["status_counts"] == {"timeout": 1}
    assert body["overall"]["timeout_rate"] == 1
    assert body["overall"]["queue_saturation_count"] == 1
    assert body["overall"]["latency_ms"]["max"] == 120_001


def test_send_empty_message_rejected(client, customer, fake_llm):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    assert client.post(f"/api/chat/sessions/{session_id}/messages", json={"content": " "}).status_code == 400


def test_evaluate_requires_customer(client, fake_llm):
    session_id = client.post("/api/chat/sessions", json={}).json()["id"]
    response = client.post(f"/api/chat/sessions/{session_id}/evaluate", json={"stream": False})
    assert response.status_code == 400


def test_streaming_on_unknown_session_emits_error(client, fake_llm):
    response = client.post("/api/chat/sessions/999/messages", json={"content": "hi"})
    assert response.status_code == 404


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
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    # adapter 先检查 BASE_URL 再检查 API Key，mock 一个有效地址让测试不依赖本机 .env
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    llm_adapter.reset_adapters()
    adapter = llm_adapter.get_chat_adapter()
    assert adapter.available is False
    assert "API Key" in adapter.unavailable_reason()
    with pytest.raises(LLMUnavailableError):
        adapter.chat_completion_once([llm_adapter.LLMMessage(role="user", content="hi")], max_tokens=128)


def test_adapter_disabled_by_switch(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    llm_adapter.reset_adapters()
    assert llm_adapter.get_chat_adapter().available is False
    assert llm_adapter.llm_status()["degraded"] is True


def test_estimate_tokens_counts_cjk():
    assert llm_adapter.estimate_tokens("中文四个字") == 5
    assert llm_adapter.estimate_tokens("abcd") == 1


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


def test_chat_once_buffers_sse_and_usage(monkeypatch):
    lines = [
        'data: {"choices": [{"delta": {"content": "客户"}}]}',
        "",
        'data: {"choices": [{"delta": {"content": "健康度"}}]}',
        'data: {"choices": [{"delta": {}, "finish_reason": "length"}]}',
        'data: {"choices": [], "usage": {"total_tokens": 42}}',
        "data: [DONE]",
    ]
    adapter, fake = _adapter(monkeypatch, [_FakeResponse(lines=lines)])
    result = adapter.chat_completion_once([{"role": "user", "content": "hi"}], max_tokens=128)
    assert result.content == "客户健康度"
    assert result.raw_usage["total_tokens"] == 42
    assert result.finish_reason == "length"
    assert len(fake.requests) == 1
    payload = fake.requests[0]["json"]
    assert payload["temperature"] == 0
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 128


def test_stream_cancel_event_actively_closes_blocking_http_response(monkeypatch):
    class BlockingResponse(_FakeResponse):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.closed = threading.Event()

        def iter_lines(self):
            self.started.set()
            assert self.closed.wait(2)
            raise RuntimeError("closed")
            yield  # pragma: no cover

        def close(self):
            self.closed.set()

    response = BlockingResponse()
    adapter, _ = _adapter(monkeypatch, [response], max_retries=0)
    cancelled = threading.Event()
    errors: list[BaseException] = []

    def consume():
        try:
            adapter.chat_completion_once(
                [{"role": "user", "content": "hi"}], max_tokens=128, cancel_event=cancelled
            )
        except BaseException as exc:  # 由主线程断言取消类型
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    assert response.started.wait(1)
    cancelled.set()
    worker.join(1)
    assert not worker.is_alive()
    assert response.closed.is_set()
    assert len(errors) == 1 and isinstance(errors[0], LLMError)


def test_stream_cancel_closes_request_before_response_headers(monkeypatch):
    """等待响应头期间也必须响应取消，而不是等 HTTP timeout。"""
    started = threading.Event()
    closed = threading.Event()

    class BlockingHeaderClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def stream(self, method, url, headers=None, json=None):
            started.set()
            assert closed.wait(2)
            raise RuntimeError("client closed before headers")

        def close(self):
            closed.set()

    monkeypatch.setattr(llm_adapter.httpx, "Client", BlockingHeaderClient)
    adapter = llm_adapter.CompatAdapter(
        name="test", base_url="https://api.test/v1", api_key="sk-test",
        model="test", max_retries=0,
    )
    cancelled = threading.Event()
    errors: list[BaseException] = []

    worker = threading.Thread(
        target=lambda: _consume_stream_errors(adapter, cancelled, errors)
    )
    worker.start()
    assert started.wait(1)
    cancelled.set()
    worker.join(1)
    assert not worker.is_alive()
    assert closed.is_set()
    assert len(errors) == 1 and isinstance(errors[0], LLMError)


def test_stream_cancel_does_not_wait_forever_for_stuck_reader(monkeypatch):
    """调用方有界返回，但底层线程退出前不得释放付费并发槽。"""
    started = threading.Event()
    release = threading.Event()

    class StuckClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def stream(self, method, url, headers=None, json=None):
            started.set()
            assert release.wait(3)
            raise RuntimeError("released")

        def close(self):
            pass

    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(llm_adapter, "_CHAT_LLM_SEM", semaphore)
    monkeypatch.setattr(llm_adapter.httpx, "Client", StuckClient)
    adapter = llm_adapter.CompatAdapter(
        name="test", base_url="https://api.test/v1", api_key="sk-test",
        model="test", max_retries=0,
    )
    cancelled = threading.Event()
    errors: list[BaseException] = []
    worker = threading.Thread(
        target=lambda: _consume_stream_errors(adapter, cancelled, errors)
    )
    worker.start()
    assert started.wait(1)
    cancelled.set()
    worker.join(1.5)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], LLMError)
    # caller 已返回，但 stuck reader 仍持有槽位，新付费请求不能绕过上限。
    assert semaphore.acquire(blocking=False) is False
    release.set()
    deadline = time.monotonic() + 1
    reacquired = False
    while time.monotonic() < deadline and not reacquired:
        reacquired = semaphore.acquire(blocking=False)
        if reacquired:
            break
        time.sleep(0.01)
    assert reacquired is True
    semaphore.release()


def _consume_stream_errors(adapter, cancelled, errors):
    try:
        adapter.chat_completion_once(
            [{"role": "user", "content": "hi"}], max_tokens=128, cancel_event=cancelled
        )
    except BaseException as exc:
        errors.append(exc)


@pytest.mark.parametrize("status,detail", [
    (503, "overloaded"), (401, "unauthorized"),
    (400, "unknown field stream_options"), (400, "unknown field thinking"),
])
def test_chat_once_failure_never_retries(monkeypatch, status, detail):
    adapter, fake = _adapter(monkeypatch, [_FakeResponse(status_code=status, text=detail)])
    with pytest.raises(LLMUnavailableError):
        adapter.chat_completion_once([{"role": "user", "content": "hi"}], max_tokens=128)
    assert len(fake.requests) == 1


def test_chat_once_discards_partial_output_on_transport_failure(monkeypatch):
    class BrokenResponse(_FakeResponse):
        def iter_lines(self):
            yield 'data: {"choices": [{"delta": {"content": "partial"}}]}'
            raise RuntimeError("connection lost")

    adapter, fake = _adapter(monkeypatch, [BrokenResponse()])
    with pytest.raises(LLMUnavailableError):
        adapter.chat_completion_once([{"role": "user", "content": "hi"}], max_tokens=128)
    assert len(fake.requests) == 1


def test_chat_once_deadline_closes_blocked_transport(monkeypatch):
    started, closed = threading.Event(), threading.Event()

    class BlockingClient:
        def __init__(self, timeout=None):
            pass

        def stream(self, *args, **kwargs):
            started.set()
            assert closed.wait(2)
            raise RuntimeError("closed")

        def close(self):
            closed.set()

    monkeypatch.setattr(llm_adapter.httpx, "Client", BlockingClient)
    adapter = llm_adapter.CompatAdapter(name="test", base_url="https://api.test/v1",
        api_key="sk-test", model="test")
    before = time.monotonic()
    with pytest.raises(LLMError, match="超过总时限"):
        adapter.chat_completion_once([{"role": "user", "content": "hi"}],
            max_tokens=128, deadline=before + 0.15)
    assert started.is_set() and closed.is_set()
    assert time.monotonic() - before < 1


def test_embedding_retries_transient_failure(monkeypatch):
    adapter, fake = _adapter(monkeypatch, [
        _FakeResponse(status_code=503, text="overloaded"),
        _FakeResponse(json_data={"data": [{"index": 0, "embedding": [0.1, 0.2]}]}),
    ])
    assert adapter.embed(["A"]) == [[0.1, 0.2]]
    assert len(fake.requests) == 2
    assert llm_adapter.get_embedding_usage()["http_attempts"] == 2


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


def test_embedding_removes_explicitly_unsupported_dimensions_once(monkeypatch):
    adapter, fake = _adapter(
        monkeypatch,
        [
            _FakeResponse(status_code=400, text="unsupported parameter: dimensions"),
            _FakeResponse(json_data={"data": [{"index": 0, "embedding": [0.1, 0.2]}]}),
            _FakeResponse(json_data={"data": [{"index": 0, "embedding": [0.3, 0.4]}]}),
        ],
    )

    assert adapter.embed(["A"], dimensions=2) == [[0.1, 0.2]]
    assert adapter.embed(["B"], dimensions=2) == [[0.3, 0.4]]
    assert fake.requests[0]["json"]["dimensions"] == 2
    assert "dimensions" not in fake.requests[1]["json"]
    assert "dimensions" not in fake.requests[2]["json"]


def test_embedding_outbound_dlp_masks_chinese_adjacent_sensitive_value():
    from services.rag.embeddings import make_embedding_func

    class StubEmbedding:
        def __init__(self):
            self.received = []

        def embed(self, texts):
            self.received.extend(texts)
            return [[1.0] * config.EMBEDDING_DIM for _ in texts]

    stub = StubEmbedding()
    llm_adapter.set_embedding_adapter(stub)
    vectors = make_embedding_func()(["联系电话13812345678"])
    assert len(vectors[0]) == config.EMBEDDING_DIM
    assert "13812345678" not in stub.received[0]
    assert "138****5678" in stub.received[0]


def test_chat_global_concurrency_semaphore(monkeypatch):
    monkeypatch.setattr(llm_adapter, "_CHAT_LLM_SEM", threading.BoundedSemaphore(2))
    active = peak = 0
    lock = threading.Lock()

    class Response(_FakeResponse):
        def iter_lines(self):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            yield 'data: {"choices": [{"delta": {"content": "ok"}}]}'
            with lock:
                active -= 1

    class Client:
        def __init__(self, timeout=None):
            pass

        def stream(self, *args, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(llm_adapter.httpx, "Client", Client)
    adapter = llm_adapter.CompatAdapter(name="test", base_url="https://api.test/v1",
        api_key="sk-test", model="test")
    results = []
    threads = [threading.Thread(target=lambda: results.append(adapter.chat_completion_once(
        [{"role": "user", "content": "hi"}], max_tokens=128))) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert peak == 2 and len(results) == 6
    assert all(result.content == "ok" for result in results)


def test_embedding_global_concurrency_semaphore(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(llm_adapter, "_EMBEDDING_SEM", threading.BoundedSemaphore(1))
    adapter = llm_adapter.CompatAdapter(
        name="test-provider",
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="test-model",
    )
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_post(path, payload):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"data": [{"index": 0, "embedding": [1.0]}]}

    monkeypatch.setattr(adapter, "_post_with_retry", fake_post)
    threads = [threading.Thread(target=adapter.embed, args=(["text"],)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1


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


def test_chat_metric_persist_removes_expired_rows(db, monkeypatch):
    monkeypatch.setattr(config, "CHAT_METRICS_RETENTION_DAYS", 30)
    expired = ChatTurnMetric(
        session_id=1,
        scenario="free_qa",
        created_at=utcnow() - datetime.timedelta(days=31),
    )
    db.add(expired)
    db.commit()
    db.expunge(expired)
    observer = chat_engine.chat_metrics.TurnMetricObserver(1, "free_qa")
    observer.persist(db)
    cutoff = utcnow() - datetime.timedelta(days=30)
    assert db.query(ChatTurnMetric).filter(ChatTurnMetric.created_at < cutoff).count() == 0


def test_chat_message_supersede_migration_adds_column_and_index(tmp_path):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-chat.db'}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, session_id INTEGER, role TEXT)"
        )
    assert migrate_add_chat_message_superseded_column(legacy_engine) == 1
    assert migrate_add_chat_message_superseded_column(legacy_engine) == 0
    columns = {
        column["name"]
        for column in __import__("sqlalchemy").inspect(legacy_engine).get_columns("chat_messages")
    }
    indexes = {
        index["name"]
        for index in __import__("sqlalchemy").inspect(legacy_engine).get_indexes("chat_messages")
    }
    assert "superseded_by_id" in columns
    assert "ix_chat_messages_superseded_by_id" in indexes


def test_chat_message_supersede_migration_repairs_plain_legacy_column(tmp_path):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'plain-supersede.db'}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE chat_sessions (id INTEGER NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql("""
            CREATE TABLE chat_messages (
                id INTEGER NOT NULL PRIMARY KEY,
                session_id INTEGER NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                "references" JSON NOT NULL,
                strategy_items JSON NOT NULL,
                tokens_used INTEGER NOT NULL,
                feedback VARCHAR(10) NOT NULL,
                degraded BOOLEAN NOT NULL,
                superseded_by_id INTEGER,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
            )
        """)
        connection.exec_driver_sql("INSERT INTO chat_sessions (id) VALUES (1)")
        connection.exec_driver_sql("""
            INSERT INTO chat_messages
                (id, session_id, role, content, "references", strategy_items,
                 tokens_used, feedback, degraded, superseded_by_id, created_at)
            VALUES (1, 1, 'assistant', 'old', '[]', '[]', 1, '', 0, NULL, CURRENT_TIMESTAMP),
                   (2, 1, 'assistant', 'new', '[]', '[]', 1, '', 0, NULL, CURRENT_TIMESTAMP)
        """)
        connection.exec_driver_sql(
            "UPDATE chat_messages SET superseded_by_id=2 WHERE id=1"
        )
    assert migrate_add_chat_message_superseded_column(legacy_engine) == 1
    with legacy_engine.connect() as connection:
        fks = connection.exec_driver_sql("PRAGMA foreign_key_list(chat_messages)").fetchall()
        rows = connection.exec_driver_sql(
            "SELECT id, content, superseded_by_id FROM chat_messages ORDER BY id"
        ).fetchall()
    assert any(
        row[2] == "chat_messages" and row[3] == "superseded_by_id"
        and str(row[6]).upper() == "SET NULL"
        for row in fks
    )
    assert rows == [(1, "old", 2), (2, "new", None)]
    assert migrate_add_chat_message_superseded_column(legacy_engine) == 0
