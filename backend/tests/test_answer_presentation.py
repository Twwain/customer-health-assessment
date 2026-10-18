"""用户文案与内部核对数据分离，覆盖历史、流式、卡片和报告入口。"""
import json
from types import SimpleNamespace

import pytest

from models import ChatMessage
from services.ai import chat_engine, strategy
from services.ai.answer_presentation import (
    HISTORY_NOTICE, REVIEW_NOTICE, AnswerPresenter, ReadableOutputStream, clean_review_notes, present_answer,
)
from services.report_builder import build_report_data
from test_chat import (
    _reset_adapters, client, customer, db, engine, fake_llm, session_factory,
)


BODY = "战略协议升级（OR-01），CI-05当前3分，SVC-05提升至4-5次，RISK-08C需跟踪。"
DIAGNOSTIC = (
    "草稿精炼达到 2 次上限，输出可能未完全满足：事实核对：草稿『OR维度最低』；"
    "原文[scoring]『kcr：24/30；绝对缺口6\ner：15.75/18\n\nsvc：3/5』；排序错误。"
)
OLD_CONTENT = BODY + "\n\n> ⚠️ " + DIAGNOSTIC + "\n\n> 💡 本页面仅显示评估摘要，完整报告请点击「生成报告」。"
ITEM = {"priority": "recommended", "urgency": "high", "title": "改善OR-01",
        "reason": "CI-05当前3分", "action": "提升SVC-05至4-5次",
        "expected_outcome": "OR-01改善后预计总分+1.4", "reference": "资料OR-01规范"}


def assert_readable(text):
    assert "OR-01" not in text and "CI-05" not in text and "SVC-05" not in text
    assert "战略协议层级" in text and "价值量化与ROI呈现" in text
    assert "原文[scoring]" not in text and "精炼达到" not in text


def test_legacy_multiline_diagnostics_are_replaced_without_removing_user_notices():
    text = present_answer(OLD_CONTENT)
    assert_readable(text)
    assert REVIEW_NOTICE in text and "生成报告" in text
    assert "svc：3/5" not in text and "排序错误" not in text
    other = "> ⚠️ 生成中断：连接超时\n\n业务原文中提到事实核对。"
    assert clean_review_notes(other) == other
    assert clean_review_notes(text) == text


def test_factor_names_use_config_and_preserve_values_and_link_targets():
    presenter = AnswerPresenter()
    text = "CI-05 价值量化与ROI呈现；价值量化与ROI呈现（CI-05）；or_01=区域级；OR-010不替换。"
    result = presenter.text(text)
    assert result == "价值量化与ROI呈现；价值量化与ROI呈现；战略协议层级=区域级；OR-010不替换。"
    link = "[OR-01说明](https://example.com/OR-01) https://example.com/CI-05"
    result = presenter.text(link)
    assert result == "[战略协议层级说明](https://example.com/OR-01) https://example.com/CI-05"
    assert presenter.text(result) == result


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 200])
def test_factor_codes_split_across_chunks_remain_readable(chunk_size):
    stream = ReadableOutputStream()
    raw = BODY + " https://example.com/OR-01"
    pieces = [stream.feed(raw[i:i+chunk_size]) for i in range(0, len(raw), chunk_size)]
    pieces.append(stream.flush())
    result = "".join(pieces)
    assert_readable(result.split(" https://")[0])
    assert "4-5次" in result and "当前3分" in result
    assert result.endswith("https://example.com/OR-01")


def test_history_get_and_preview_are_readable_without_overwriting_originals(client, db):
    session = chat_engine.create_session(db)
    user = ChatMessage(session_id=session.id, role="user", content="CI-05是什么？")
    assistant = ChatMessage(session_id=session.id, role="assistant", content=OLD_CONTENT, strategy_items=[ITEM])
    db.add_all([user, assistant])
    db.commit()
    detail = client.get(f"/api/chat/sessions/{session.id}").json()
    assert detail['messages'][0]['content'] == user.content
    answer = detail['messages'][1]
    assert HISTORY_NOTICE in answer['content'] and BODY not in answer['content']
    assert answer['strategy_items'] == []
    assert "OR-01" not in client.get('/api/chat/sessions').json()['items'][0]['last_message']
    db.expire_all()
    assert db.get(ChatMessage, assistant.id).content == OLD_CONTENT
    history = chat_engine._history_messages(db, session)
    assert history[0].content == user.content
    assert "原文[scoring]" not in history[1].content


def test_chat_stream_warnings_done_and_saved_answer_share_readable_content(db, customer, fake_llm, monkeypatch):
    session = chat_engine.create_session(db, customer_id=customer.id)
    diagnostics = {"verification": [{"status": "issues", "issues": [{"claim": "OR-01", "reason": "排序错误"}]}]}

    def generate(*args, **kwargs):
        for char in BODY:
            kwargs['on_event']('delta', {'text': char})
        return SimpleNamespace(text=BODY + '\n```json\n' + json.dumps([ITEM], ensure_ascii=False) + '\n```',
            degraded=False, degraded_items=[], references=[], warnings=[DIAGNOSTIC, "关键事实核对未完成：证据无法定位"],
            timings=diagnostics)

    monkeypatch.setattr(chat_engine, 'generate', generate)
    events = list(chat_engine.run_turn(db, session, scenario='assessment'))
    warnings = [e.data['message'] for e in events if e.type == 'warning']
    assert warnings == [REVIEW_NOTICE]
    assert_readable(''.join(e.data['text'] for e in events if e.type == 'delta'))
    final = next(e.data for e in events if e.type == 'done')
    assert HISTORY_NOTICE in final['message']['content']
    assert final['timings']['verification'] == diagnostics['verification']  # 核对证据不被展示转换改写
    assert final['message']['strategy_items'] == []
    # 此模拟返回了违反新质量契约的gen；历史DTO仍不能展示它，原始记录保留。
    assert REVIEW_NOTICE in db.get(ChatMessage, final['message']['id']).content


def test_free_qa_chunked_codes_and_user_question_preserved(db, fake_llm):
    fake_llm.chunks = list(BODY)
    session = chat_engine.create_session(db)
    events = list(chat_engine.run_turn(db, session, content='CI-05是什么？', scenario='free_qa'))
    assert_readable(''.join(e.data['text'] for e in events if e.type == 'delta'))
    final = next(e.data['message'] for e in events if e.type == 'done')
    assert_readable(final['content'])
    assert db.query(ChatMessage).filter_by(role='user').one().content == 'CI-05是什么？'


def test_report_uses_readable_strategy_and_short_warning_but_keeps_verification(db, customer, fake_llm, monkeypatch):
    diagnostics = {'verification': [{'status': 'issues', 'issues': [{'claim': 'CI-05', 'evidence': '原文证据'}]}]}
    monkeypatch.setattr(strategy, 'generate', lambda *a, **k: SimpleNamespace(
        text='```json\n' + json.dumps([ITEM], ensure_ascii=False) + '\n```', degraded=False,
        degraded_items=[], references=[], warnings=[DIAGNOSTIC], timings=diagnostics,
    ))
    report = build_report_data(db, customer)
    assert report.warnings == [REVIEW_NOTICE]
    assert report.strategy_items[0]['reason'] == '价值量化与ROI呈现当前3分'
    assert report.timings == diagnostics and not report.degraded
