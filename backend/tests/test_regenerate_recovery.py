"""重生成必须在成功提交后才替换旧答案；失败草稿不进入活动历史。"""

import time

import pytest

import config
from models import AssessmentHistory, ChatMessage, ChatTurnMetric
from services import assessment_history
from services.ai import chat_engine, llm_adapter
from services.ai.graph_builder import AgentResult
from services.ai.llm_adapter import LLMError, LLMUnavailableError
from test_chat import (
    FakeAdapter, _parse_sse, _reset_adapters, client, customer, db, engine,
    fake_llm, session_factory,
)


def _seed_answer(client, customer):
    session_id = client.post("/api/chat/sessions", json={"customer_id": customer.id}).json()["id"]
    reply = client.post(
        f"/api/chat/sessions/{session_id}/messages",
        json={"content": "原来的问题", "stream": False},
    ).json()["message"]
    return session_id, reply


class InterruptedAdapter(FakeAdapter):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    def stream_chat_completion(self, messages, **kwargs):
        self.calls.append(list(messages))
        if self.mode == "empty":
            yield "  \n"
            return
        if self.mode == "unavailable":
            raise LLMUnavailableError("连接不可用")
        yield "这是失败草稿内容，不能进入历史上下文。" * 3
        if self.mode == "runtime":
            raise RuntimeError("连接异常")
        if self.mode == "llm_error":
            raise LLMError("连接中断")
        if self.mode == "timeout":
            time.sleep(0.08)
            yield "已经超时的后续内容"
        if kwargs.get("on_usage"):
            kwargs["on_usage"]({
                "total_tokens": 100, "completion_tokens": 60,
                "finish_reason": "length" if self.mode == "length" else "stop",
            })


@pytest.mark.parametrize("mode", ["llm_error", "runtime", "unavailable", "empty", "timeout", "length"])
@pytest.mark.parametrize("stream", [True, False])
def test_failed_regeneration_preserves_db_http_history_and_next_context(
    client, customer, db, fake_llm, monkeypatch, mode, stream,
):
    session_id, old = _seed_answer(client, customer)
    llm_adapter.set_chat_adapter(InterruptedAdapter(mode))
    if mode == "timeout":
        monkeypatch.setattr(config, "CHAT_TURN_TIMEOUT_SECONDS", 0.04)
    response = client.post(f"/api/chat/sessions/{session_id}/regenerate", json={"stream": stream})
    assert response.status_code == 200
    if stream:
        events = _parse_sse(response.text)
        assert events[-1][0] == "error"
        assert not any(name == "done" for name, _ in events)
        assert events[-1][1]["retained_message_id"] == old["id"]
        assert "已保留原答案" in events[-1][1]["message"]
    else:
        assert response.json()["message"] is None
        assert "已保留原答案" in response.json()["error"]
    history = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
    assert [m["id"] for m in history if m["role"] == "assistant"] == [old["id"]]
    db.expire_all()
    assistants = db.query(ChatMessage).filter(ChatMessage.role == "assistant").all()
    assert len(assistants) == 1
    assert assistants[0].content == old["content"]
    assert assistants[0].superseded_by_id is None
    assert not chat_engine.is_session_streaming(session_id)

    monkeypatch.setattr(config, "CHAT_TURN_TIMEOUT_SECONDS", 120)
    next_adapter = FakeAdapter(chunks=["下一轮完整答案"])
    llm_adapter.set_chat_adapter(next_adapter)
    client.post(f"/api/chat/sessions/{session_id}/messages", json={"content": "继续交流", "stream": False})
    sent_history = [m.content for m in next_adapter.calls[0] if m.role == "assistant"]
    assert old["content"] in sent_history
    assert not any("失败草稿" in text for text in sent_history)


def test_client_cancellation_after_delta_preserves_old_answer_and_releases_session(
    client, customer, db, fake_llm,
):
    session_id, old = _seed_answer(client, customer)
    llm_adapter.set_chat_adapter(InterruptedAdapter("llm_error"))
    # 直接关闭真实 SSE 生成器，模拟客户端在收到部分文本后断开。
    stream = chat_engine.stream_sse(session_id, regenerate=True)
    saw_delta = False
    for event in stream:
        if "event: delta" in event:
            saw_delta = True
            break
    assert saw_delta
    stream.close()
    assert not chat_engine.is_session_streaming(session_id)
    db.expire_all()
    assert db.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 1
    assert db.get(ChatMessage, old["id"]).superseded_by_id is None
    assert db.query(ChatTurnMetric).order_by(ChatTurnMetric.id.desc()).first().status == "cancelled"
    history = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
    assert history[-1]["id"] == old["id"]


@pytest.mark.parametrize("mode", ["degraded", "exception", "truncated", "no_visible_content", "complete"])
def test_agent_regeneration_only_commits_complete_success(
    client, customer, db, fake_llm, monkeypatch, mode,
):
    session_id, old = _seed_answer(client, customer)

    def generate(*args, **kwargs):
        kwargs["on_event"]("delta", {"text": "新的策略草稿"})
        if mode == "exception":
            raise RuntimeError("策略生成失败")
        return AgentResult(
            text="```json\n{\n```" if mode == "no_visible_content" else "新的策略正文",
            references=[], degraded=mode == "degraded",
            degraded_items=[], warnings=[], iterations=1, model="fake",
            timings={"llm_calls": [
                {"phase": "refine", "finish_reason": "length" if mode == "truncated" else "stop"},
                {"phase": "verification", "finish_reason": "stop"},
            ]},
        )

    monkeypatch.setattr(chat_engine, "generate", generate)
    response = client.post(
        f"/api/chat/sessions/{session_id}/regenerate",
        json={"scenario": "strategy", "stream": True},
    )
    events = _parse_sse(response.text)
    db.expire_all()
    previous = db.get(ChatMessage, old["id"])
    active = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]
    if mode == "complete":
        assert events[-1][0] == "done"
        assert previous.superseded_by_id == active["id"]
        assert active["id"] != old["id"]
        assert "新的策略正文" in active["content"]
    else:
        assert events[-1][0] == "error"
        assert events[-1][1]["retained_message_id"] == old["id"]
        assert previous.superseded_by_id is None
        assert active["id"] == old["id"]


@pytest.mark.parametrize("cancel_after", ["strategy", "references", "done"])
def test_strategy_snapshot_commits_only_with_complete_answer(
    client, customer, db, fake_llm, monkeypatch, cancel_after,
):
    session_id, old = _seed_answer(client, customer)
    record = assessment_history.record_assessment(db, customer, strategy_snapshot=[{"title": "原有策略"}])
    record_id = record.id

    def generate(*args, **kwargs):
        return AgentResult(
            text='新的策略正文\n```json\n[{"title":"本次草稿策略","action":"联系客户"}]\n```',
            references=[{"id": 1, "title": "资料", "document_id": 1}],
            degraded=False, degraded_items=[], warnings=[], iterations=1, model="fake",
        )

    monkeypatch.setattr(chat_engine, "generate", generate)
    stream = chat_engine.stream_sse(session_id, scenario="strategy", regenerate=True)
    reached = False
    for event in stream:
        if f"event: {cancel_after}" in event:
            reached = True
            break
    assert reached
    stream.close()
    db.expire_all()
    if cancel_after == "done":
        assert db.get(AssessmentHistory, record_id).strategy_snapshot[0]["title"] == "本次草稿策略"
        assert db.get(ChatMessage, old["id"]).superseded_by_id is not None
        assert db.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 2
    else:
        assert db.get(AssessmentHistory, record_id).strategy_snapshot == [{"title": "原有策略"}]
        assert db.get(ChatMessage, old["id"]).superseded_by_id is None
        assert db.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 1
    assert not chat_engine.is_session_streaming(session_id)
