"""Synthetic protocol/behavior tests; no customer data or provider calls."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database import Base
from models import Customer, ChatSession, ChatMessage, AIAssessmentRun, AssessmentHistory
from services.ai.protocol import strict_json, ProtocolError, load_assets
from services.ai.facts import capture
from services.ai.catalog import build_catalog
from services.ai.contracts import build_contract
from services.ai.validator import validate
from services.ai.open_plan import repair_envelope
from services.ai.orchestrator import execute
from services.ai.repository import resolve_messages, resolve_strategy_history


@pytest.fixture
def v2db(tmp_path):
    engine = create_engine("sqlite:///"+str(tmp_path/"v2.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(customer_name="合成示例", custom_fields={})
        db.add(customer)
        db.commit()
        session = ChatSession(customer_id=customer.id, scenario="assessment")
        db.add(session)
        db.commit()
        yield db, customer, session
    engine.dispose()


def valid_plan(package, catalog, contract):
    req = contract["requirements"]
    actions = []
    for i in range(req['actions']['min']):
        actions.append(dict(id=f'a:{i}', title=f'合成推进步骤{i}', reason='综合价值与推进条件形成待评审材料。',
            steps=['选择客户认可的价值口径', '收集基线与投入并标注缺失项', '评审测算假设'],
            suggested_owner='建议业务负责人和客户经理', suggested_timing='建议下一次评审前',
            deliverable='待评审测算表', expected_outcome='支持后续讨论，不承诺结果。', depends_on=[], source_refs=[],
            priority='recommended' if i == 0 else 'alternative'))
    risk_nodes = [n for n in catalog["render_nodes"].values() if "risk" in n["sections"] and set(n["coverage_keys"]) & set(req["risks"]["required_alert_refs"])]
    details = []
    for section in req["required_sections"]:
        if section in ("trend", "explanation", "data_quality"):
            details.extend(dict(section=section, render_ref=n["id"]) for n in catalog["render_nodes"].values()
                           if section in n["sections"])
    return dict(schema_version="2.4", snapshot_id=package["snapshot_id"], request_contract_id=contract["id"],
        summary_refs=["r:score"] if req['summary']['min'] else [], risks=[dict(render_ref=n["id"], priority=i+1) for i, n in enumerate(risk_nodes)],
        strength_refs=[], detail_refs=details, hypotheses=[], actions=actions,
        verification_questions=[f'建议确认第{i+1}项推进条件？' for i in range(req['questions']['exact'])], projections=[],
        analysis=dict(summary='可尝试将定性价值转成可评审材料，这一判断可能有误。', risks=[], strengths=[]), citation_refs=[])


class Adapter:
    def __init__(self, mutate=None):
        self.calls = []
        self.mutate = mutate

    def chat_completion_once(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        payload = json.loads(messages[-1]["content"])
        if "plan_schema" in payload:
            catalog = {k: {c["id"]: c for c in v} for k, v in payload["catalog"].items()}
            plan = valid_plan(dict(snapshot_id=payload["snapshot_id"]), catalog, payload["request_contract"])
            if self.mutate:
                self.mutate(plan)
            content = json.dumps(plan, ensure_ascii=False)
        else:
            raise AssertionError("unexpected repair")
        return SimpleNamespace(content=content, raw_usage={"total_tokens": 100}, tokens_used=100)


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', 'prefix {}', '{} trailing', '[Infinity]'])
def test_strict_rejects_untrusted_encodings(raw):
    with pytest.raises(ProtocolError):
        strict_json(raw)


def test_reviewed_prompts_and_schema():
    prompts, schema, manifest = load_assets()
    assert manifest['prompts_version'] == '2.4'
    assert schema['properties']['schema_version']['const'] == '2.4'
    assert set(prompts) == {'intent', 'plan', 'repair'}


def test_quoted_data_is_readable_without_markdown_or_reportlab_markup():
    from services.ai.catalog import quoted
    value = quoted('[3,2,1,0,-1] <b> **假标题**')
    assert "&#" not in value and "<b>" not in value and "**" not in value
    assert "［3,2,1,0,-1］" in value


def test_dlp_preserves_opaque_ids_but_masks_question_history_and_evidence():
    from services.ai.planner import safe_model_package
    opaque = "contract:ab13812345678cd"
    payload = dict(request_contract=dict(id=opaque), snapshot_id="snapshot:aa13812345678bb",
        question="电话13812345678", history=[dict(content="电话13812345678")],
        untrusted_evidence=[dict(content="电话13812345678")], base_plan=dict(request_contract_id=opaque))
    safe = safe_model_package(payload)
    assert safe["request_contract"]["id"] == opaque
    assert safe["snapshot_id"] == payload["snapshot_id"]
    assert safe["base_plan"]["request_contract_id"] == opaque
    assert all("13812345678" not in json.dumps(safe[k]) for k in ("question", "history", "untrusted_evidence"))


def test_run_no_history_point_and_reference_only_message(v2db):
    db, customer, session = v2db
    adapter = Adapter()
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=adapter, retrieve_evidence=False)
    assert run.status == "succeeded", run.error_code
    assert len(adapter.calls) == 1
    assert db.query(AssessmentHistory).count() == 0
    message = db.query(ChatMessage).one()
    assert message.content == "" and message.strategy_items == [] and message.references == []
    resolved = resolve_messages(db, [message])[0]
    assert resolved["availability"] == "available"
    assert len(resolved["strategy_items"]) == 1
    db.delete(run)
    db.commit()
    assert resolve_messages(db, [message])[0]["content"] == "此历史结果已不可用，请重新评估"


def test_idempotency_returns_same_run_without_call(v2db):
    db, customer, session = v2db
    adapter = Adapter()
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=adapter, idempotency_key="same", retrieve_evidence=False)
    again = execute(db, customer.id, "assessment", session_id=session.id, adapter=adapter, idempotency_key="same", retrieve_evidence=False)
    assert run.run_id == again.run_id and len(adapter.calls) == 1
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        execute(db, customer.id, "assessment", question="changed", session_id=session.id, adapter=adapter, idempotency_key="same")


def test_mutations_rejected(v2db):
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id)
    catalog = build_catalog(package)
    contract = build_contract(package, catalog, "report")
    schema = load_assets()[1]
    plan = valid_plan(package, catalog, contract)
    assert not validate(plan, package, catalog, contract, schema)
    for mutation in (
        lambda p: p["actions"][0].update(source_refs=["src:missing"]),
        lambda p: p.update(snapshot_id="snapshot:another"),
        lambda p: p["summary_refs"].append("r:invented"),
        lambda p: p["actions"][1].update(id=p["actions"][0]["id"]),
        lambda p: p["actions"].pop(),
        lambda p: p["actions"][0].update(stage="later_vision"),
    ):
        broken = deepcopy(plan)
        mutation(broken)
        assert validate(broken, package, catalog, contract, schema)


def test_uncertain_business_judgment_does_not_change_engine_facts(v2db):
    from services.ai.renderer import render
    db, customer, _ = v2db
    package = capture(db.get_bind(), customer.id)
    catalog = build_catalog(package)
    contract = build_contract(package, catalog, "report")
    plan = valid_plan(package, catalog, contract)
    frozen = deepcopy(package)
    plan['analysis']['summary'] = '可能需要优先开展试点；这一判断可能不适合客户。'
    plan['hypotheses'] = [dict(statement='业务方可能愿意共同评审价值口径', how_to_check='邀请业务方确认意愿')]
    plan['actions'][0]['reason'] = '建议先形成待评审材料，判断可能错误。'
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    result = render(plan, package, catalog, contract)
    assert result['assessment'] == frozen['assessment']
    assert result['trend'] == frozen['trend']
    assert package == frozen
    assert result['analysis'] == plan['analysis']


def test_position_repair_preserves_valid_siblings(v2db):
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id)
    catalog = build_catalog(package)
    contract = build_contract(package, catalog, "report")
    schema = load_assets()[1]
    plan = valid_plan(package, catalog, contract)
    del plan["actions"][2]["id"]
    errors = validate(plan, package, catalog, contract, schema)
    envelope = repair_envelope(plan, package, catalog, contract, schema, errors)
    assert len(envelope.slots) == 1
    value = "a:2"
    repaired = envelope.merge(dict(base_plan_hash=envelope.base_hash, replacements=[dict(slot_id=envelope.slots[0]["slot_id"], value=value)]))
    assert repaired["actions"][:2] == plan["actions"][:2]
    assert not validate(repaired, package, catalog, contract, schema, assigned_local_ids=envelope.assigned_local_ids)


def test_real_customer_mutation_blocks_publication(v2db):
    db, customer, session = v2db
    class MutatingAdapter(Adapter):
        def chat_completion_once(self, messages, **kwargs):
            with Session(db.get_bind()) as other:
                row = other.get(Customer, customer.id)
                row.custom_fields = {"kcr_01": "100%"}
                other.commit()
            return super().chat_completion_once(messages, **kwargs)
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=MutatingAdapter(), retrieve_evidence=False)
    assert run.status == "source_changed"
    assert db.query(ChatMessage).count() == 0


def test_source_withdrawal_clears_message_history_and_audit_copies(v2db):
    from models import KnowledgeDocument, KnowledgeItem, KnowledgeChunk
    from services.ai.protocol import fingerprint
    from services.ai.repository import expire_private_data
    db, customer, session = v2db
    doc = KnowledgeDocument(title="合成资料", status="canonical", index_status="indexed")
    db.add(doc); db.flush()
    item = KnowledgeItem(document_id=doc.id, title="合成资料", category="内部规范", status="canonical")
    chunk = KnowledgeChunk(document_id=doc.id, content="条件成立后方可使用，不代表本客户适用。")
    db.add_all([item, chunk]); db.commit()
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=Adapter(), retrieve_evidence=False)
    assert run.status == "succeeded", (run.status, run.error_code)
    package = deepcopy(run.fact_package)
    package["evidence"] = [dict(chunk_id=chunk.id, document_id=doc.id, item_id=item.id, content=chunk.content,
        title=item.title, category=item.category, content_hash=fingerprint(dict(content=chunk.content, title=item.title,
        category=item.category, status=item.status, document_status=doc.status)))]
    run.fact_package = package
    db.commit()
    message = db.query(ChatMessage).one()
    assert resolve_messages(db, [message])[0]["availability"] == "available"
    item.status = "proposed"; db.commit()
    for purpose in ("display", "preview", "model_history"):
        result = resolve_messages(db, [message], purpose)[0]
        assert result["content"] == "此历史结果已不可用，请重新评估"
        assert result["strategy_items"] == [] and result["references"] == []
    expire_private_data(db)
    assert run.status == "source_unavailable"
    assert run.fact_package == run.validated_plan == run.validated_result == {}


def test_scoped_reference_cannot_read_another_session(v2db):
    from services.ai.repository import available_result
    db, customer, session = v2db
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=Adapter(), retrieve_evidence=False)
    assert available_result(db, run, customer_id=customer.id+1, session_id=session.id) is None
    assert available_result(db, run, customer_id=customer.id, session_id=session.id+1) is None


def test_cancel_before_and_after_commit(v2db):
    import threading
    from services.ai.chat import cancel_run
    db, customer, session = v2db
    cancelled = threading.Event()
    class CancellingAdapter(Adapter):
        def chat_completion_once(self, messages, **kwargs):
            result = super().chat_completion_once(messages, **kwargs)
            cancelled.set()
            return result
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=CancellingAdapter(),
                  cancelled=cancelled, retrieve_evidence=False)
    assert run.status == "cancelled" and db.query(ChatMessage).count() == 0
    success = execute(db, customer.id, "assessment", session_id=session.id, adapter=Adapter(), retrieve_evidence=False)
    assert cancel_run(db, session.id, success.run_id).status == "succeeded"


def test_extreme_provider_fallback_explicit_and_regeneration_retains_old(v2db):
    from services.ai.llm_adapter import LLMUnavailableError
    db, customer, session = v2db
    old = execute(db, customer.id, "assessment", session_id=session.id, adapter=Adapter(), retrieve_evidence=False)
    message = db.query(ChatMessage).one()
    class Unavailable(Adapter):
        def chat_completion_once(self, *args, **kwargs):
            raise LLMUnavailableError("offline")
    failed = execute(db, customer.id, "assessment", session_id=session.id, adapter=Unavailable(),
                     supersede_message_id=message.id, base_contract=old.request_contract, retrieve_evidence=False)
    assert failed.status == "dependency_unavailable" and not failed.validated_result
    assert db.get(ChatMessage, message.id).superseded_by_id is None
    emergency = execute(db, customer.id, "assessment", session_id=session.id, adapter=Unavailable(), retrieve_evidence=False)
    assert emergency.status == "succeeded" and emergency.error_code == "dependency_unavailable"
    assert emergency.validated_result["degraded"] is True
    assert "规则兜底" in emergency.validated_result["content"]


def test_no_hidden_third_attempt_for_invalid_plan(v2db):
    db, customer, session = v2db
    class Invalid(Adapter):
        def chat_completion_once(self, *args, **kwargs):
            self.calls.append(1)
            return SimpleNamespace(content='{"body":"fabricated"}', raw_usage={"total_tokens": 1})
    adapter = Invalid()
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=adapter, retrieve_evidence=False)
    assert run.status == "incomplete" and len(adapter.calls) <= 2
    assert not db.query(ChatMessage).count()


def test_reverse_order_append_slots_and_unknown_slot(v2db):
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id)
    catalog = build_catalog(package)
    contract = build_contract(package, catalog, "report")
    schema = load_assets()[1]
    original = valid_plan(package, catalog, contract)
    plan = deepcopy(original); plan["actions"] = plan["actions"][:1]
    envelope = repair_envelope(plan, package, catalog, contract, schema, validate(plan, package, catalog, contract, schema))
    replacement = []
    for slot in envelope.slots:
        path = envelope.paths[slot["slot_id"]]
        assert path[0] == "actions"
        value = deepcopy(original["actions"][int(path[1])]); value["id"] = slot["schema"]["properties"]["id"]["const"]
        replacement.append(dict(slot_id=slot["slot_id"], value=value))
    response = dict(base_plan_hash=envelope.base_hash, replacements=list(reversed(replacement)))
    fixed = envelope.merge(response)
    assert fixed["actions"][0] == plan["actions"][0] and len(fixed["actions"]) == 3
    response["replacements"][0]["slot_id"] = "slot:invented"
    with pytest.raises(ProtocolError):
        envelope.merge(response)


def test_whole_json_repair_and_missing_top_level(v2db):
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package)
    contract = build_contract(package, catalog, "report"); schema = load_assets()[1]
    valid = valid_plan(package, catalog, contract)
    envelope = repair_envelope(None, package, catalog, contract, schema, [ProtocolError("invalid_json")])
    repaired = envelope.merge(dict(base_plan_hash=envelope.base_hash, replacements=[dict(slot_id="slot:s1", value=valid)]))
    assert repaired == valid
    missing = deepcopy(valid); del missing["summary_refs"]
    envelope = repair_envelope(missing, package, catalog, contract, schema, validate(missing, package, catalog, contract, schema))
    assert list(envelope.paths.values()) == [("summary_refs",)]


def test_newest_history_message_never_exceeds_outbound_budget(v2db, monkeypatch):
    import config
    from services.ai.chat_engine import _history_messages
    db, customer, session = v2db
    db.add(ChatMessage(session_id=session.id, role="user", content="x"*500))
    db.commit()
    monkeypatch.setattr(config, "CHAT_CONTEXT_CHAR_BUDGET", 40)
    assert sum(len(m.content) for m in _history_messages(db, session)) <= 40


def test_comparison_member_changes_and_deletion_are_not_publishable(v2db):
    from services.ai.facts import capture_comparison, current_fingerprint, explicit_comparison_ids
    db, customer, session = v2db
    other = Customer(customer_name="另一个合成对象", custom_fields={"kcr_01": "100%"})
    db.add(other); db.commit()
    assert explicit_comparison_ids(db, "比较当前客户与另一个合成对象", customer.id) == [other.id]
    package = capture_comparison(db.get_bind(), customer.id, [other.id])
    catalog = build_catalog(package)
    intent = dict(kind="compare", subject_refs=list(package["snapshot"]["members"]), field_refs=[], option_refs=[],source_refs=[], count=None)
    contract = build_contract(package, catalog, "free_qa", intents=[intent])
    plan = valid_plan(package, catalog, contract)
    plan["detail_refs"] = [dict(section="comparison", render_ref="r:comparison")]
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    assert current_fingerprint(db.get_bind(), package) == package["source_fingerprint"]
    other.custom_fields = {}; db.commit()
    assert current_fingerprint(db.get_bind(), package) != package["source_fingerprint"]


def test_projection_uses_engine_group_recalculation_and_rejects_unknown(v2db):
    from services.ai.projection import project
    db, customer, session = v2db
    customer.custom_fields = {"kcr_01": "0-39%", "er_01": "0-39%"}; db.commit()
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package)
    choices = [o for o in catalog["projection_options"].values() if o["field_ref"] in ("field:kcr_01", "field:er_01") and o["value"] == "100%"]
    intent = dict(kind="projection", subject_refs=[package["customer_ref"]], field_refs=[o["field_ref"] for o in choices],
                  option_refs=[o["id"] for o in choices],source_refs=[], count=None)
    contract = build_contract(package, catalog, "free_qa", intents=[intent])
    plan = valid_plan(package, catalog, contract)
    plan["projections"] = [dict(id="p:0", purpose="what_if", changes=[dict(field_ref=o["field_ref"], option_ref=o["id"]) for o in choices])]
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    result = project(package, {"kcr_01": "100%", "er_01": "100%"})
    assert result["delta"] > 0
    assert project(package, {"kcr_01": "100%"})["delta"] < result["delta"]
    with pytest.raises(ProtocolError, match="unknown_projection_baseline"):
        project(package, {"kcr_08": "VP级"})


def test_policy_no_hit_is_explicit_unknown_not_degraded(v2db):
    from services.ai.renderer import render
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id); package["retrieval_status"] = "no_hits"
    catalog = build_catalog(package)
    intent = dict(kind="policy", subject_refs=[package["customer_ref"]], field_refs=[], option_refs=[],source_refs=[], count=None)
    contract = build_contract(package, catalog, "free_qa", intents=[intent])
    plan = valid_plan(package, catalog, contract); plan["detail_refs"] = [dict(section="policy", render_ref="r:evidence_status")]
    assert not validate(plan, package, catalog, contract, load_assets()[1])
    output = render(plan, package, catalog, contract)
    assert "检索未找到可引用条款" in output["content"] and not output.get("degraded")


def test_migration_consistent_backup_and_idempotent_bridge(tmp_path):
    import sqlite3
    from database import backup_before_ai_v2, migrate_ai_v2
    path = tmp_path/"legacy.db"
    with sqlite3.connect(path) as raw:
        raw.executescript("CREATE TABLE customers(id INTEGER PRIMARY KEY); CREATE TABLE chat_sessions(id INTEGER PRIMARY KEY);"
            "CREATE TABLE chat_messages(id INTEGER PRIMARY KEY, content TEXT); CREATE TABLE assessment_history(id INTEGER PRIMARY KEY);")
        raw.execute("INSERT INTO chat_messages VALUES(1, 'immutable legacy')")
    engine = create_engine("sqlite:///"+str(path))
    backup = backup_before_ai_v2(engine)
    assert backup and backup.is_file()
    assert migrate_ai_v2(engine) == 4
    assert migrate_ai_v2(engine) == 0
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT content,content_format,ai_run_id FROM chat_messages").fetchone() == ("immutable legacy", "legacy", None)
        assert not raw.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(backup) as raw:
        assert [r[1] for r in raw.execute("PRAGMA table_info(chat_messages)")] == ["id", "content"]
    engine.dispose()
