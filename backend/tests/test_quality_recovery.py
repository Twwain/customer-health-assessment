"""Recover checker/format failures without accepting unsupported business content."""
import json

import pytest
from sqlalchemy import create_engine, inspect

from database import migrate_add_quality_diagnostics
from services.ai import local_quality
from services.ai.context_builder import ChatContext, build_alert_context
from services.ai.graph_builder import AssessmentStrategyAgent
from services.ai.metrics import quality_diagnostics
from services.ai.score_preparation import prepare_strategy_scores
from services.ai.strategy import build_degraded_strategies
from test_fact_check import ScriptedAdapter, _score_case
from test_local_quality import BAD, GOOD, SAFE, SOURCE, ISSUE, CLEAN, patch, review
from test_verification_quality import structured_strategy


def run(replies, *, rounds=3):
    adapter = ScriptedAdapter(replies)
    agent = AssessmentStrategyAgent(adapter=adapter, tools_enabled=False, max_iterations=rounds)
    return agent.run("assessment", ChatContext(knowledge_text=SOURCE), None, None, retrieve_enabled=False), adapter


def test_second_business_repair_is_followed_by_full_check_and_keeps_unrelated_text():
    middle = "制度规定48小时内核验。"
    initial = BAD + "\n\n" + SAFE
    answer = local_quality.parse_answer(initial)
    second = middle + "\n\n" + SAFE
    issue2 = json.dumps({"issues": [{"claim": middle, "source_id": "knowledge", "evidence": SOURCE,
                                    "reason": "时限应为36小时"}]}, ensure_ascii=False)
    result, adapter = run([initial, ISSUE, patch(answer, {"u0": {"text": middle}}), issue2,
                           patch(local_quality.parse_answer(second), {"u0": {"text": GOOD}}), CLEAN])
    assert not result.degraded and GOOD in result.text and SAFE in result.text
    assert result.timings["refine_rounds"] == 2 and len(adapter.calls) == 6
    assert [c["status"] for c in result.timings["verification"]] == ["issues", "issues", "passed"]


def test_malformed_checker_response_retries_checker_without_business_rewrite():
    result, adapter = run([SAFE, '{"issues":', CLEAN])
    assert not result.degraded and result.text == SAFE
    assert result.timings["refine_rounds"] == 0 and len(adapter.calls) == 3
    assert result.timings["verification"][-1]["recheck"]


def test_redundant_review_kind_is_repaired_without_new_model_call():
    answer = local_quality.parse_answer(BAD + "\n\n" + SAFE)
    payload = json.loads(review(answer))
    for row in payload["units"]:
        row["kind"] = "markdown"
    result, adapter = run([answer.draft, ISSUE, json.dumps(payload), CLEAN], rounds=1)
    assert not result.degraded and result.text == SAFE and len(adapter.calls) == 4
    assert any(r["operation"] == "format_repair" for r in result.timings["local_repairs"])


def test_mismatched_review_kind_cannot_be_silently_removed():
    answer = local_quality.parse_answer(SAFE)
    payload = json.loads(review(answer))
    payload["units"][0]["kind"] = "strategy"
    with pytest.raises(ValueError, match="类型"):
        local_quality.repair_review_format(json.dumps(payload), answer)


def test_model_repairs_malformed_patch_without_spending_business_round():
    answer = local_quality.parse_answer(BAD + "\n\n" + SAFE)
    fixed = patch(answer, {"u0": {"text": GOOD}})
    result, adapter = run([answer.draft, ISSUE, '{"dependencies":', fixed, CLEAN])
    assert not result.degraded and GOOD in result.text and SAFE in result.text
    assert result.timings["refine_rounds"] == 1 and len(adapter.calls) == 5
    assert sum(c["phase"] == "format_repair" for c in result.timings["llm_calls"]) == 1


def test_format_repair_never_allows_new_business_text_in_review():
    answer = local_quality.parse_answer(SAFE)
    response = json.loads(review(answer))
    response['units'][0]['note'] = "仅为多余字段"
    changed = json.loads(review(answer))
    changed['units'][0]['sources'][0]['evidence'] = GOOD
    agent = AssessmentStrategyAgent(adapter=ScriptedAdapter([json.dumps(changed)]))
    with pytest.raises(ValueError, match="业务内容"):
        agent._apply_quality(json.dumps(response), [],
            lambda value: local_quality.apply_review(value, answer, set(), {"knowledge": SOURCE}),
            phase="unit_review", answer=answer)


@pytest.mark.parametrize("phase", ["unit_review", "local_repair"])
def test_model_can_remove_extra_protocol_fields_without_changing_semantics(phase):
    answer = local_quality.parse_answer(BAD + "\n\n" + SAFE)
    if phase == "unit_review":
        corrected = review(answer, remove={"u0"})
        apply = lambda value: local_quality.apply_review(value, answer, {"u0"}, {"knowledge": SOURCE})
        key = "units"
    else:
        corrected = patch(answer, {"u0": {"text": GOOD}})
        apply = lambda value: local_quality.apply_patches(value, answer, {"u0"}, {"knowledge": SOURCE})
        key = "patches"
    response = json.loads(corrected)
    response['note'] = "多余的顶层字段"
    response[key][0]['note'] = "多余的条目字段"
    adapter = ScriptedAdapter([corrected])
    agent = AssessmentStrategyAgent(adapter=adapter)
    text, _ = agent._apply_quality(json.dumps(response), [], apply, phase=phase, answer=answer)
    assert SAFE in text and BAD not in text and len(adapter.calls) == 1
    assert agent._refine_rounds == 0


@pytest.mark.parametrize("field,value", [("id", "u1"), ("decision", "remove"),
    ("depends_on", ["u1"]), ("sources", [{"source_id": "knowledge", "evidence": GOOD}])])
def test_parseable_review_format_repair_preserves_all_consumed_fields(field, value):
    answer = local_quality.parse_answer(SAFE + "\n\n" + GOOD)
    original = json.loads(review(answer))
    original['note'] = "多余字段"
    changed = json.loads(review(answer))
    changed['units'][0][field] = value
    agent = AssessmentStrategyAgent(adapter=ScriptedAdapter([json.dumps(changed)]))
    with pytest.raises(ValueError, match="业务内容"):
        agent._apply_quality(json.dumps(original), [],
            lambda text: local_quality.apply_review(text, answer, set(), {"knowledge": SOURCE}),
            phase="unit_review", answer=answer)


def test_engine_corrects_gain_but_does_not_rewrite_business_actions():
    customer, facts, sources = _score_case()
    item = structured_strategy(outcome="改善经营质量，总增分约99分。")
    original_action = item["action"]
    draft = '```json\n' + json.dumps({"strategies": [item]}, ensure_ascii=False) + '\n```'
    corrected, repairs = prepare_strategy_scores(draft, customer, facts)
    parsed = local_quality.parse_answer(corrected).units[0].item
    assert parsed["action"] == original_action
    assert "总增分约5.0分" in parsed["expected_outcome"] and repairs
    from services.ai.fact_check import parse_result
    assert parse_result(CLEAN, corrected, sources, customer=customer, scoring_facts=facts).status == "passed"


def test_engine_never_uses_incomplete_or_conflicting_target_map():
    customer, facts, _ = _score_case()
    item = structured_strategy(changes={"kcr_01": "100%"}, outcome="总增分99分")
    draft = '```json\n' + json.dumps({"strategies": [item]}, ensure_ascii=False) + '\n```'
    assert prepare_strategy_scores(draft, customer, facts) == (draft, [])


@pytest.mark.parametrize("instruction", [
    "维持原值", "：维持原值。", "维持原值；",
])
def test_explicit_unchanged_target_remains_verifiable(instruction):
    from services.ai.fact_check import parse_result
    customer, facts, sources = _score_case()
    current = facts['factors']['kcr_01']['current_value']
    item = structured_strategy(changes={'kcr_01': current}, action='kcr_01' + instruction,
                               outcome='总增分0分')
    draft = '```json\n' + json.dumps({'strategies': [item]}, ensure_ascii=False) + '\n```'
    checked = parse_result(CLEAN, draft, sources, customer=customer, scoring_facts=facts)
    assert checked.status == 'passed' and checked.verified_projections


@pytest.mark.parametrize("instruction", [
    "不再维持原值，提升至100%", "无需维持原值，提升至100%",
    "维持原值，并提升至100%", "提升至100%，不维持原值",
    "维持原值或提升至100%", "维持原值，目标为100%",
    "维持原值；随后kcr_01提升至100%",
])
def test_negated_or_conflicting_unchanged_targets_never_get_engine_proof(instruction):
    from services.ai.fact_check import parse_result
    customer, facts, sources = _score_case()
    current = facts['factors']['kcr_01']['current_value']
    item = structured_strategy(changes={'kcr_01': current}, action='kcr_01' + instruction,
                               outcome='总增分0分')
    draft = '```json\n' + json.dumps({'strategies': [item]}, ensure_ascii=False) + '\n```'
    checked = parse_result(CLEAN, draft, sources, customer=customer, scoring_facts=facts)
    assert checked.status == 'unverified' and not checked.verified_projections
    assert prepare_strategy_scores(draft, customer, facts) == (draft, [])


def test_low_score_without_alerts_still_produces_urgent_shortfall_actions():
    from services.scoring import get_scoring_strategy
    customer, _, _ = _score_case()
    assessment = get_scoring_strategy().evaluate(customer)
    assessment.total_score = 49
    assessment.level = "风险"
    assessment.alerts = []
    assessment.suggestions = []
    items = build_degraded_strategies(assessment)
    assert items and all(i["urgency"] == "high" for i in items)
    assert "保持现有服务节奏" not in json.dumps(items, ensure_ascii=False)
    assert "可控状态" not in build_alert_context(assessment, None)


def test_quality_diagnostics_keep_categories_without_customer_or_exception_text():
    data = quality_diagnostics({"verification": [{"status": "unverified", "reason": "客户张三：核对响应不完整",
                                                   "issues": [{"claim": "客户私密资料"}]}],
                                "local_repairs": [{"operation": "patch", "status": "failed", "reason": "secret-key"}]})
    assert data["checks"][0]["reason"] == "checker_protocol"
    assert "张三" not in str(data) and "secret-key" not in str(data) and "私密" not in str(data)


def test_existing_metric_database_is_migrated_idempotently(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'old.db'))
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE chat_turn_metrics (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('INSERT INTO chat_turn_metrics (id) VALUES (1)')
    migrate_add_quality_diagnostics(engine)
    migrate_add_quality_diagnostics(engine)
    assert "quality_diagnostics" in {c["name"] for c in inspect(engine).get_columns('chat_turn_metrics')}
    with engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT quality_diagnostics FROM chat_turn_metrics').scalar() == '{}'
