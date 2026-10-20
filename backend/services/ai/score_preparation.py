"""Compute explicit strategy gains before asking the model to verify the answer."""
import json
from dataclasses import replace

from . import fact_check, local_quality
from .scoring_facts import project_scores


def prepare_strategy_scores(draft, customer, facts):
    if customer is None or not facts:
        return draft, []
    try:
        answer = local_quality.parse_answer(draft)
    except ValueError:
        return draft, []
    replacements, diagnostics = {}, []
    for unit in answer.units:
        if unit.item is None or not unit.item.get("score_targets"):
            continue
        item = dict(unit.item)
        targets = item["score_targets"]
        if not isinstance(targets, dict):
            continue
        targets = {key: facts["factors"][key]["current_value"]
                   if value == "维持原值" and key in facts["factors"] else value
                   for key, value in targets.items()}
        item["score_targets"] = targets
        action = str(item.get("action", ""))
        try:
            evidence = fact_check._projection_target_evidence(
                draft, {"dimension": "total"}, targets, facts, scope_override=action)
            if evidence is None:
                continue
            projection = project_scores(customer, targets)
        except (ValueError, TypeError):
            continue
        outcome = str(item.get("expected_outcome", ""))
        edits = []
        for expression in fact_check._score_expressions(outcome, facts):
            if (expression["dimension"] == "total" and expression["numeric_only"]
                    and expression["complete_tail"] and expression["claim"].startswith("总增分")):
                gain = projection["delta"]["total_score"]
                edits.append((expression["start"], expression["end"], f"总增分约{gain:.1f}分"))
        for start, end, value in reversed(edits):
            outcome = outcome[:start] + value + outcome[end:]
        if not fact_check._score_expressions(outcome, facts):
            outcome += f"（评分引擎按上述目标测算：总增分约{projection['delta']['total_score']:.1f}分，须实际达成后复评。）"
        item["expected_outcome"] = outcome
        if item != unit.item:
            replacements[unit.id] = replace(unit, item=item, text=json.dumps(item, ensure_ascii=False))
            diagnostics.append({"operation": "engine_scores", "status": "applied", "unit": unit.id})
    return (answer.render(replacements=replacements) if replacements else draft), diagnostics
