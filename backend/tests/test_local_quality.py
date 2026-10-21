"""One wrong conclusion must not erase unrelated verified analysis."""
import json
import threading
import time

import pytest

from services.ai import local_quality as quality
from services.ai.context_builder import ChatContext
from services.ai.fact_check import _draft_segments, parse_result
from services.ai.graph_builder import AssessmentStrategyAgent
from services.ai.llm_adapter import LLMError, LLMUnavailableError
from test_fact_check import ScriptedAdapter


SOURCE = "制度规定36小时内核验。客户已有高层支持，建议继续高层沟通。"
SOURCES = {"knowledge": SOURCE}
BAD = "制度规定72小时内核验。"
GOOD = "制度规定36小时内核验。"
SAFE = "建议继续高层沟通。"
CLEAN = '{"issues":[],"score_projections":[]}'
ISSUE = json.dumps({"issues": [{"claim": BAD, "source_id": "knowledge",
                               "evidence": "制度规定36小时内核验。", "reason": "核验时限应为36小时"}],
                    "score_projections": []}, ensure_ascii=False)


def anchors():
    return [{"source_id": "knowledge", "evidence": SOURCE}]


def review(answer, *, remove=(), dependencies=None):
    dependencies = dependencies or {}
    return json.dumps({"units": [{"id": unit.id, "decision": "remove" if unit.id in remove else "keep",
                                   "depends_on": dependencies.get(unit.id, []), "sources": anchors()}
                                  for unit in answer.units]}, ensure_ascii=False)


def patch(answer, replacements, *, dependencies=None):
    dependencies = dependencies or {}
    return json.dumps({"dependencies": [{"id": unit.id, "depends_on": dependencies.get(unit.id, [])}
                                         for unit in answer.units],
                       "patches": [{"id": key, **value, "sources": anchors()}
                                   for key, value in replacements.items()]}, ensure_ascii=False)


def run(replies, *, max_iterations=1, token_budget=10000, ctx=None):
    adapter = ScriptedAdapter(replies)
    events = []
    agent = AssessmentStrategyAgent(adapter=adapter, max_iterations=max_iterations, tools_enabled=False,
                                    token_budget=token_budget,
                                    on_event=lambda event, data: events.append((event, data)))
    result = agent.run("assessment", ctx or ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False)
    return result, adapter, events


def test_paragraphs_lists_and_raw_strategy_objects_have_real_independent_spans():
    item = {"title": "沟通", "action": SAFE, "score_targets": {"kcr_01": "100%"}}
    draft = "## 关系\n\n- 第一项\n  续行\n- 第二项\n\n```json\n" + json.dumps({"strategies": [item]}, ensure_ascii=False) + "\n```"
    answer = quality.parse_answer(draft)
    assert [unit.kind for unit in answer.units] == ["heading", "markdown", "markdown", "strategy"]
    assert "续行" in answer.units[1].text and "第二项" not in answer.units[1].text
    for unit in answer.units:
        assert draft[unit.start:unit.end].strip() == unit.text
    assert answer.units[-1].item == item
    rendered = answer.render()
    assert '"score_targets"' in rendered and "100%" in rendered


def test_correct_independent_analysis_survives_a_known_wrong_conclusion():
    draft = BAD + "\n\n" + SAFE
    answer = quality.parse_answer(draft)
    result, adapter, events = run([draft, ISSUE, review(answer), CLEAN])
    assert not result.degraded and BAD not in result.text and SAFE in result.text
    assert result.timings["answer_source"] == "ai_with_local_repairs"
    assert len(adapter.calls) == 4
    assert [call["phase"] for call in result.timings["llm_calls"]] == ["reason", "verification", "unit_review", "verification"]
    assert result.timings["llm_calls"][2]["thinking_enabled"] is False
    assert any(event == "replace" and data["reason"] == "local_quality_repair" for event, data in events)
    assert all('"decision"' not in data.get("text", "") for _, data in events)


def test_selection_forces_known_error_and_transitive_dependencies_out():
    draft = BAD + "\n\n因此应将核验作为唯一首要任务。\n\n预计该首要任务消除全部风险。\n\n" + SAFE
    answer = quality.parse_answer(draft)
    selected, diagnosis = quality.apply_review(
        review(answer, dependencies={"u1": ["u0"], "u2": ["u1"]}), answer, {"u0"}, SOURCES)
    assert selected == SAFE
    assert diagnosis["removed"] == ["u0", "u1", "u2"]


def test_partial_removal_always_discards_old_overall_summary():
    draft = BAD + "\n\n综上，客户所有风险均可立即消除。\n\n" + SAFE
    answer = quality.parse_answer(draft)
    selected, _ = quality.apply_review(review(answer), answer, {"u0"}, SOURCES)
    assert selected == SAFE and "所有风险" not in selected


def test_specific_conclusion_heading_does_not_turn_independent_paragraphs_into_global_summary():
    draft = BAD + "\n\n## 关系结论\n\n" + SAFE
    answer = quality.parse_answer(draft)
    selected, _ = quality.apply_review(review(answer), answer, {"u0"}, SOURCES)
    assert "## 关系结论" in selected and SAFE in selected
    assert not answer.units[-1].summary


def test_targeted_patch_keeps_unaffected_units_identical_and_removes_old_summary():
    draft = BAD + "\n\n总体结论：核验已超时。\n\n" + SAFE
    answer = quality.parse_answer(draft)
    proposal = patch(answer, {"u0": {"text": GOOD}})
    result, adapter, _ = run([draft, ISSUE, proposal, CLEAN], max_iterations=2)
    assert not result.degraded and GOOD in result.text and SAFE in result.text
    assert "核验已超时" not in result.text and BAD not in result.text
    assert len(adapter.calls) == 4 and result.timings["local_repairs"][0]["patched"] == ["u0"]


def test_failed_patch_preserves_original_answer_for_independent_selection():
    draft = BAD + "\n\n" + SAFE
    answer = quality.parse_answer(draft)
    result, adapter, _ = run([draft, ISSUE, LLMUnavailableError("修订超时"), review(answer), CLEAN], max_iterations=2)
    assert not result.degraded and result.text == SAFE and len(adapter.calls) == 5
    assert result.timings["local_repairs"][0]["status"] == "failed"


@pytest.mark.parametrize("replacement", [BAD, BAD + "请按此执行。"])
def test_patch_cannot_keep_known_wrong_quote_even_if_next_model_might_pass(replacement):
    draft = BAD + "\n\n" + SAFE
    answer = quality.parse_answer(draft)
    result, adapter, _ = run([draft, ISSUE, patch(answer, {"u0": {"text": replacement}}), review(answer), CLEAN],
                             max_iterations=2)
    assert not result.degraded and result.text == SAFE
    assert len(adapter.calls) == 5
    assert "已确认错误" in result.timings["local_repairs"][0]["reason"]
    assert [item["status"] for item in result.timings["verification"]] == ["issues", "passed"]


def test_incomplete_fact_check_can_recover_only_after_selection_and_full_verification():
    result, adapter, _ = run([SAFE, '{"issues":', '{"issues":', review(quality.parse_answer(SAFE)), CLEAN])
    assert not result.degraded and result.text == SAFE
    assert [item["status"] for item in result.timings["verification"]] == ["unverified", "unverified", "passed"]
    assert len(adapter.calls) == 5


def test_all_wrong_or_dependent_content_uses_whole_answer_fallback():
    answer = quality.parse_answer(BAD)
    result, adapter, _ = run([BAD, ISSUE, review(answer)])
    assert result.degraded and BAD not in result.text
    assert len(adapter.calls) == 3 and result.timings["answer_source"] == "rules_after_quality_check"


def test_new_problem_in_final_retained_answer_never_leaks():
    draft = BAD + "\n\n" + SAFE
    answer = quality.parse_answer(draft)
    final_issue = json.dumps({"issues": [{"claim": SAFE, "source_id": "knowledge", "evidence": SOURCE,
                                         "reason": "该建议仍依赖此前错误的优先级"}], "score_projections": []}, ensure_ascii=False)
    result, adapter, _ = run([draft, ISSUE, review(answer), final_issue, LLMUnavailableError("最终修订不可用")])
    assert result.degraded and BAD not in result.text and SAFE not in result.text
    assert len(adapter.calls) == 5 and result.timings['final_repairs'] == 1
    assert result.timings["local_repairs"][-1]["operation"] == "final_check"


def test_final_check_exception_never_marks_selected_content_verified():
    draft = BAD + "\n\n" + SAFE
    result, adapter, _ = run([draft, ISSUE, review(quality.parse_answer(draft)),
                              LLMUnavailableError("核对不可用"), LLMUnavailableError("核对仍不可用")])
    assert result.degraded and SAFE not in result.text and len(adapter.calls) == 5
    assert result.timings["verification"][-1]["status"] == "unverified"


@pytest.mark.parametrize("mutate", [
    lambda p: p["units"].append(p["units"][0]),
    lambda p: p["units"].pop(),
    lambda p: p["units"][1].update(id="unknown"),
    lambda p: p["units"][1].update(depends_on=["unknown"]),
    lambda p: p["units"][1].update(depends_on=["u1"]),
    lambda p: p["units"][1].update(depends_on=["u0", "u0"]),
    lambda p: p["units"][1].update(text="新生成的业务结论"),
])
def test_review_protocol_rejects_unknown_duplicate_incomplete_unsupported_or_new_text(mutate):
    answer = quality.parse_answer(BAD + "\n\n" + SAFE)
    payload = json.loads(review(answer))
    mutate(payload)
    with pytest.raises(ValueError):
        quality.apply_review(json.dumps(payload, ensure_ascii=False), answer, {"u0"}, SOURCES)


@pytest.mark.parametrize("unsupported", [[], [{"source_id": "unknown", "evidence": SOURCE}],
                                          [{"source_id": "knowledge", "evidence": "虚构原文"}]])
def test_unsupported_unit_and_dependants_are_removed_without_losing_independent_analysis(unsupported):
    answer = quality.parse_answer(BAD + "\n\n依赖前述结论的建议。\n\n" + SAFE)
    payload = json.loads(review(answer, dependencies={"u1": ["u0"]}))
    payload["units"][0]["sources"] = unsupported
    selected, diagnostic = quality.apply_review(json.dumps(payload, ensure_ascii=False), answer, set(), SOURCES)
    assert selected == SAFE
    assert diagnostic["removed"] == ["u0", "u1"]
    assert set(diagnostic["unsupported"]) == {"u0"}


def test_fixed_group_label_requires_no_evidence_but_bold_claim_does():
    answer = quality.parse_answer("**已确认的成因（数据可直接支撑）**\n\n**客户不存在风险，总分100分。**\n\n" + SAFE)
    assert [u.kind for u in answer.units] == ["label", "markdown", "markdown"]
    payload = json.loads(review(answer))
    for entry in payload["units"][:2]:
        entry["sources"] = []
    selected, diagnostic = quality.apply_review(json.dumps(payload, ensure_ascii=False), answer, set(), SOURCES)
    assert "已确认的成因" in selected and SAFE in selected and "100分" not in selected
    assert diagnostic["removed"] == ["u1"]


def test_labels_alone_never_count_as_reliable_analysis():
    answer = quality.parse_answer("**已确认的成因**\n\n" + BAD)
    with pytest.raises(ValueError, match="没有可靠"):
        quality.apply_review(review(answer), answer, {"u1"}, SOURCES)


def test_realistic_selection_with_empty_intro_evidence_survives_full_verification():
    draft = BAD + "\n\n**已确认的成因**\n\n按紧急度排序，责任角色默认客户经理，需系统/交付侧配合：\n\n" + SAFE
    answer = quality.parse_answer(draft)
    payload = json.loads(review(answer))
    payload["units"][1]["sources"] = []
    payload["units"][2]["sources"] = []
    result, _, _ = run([draft, ISSUE, json.dumps(payload, ensure_ascii=False), CLEAN])
    assert not result.degraded and SAFE in result.text and BAD not in result.text
    assert "责任角色" not in result.text
    assert result.timings["verification"][-1]["status"] == "passed"


def test_duplicate_json_fields_are_not_silently_overwritten():
    answer = quality.parse_answer(SAFE)
    response = review(answer).replace('"decision": "keep"', '"decision":"remove","decision":"keep"')
    with pytest.raises(ValueError, match="重复"):
        quality.apply_review(response, answer, set(), SOURCES)


def test_unrelated_or_duplicate_patch_is_rejected_and_original_remains_available():
    draft = BAD + "\n\n" + SAFE
    answer = quality.parse_answer(draft)
    for changes in ({"u1": {"text": "随意改写无关内容"}}, {"u0": {"text": GOOD}, "u1": {"text": "改写"}}):
        with pytest.raises(ValueError):
            quality.apply_patches(patch(answer, changes), answer, {"u0"}, SOURCES)
    assert answer.draft == draft


@pytest.mark.parametrize("replacement", [
    GOOD + "\n\n额外生成的独立结论。",
    GOOD + "\n## 新章节\n新的分析。",
    "## 将正文替换成新标题",
    "- " + GOOD + "\n- 新增策略条目",
])
def test_markdown_patch_cannot_smuggle_a_whole_answer_or_new_units_into_one_id(replacement):
    answer = quality.parse_answer(BAD + "\n\n" + SAFE)
    with pytest.raises(ValueError, match="单个同类"):
        quality.apply_patches(patch(answer, {"u0": {"text": replacement}}), answer, {"u0"}, SOURCES)


def test_existing_multiline_list_item_can_be_corrected_without_creating_an_extra_unit():
    answer = quality.parse_answer("- " + BAD + "\n  保留原来的执行说明。\n\n" + SAFE)
    corrected = "- " + GOOD + "\n  保留原来的执行说明。"
    rendered, _ = quality.apply_patches(patch(answer, {"u0": {"text": corrected}}), answer, {"u0"}, SOURCES)
    assert corrected in rendered and SAFE in rendered
    assert len(quality.parse_answer(rendered).units) == 2


def test_raw_card_target_map_survives_patch_and_partial_selection():
    bad = {"title": "核验", "action": BAD, "score_targets": {"svc_05": "≥6次"}}
    safe = {"title": "高层沟通", "action": SAFE, "score_targets": {"kcr_01": "100%"}}
    draft = '```json\n' + json.dumps({"strategies": [bad, safe]}, ensure_ascii=False) + '\n```'
    answer = quality.parse_answer(draft)
    corrected = {**bad, "action": GOOD}
    result, _ = quality.apply_patches(patch(answer, {"u0": {"item": corrected}}), answer, {"u0"}, SOURCES)
    parsed = quality.parse_answer(result)
    assert [u.item for u in parsed.units] == [corrected, safe]
    selected, _ = quality.apply_review(review(answer), answer, {"u0"}, SOURCES)
    assert quality.parse_answer(selected).units[0].item == safe
    with pytest.raises(ValueError, match="丢失"):
        quality.apply_patches(patch(answer, {"u0": {"item": {"title": "核验", "action": GOOD}}}), answer, {"u0"}, SOURCES)


def test_incomplete_score_targets_are_removed_locally_while_complete_card_is_verified():
    from test_fact_check import _score_case
    from test_verification_quality import structured_strategy
    customer, facts, _ = _score_case()
    incomplete = structured_strategy(changes={"kcr_01": "100%"})
    incomplete["title"] = "不完整目标的推算"
    complete = structured_strategy()
    complete["title"] = "完整目标的正确推算"
    draft = '```json\n' + json.dumps({"strategies": [incomplete, complete]}, ensure_ascii=False) + '\n```'
    answer = quality.parse_answer(draft)
    result, adapter, _ = run([draft, CLEAN, review(answer), CLEAN],
                             ctx=ChatContext(customer=customer, scoring_facts=facts, knowledge_text=SOURCE))
    assert not result.degraded and "不完整目标的推算" not in result.text
    retained = quality.parse_answer(result.text)
    assert [unit.item for unit in retained.units] == [complete]
    checks = next(item for item in result.timings["local_repairs"] if item["operation"] == "local_unit_checks")
    assert checks["units"][0]["id"] == "u0" and checks["units"][0]["status"] == "unverified"
    assert '"unverified_units"' in adapter.calls[2][0][-1].content
    assert [item["status"] for item in result.timings["verification"]] == ["unverified", "passed"]


def test_unverifiable_markdown_score_projection_does_not_erase_independent_complete_card():
    from test_fact_check import _score_case
    from test_verification_quality import structured_strategy
    customer, facts, _ = _score_case()
    complete = structured_strategy()
    draft = '总分由78.9提升至83.9。\n\n```json\n' + json.dumps({"strategies": [complete]}, ensure_ascii=False) + '\n```'
    answer = quality.parse_answer(draft)
    result, _, _ = run([draft, CLEAN, review(answer), CLEAN],
                        ctx=ChatContext(customer=customer, scoring_facts=facts, knowledge_text=SOURCE))
    assert not result.degraded
    retained = quality.parse_answer(result.text)
    assert len(retained.units) == 1 and retained.units[0].item == complete


def test_compact_multi_card_json_check_quotes_never_cross_card_boundary():
    bad = {"title": "核验", "action": BAD}
    safe = {"title": "高层沟通", "action": SAFE}
    draft = '```json\n' + json.dumps({"strategies": [bad, safe]}, ensure_ascii=False) + '\n```'
    segments = _draft_segments(draft)
    assert len(segments) == 2 and BAD in segments["0"] and SAFE not in segments["0"]
    payload = {"issues": [{"claim_id": "0", "source_id": "knowledge", "evidence_id": "0", "reason": "时限错"}],
               "score_projections": []}
    check = parse_result(json.dumps(payload), draft, SOURCES)
    answer = quality.parse_answer(draft)
    assert answer.locate(check.issues) == {"u0"}
    selected, _ = quality.apply_review(review(answer), answer, answer.locate(check.issues), SOURCES)
    assert quality.parse_answer(selected).units[0].item == safe


def test_claim_spanning_units_or_repeated_in_body_and_cards_is_conservatively_mapped():
    draft = BAD + "\n\n" + BAD + "\n\n" + SAFE
    answer = quality.parse_answer(draft)
    assert answer.locate([{"claim": BAD}]) == {"u0", "u1"}
    assert answer.locate([{"claim": draft[:draft.index(SAFE)]}]) == {"u0", "u1"}
    with pytest.raises(ValueError):
        answer.locate([{"claim": "找不到的引用"}])


def test_exhausted_budget_and_oversized_draft_do_not_start_extra_review(monkeypatch):
    result, adapter, _ = run([BAD, ISSUE], token_budget=100)
    assert result.degraded and len(adapter.calls) <= 2
    import config
    monkeypatch.setattr(config, "AI_FACT_CHECK_MAX_DRAFT_CHARS", 3)
    result, adapter, _ = run([SAFE])
    assert result.degraded and len(adapter.calls) == 1
    assert any("长度" in entry.get("reason", "") for entry in result.timings["local_repairs"])


def test_quality_call_honors_cancellation_and_deadline_without_spending_more_calls():
    event = threading.Event()
    adapter = ScriptedAdapter([])
    agent = AssessmentStrategyAgent(adapter=adapter, cancel_event=event)
    event.set()
    with pytest.raises(LLMError):
        agent._quality_call([], phase="unit_review")
    assert not adapter.calls
    event.clear()
    agent._deadline = time.monotonic() - 1
    with pytest.raises(LLMError):
        agent._quality_call([], phase="unit_review")
    assert not adapter.calls


def test_cycle_that_depends_on_wrong_unit_is_removed_transitively():
    answer = quality.parse_answer(BAD + "\n\n间接收益。\n\n优先级结论。\n\n" + SAFE)
    dependencies = {"u1": ["u0", "u2"], "u2": ["u1"]}
    selected, _ = quality.apply_review(review(answer, dependencies=dependencies), answer, {"u0"}, SOURCES)
    assert selected == SAFE


@pytest.mark.parametrize("subject", ["在", "位于", "的是"])
def test_correct_postposed_ranking_summary_survives_local_selection_of_unrelated_bad_card(subject):
    from test_fact_check import _score_case
    customer, facts, _ = _score_case()
    overview = f"绝对缺口最大在KCR（6分），得分率最低{subject}SVC（60%）。"
    wrong = {"title": "核验", "action": BAD, "reference": ""}
    draft = overview + '\n\n```json\n' + json.dumps({"strategies": [wrong]}, ensure_ascii=False) + '\n```'
    answer = quality.parse_answer(draft)
    result, adapter, _ = run([draft, ISSUE, review(answer), CLEAN],
                             ctx=ChatContext(customer=customer, scoring_facts=facts, knowledge_text=SOURCE))
    assert not result.degraded and result.text == overview
    assert BAD not in result.text and len(adapter.calls) == 4
    assert result.timings["local_repairs"][-1]["removed"] == ["u1"]
    assert [item["status"] for item in result.timings["verification"]] == ["issues", "passed"]
