"""Behavioral regressions for independently reviewed R1–R7."""
from copy import deepcopy
from datetime import timedelta
import json
import subprocess
import sys

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from test_ai_protocol import v2db, Adapter, valid_plan
from database import utcnow, get_db
from models import ChatMessage, AssessmentHistory, AIAssessmentRun, KnowledgeDocument, KnowledgeItem, KnowledgeChunk
from services.ai.protocol import load_assets, ProtocolError, fingerprint, schema_errors
from services.ai.facts import capture
from services.ai.catalog import build_catalog
from services.ai.contracts import build_contract
from services.ai.validator import validate
from services.ai.open_plan import compile_open as compile_schema, repair_envelope
from services.ai.orchestrator import execute
from services.ai.repository import resolve_messages, sources_changed, expire_private_data
from services.ai.chat import cancel_run
from services import assessment_history


def test_r1_count_dependency_does_not_answer_asked_factor(v2db):
    db, customer, session = v2db
    customer.custom_fields = {"kcr_01": "100%"}; db.commit()
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package)
    intent = dict(kind="explain_score", subject_refs=[package["customer_ref"]], field_refs=["field:kcr_01"], option_refs=[],source_refs=[], count=None)
    contract = build_contract(package, catalog, "free_qa", intents=[intent])
    plan = valid_plan(package, catalog, contract)
    plan["detail_refs"] = [dict(section="explanation", render_ref="r:quality")]
    assert any(e.code == "required_fact_missing" for e in validate(plan, package, catalog, contract, load_assets()[1]))
    plan["detail_refs"] = [dict(section="explanation", render_ref="r:kcr_01")]
    assert not validate(plan, package, catalog, contract, load_assets()[1])


def test_r2_scoped_questions_and_slot_values_never_mix_sources(v2db):
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package)
    intent = dict(kind="verify_questions", subject_refs=[package["customer_ref"]], field_refs=[], option_refs=[],source_refs=[], count=2)
    contract = build_contract(package, catalog, "free_qa", intents=[intent]); base = load_assets()[1]
    schema = compile_schema(base, package, catalog, contract)
    plan = valid_plan(package, catalog, contract)
    assert not schema_errors(plan, schema)
    good = deepcopy(plan)
    unexpected = deepcopy(plan)
    unexpected["citation_refs"] = ["src:1"]
    assert schema_errors(unexpected, schema)
    plan["verification_questions"][0] = ""
    assert schema_errors(plan, schema)
    errors = validate(plan, package, catalog, contract, base)
    envelope = repair_envelope(plan, package, catalog, contract, base, errors)
    question_slot = next(s for s in envelope.slots if envelope.paths[s["slot_id"]][0] == "verification_questions")
    assert "src:1" not in question_slot["allowed_refs"]
    assert question_slot['schema']['type'] == 'string'
    fixed = envelope.merge(dict(base_plan_hash=envelope.base_hash, replacements=[
        dict(slot_id=question_slot["slot_id"], value=good["verification_questions"][0])]))
    assert fixed["verification_questions"][1] == good["verification_questions"][1]
    assert not validate(fixed, package, catalog, contract, base)


def test_r2_missing_trend_slot_cannot_be_filled_with_unrelated_details(v2db):
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package)
    contract = build_contract(package, catalog, "assessment"); base = load_assets()[1]
    schema = compile_schema(base, package, catalog, contract)
    plan = valid_plan(package, catalog, contract); del plan["detail_refs"]
    assert schema_errors(plan, schema)
    errors = validate(plan, package, catalog, contract, base)
    envelope = repair_envelope(plan, package, catalog, contract, base, errors)
    slot = next(s for s in envelope.slots if envelope.paths[s["slot_id"]] == ("detail_refs",))
    wrong = [dict(section="explanation", render_ref="r:quality")]*24
    assert schema_errors(dict(base_plan_hash=envelope.base_hash, replacements=[dict(slot_id=slot["slot_id"], value=wrong)]), envelope.output_schema())
    fixed = envelope.merge(dict(base_plan_hash=envelope.base_hash,
        replacements=[dict(slot_id=slot["slot_id"], value=[dict(section="trend",render_ref="r:trend")])]))
    assert not validate(fixed, package, catalog, contract, base)


def test_r2_bad_detail_sections_repair_only_the_known_nodes(v2db):
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package)
    fields = ["kcr_01", "kcr_02", "kcr_04"]
    intents = [dict(kind="explain_score", subject_refs=[package["customer_ref"]], field_refs=["field:"+f for f in fields],option_refs=[],source_refs=[],count=None),
               dict(kind="trend",subject_refs=[package["customer_ref"]],field_refs=[],option_refs=[],source_refs=[],count=None)]
    contract = build_contract(package, catalog, "free_qa", intents=intents); base = load_assets()[1]
    plan = valid_plan(package, catalog, contract)
    plan["detail_refs"] = [dict(section="trend",render_ref="r:trend")]
    preserved = deepcopy(plan["detail_refs"])
    plan["detail_refs"] += [dict(section="reason", render_ref="r:"+field) for field in fields]
    errors = validate(plan, package, catalog, contract, base)
    envelope = repair_envelope(plan, package, catalog, contract, base, errors)
    assert len(envelope.slots) == 3
    replacements = []
    for slot, field in zip(envelope.slots, fields):
        assert slot["allowed_refs"] == ["r:"+field]
        assert schema_errors(dict(section='explanation', render_ref='r:invented'), slot['schema'])
        value = slot['schema']['const']
        assert value['render_ref'] == 'r:'+field
        replacements.append(dict(slot_id=slot["slot_id"], value=deepcopy(value)))
    fixed = envelope.merge(dict(base_plan_hash=envelope.base_hash, replacements=replacements))
    assert fixed["detail_refs"][:len(preserved)] == preserved
    assert not validate(fixed, package, catalog, contract, base)
    replacements[0]["value"]["section"] = "reason"
    try:
        envelope.merge(dict(base_plan_hash=envelope.base_hash, replacements=replacements))
        assert False, "invalid section accepted"
    except ProtocolError as exc:
        assert exc.pointer.startswith("/replacements/0")


def test_r2_diagnostics_keep_only_known_refs_enums_types_and_hashes(v2db):
    from services.ai.diagnostics import plan_shapes
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package)
    private = "arbitrary private draft 13812345678"
    output = plan_shapes(dict(detail_refs=[dict(section="risk", render_ref="r:score"),
        dict(section=private, render_ref=private), dict(section=[],render_ref={"sensitive":private})]),package,catalog)
    assert output[0]["known_render_ref"] == "r:score" and output[0]["registered_section"] == "risk"
    assert private not in json.dumps(output) and "13812345678" not in json.dumps(output)
    assert output[1]["unknown_ref_hash"] and output[2]["unknown_ref_hash"]


def test_r2_model_choices_follow_request_contract_without_losing_reasons(v2db):
    from services.ai.open_plan import model_catalog
    db, customer, session = v2db
    package = capture(db.get_bind(), customer.id); catalog = build_catalog(package); base=load_assets()[1]
    contract = build_contract(package,catalog,"report")
    schema=compile_schema(base,package,catalog,contract); safe=model_catalog(catalog,package,contract)
    assert "trend" in {v["properties"]["section"]["const"] for v in schema["properties"]["detail_refs"]["items"]["oneOf"]}
    assert {n['id'] for n in safe['render_nodes']} <= set(contract['allowed_catalog_ids'])
    assert not safe.get("action_candidates")
    assert schema['properties']['actions']['items']['properties']['reason']['type'] == 'string'
    intent=dict(kind="score_grade",subject_refs=[package["customer_ref"]],field_refs=[],option_refs=[],source_refs=[],count=None)
    qa=build_contract(package,catalog,"free_qa",intents=[intent]); qschema=compile_schema(base,package,catalog,qa)
    assert qschema["properties"]["actions"]["maxItems"] == 0
    assert not model_catalog(catalog,package,qa).get("action_candidates")


def test_r3_legacy_write_preserves_v2_ref_and_no_body_copy(v2db):
    db, customer, session = v2db
    history = assessment_history.record_assessment(db, customer, skip_if_unchanged=False)
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=Adapter(), retrieve_evidence=False)
    db.refresh(history)
    assert history.strategy_format == "ai_v2_ref"
    db.refresh(history)
    assert history.strategy_snapshot == [] and history.strategy_run_id == run.run_id
    db.delete(run); db.commit()
    result = assessment_history.to_history_items([history], db)[0]
    assert result.strategy_snapshot == [] and result.availability == "source_unavailable"


def test_r4_committed_fallback_cancel_retention_and_withdrawal(v2db):
    from services.ai.llm_adapter import LLMUnavailableError
    db, customer, session = v2db
    class Unavailable(Adapter):
        def chat_completion_once(self, *args, **kwargs):
            raise LLMUnavailableError("injected")
    run = execute(db, customer.id, "assessment", session_id=session.id, adapter=Unavailable(), retrieve_evidence=False)
    assert run.status == "succeeded" and run.validated_result["degraded"]
    message = db.query(ChatMessage).filter_by(ai_run_id=run.run_id).one()
    cancel_run(db, session.id, run.run_id)
    assert resolve_messages(db, [message])[0]["availability"] == "available"
    run.updated_at = utcnow()-timedelta(days=8); db.commit(); expire_private_data(db)
    assert run.fact_package and run.validated_result
    doc = KnowledgeDocument(title="合成来源", status="canonical", index_status="indexed")
    db.add(doc); db.flush()
    item = KnowledgeItem(document_id=doc.id, title="合成来源", category="内部规范", status="canonical")
    chunk = KnowledgeChunk(document_id=doc.id, content="WITHDRAWABLE_SOURCE")
    db.add_all([item, chunk]); db.commit()
    package = deepcopy(run.fact_package)
    package["evidence"] = [dict(chunk_id=chunk.id, document_id=doc.id, item_id=item.id, content=chunk.content,
        title=item.title, category=item.category, content_hash=fingerprint(dict(content=chunk.content, title=item.title,
            category=item.category, status=item.status, document_status=doc.status)))]
    run.fact_package = package; db.commit()
    item.status = "proposed"; db.commit(); sources_changed(db)
    assert run.fact_package == run.validated_plan == run.validated_result == {}
    assert run.status == "source_unavailable" and not resolve_messages(db, [message])[0]["references"]


def test_r5_regenerate_lost_confirmation_replays_same_run(v2db, monkeypatch):
    import config
    from main import app
    from services.ai import llm_adapter
    import services.ai.orchestrator as orchestrator
    import services.ai.evidence as evidence
    db, customer, session = v2db
    adapter = Adapter()
    monkeypatch.setattr(llm_adapter, "get_chat_adapter", lambda: adapter)
    monkeypatch.setattr(orchestrator, "get_chat_adapter", lambda: adapter)
    def no_hits(db, package, question, customer):
        package["retrieval_status"] = "no_hits"; return {}
    monkeypatch.setattr(evidence, "retrieve", no_hits)
    def dependency():
        with Session(db.get_bind()) as other:
            yield other
    app.dependency_overrides[get_db] = dependency
    try:
        client = TestClient(app)
        initial = client.post(f"/api/chat/sessions/{session.id}/evaluate", json={"stream": False}).json()
        assert initial["message"], initial
        assert not initial["degraded"]
        path = f"/api/chat/sessions/{session.id}/regenerate"
        first = client.post(path, json={"stream": False, "idempotency_key": "lost-done"}).json()
        assert first["message"], first
        calls = len(adapter.calls)
        retry = client.post(path, json={"stream": False, "idempotency_key": "lost-done"}).json()
        assert retry["message"], retry
        assert retry["run_id"] == first["run_id"] and retry["message"]["id"] == first["message"]["id"]
        assert len(adapter.calls) == calls
        streamed = client.post(path, json={"stream": True, "idempotency_key": "lost-done"})
        assert 'event: done' in streamed.text and first["run_id"] in streamed.text
        assert len(adapter.calls) == calls
        conflict = client.post(path, json={"stream": False, "idempotency_key": "lost-done", "scenario": "assessment"})
        assert conflict.status_code == 409
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_r6_dead_process_run_terminates_and_retains_old_answer(v2db):
    from services.ai.execution import recover_pending
    from routers.chat import _to_item
    db, customer, session = v2db
    old = execute(db, customer.id, "assessment", session_id=session.id, adapter=Adapter(), retrieve_evidence=False)
    message = db.query(ChatMessage).filter_by(ai_run_id=old.run_id).one()
    script = '''import sys,os
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from models import AIAssessmentRun
from services.ai.execution import register
engine=create_engine(sys.argv[1])
with Session(engine) as db:
 run=AIAssessmentRun(run_id="crashed-process",customer_id=int(sys.argv[2]),session_id=int(sys.argv[3]),scenario="assessment",payload_hash="crash",status="planning",versions={"runtime":register("crashed-process",60)})
 db.add(run);db.commit()
os._exit(0)
'''
    result = subprocess.run([sys.executable, "-c", script, str(db.get_bind().url), str(customer.id), str(session.id)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    summary = _to_item(db, session)
    assert not summary.streaming and "已保留原答案" in summary.run_notice
    crashed = db.get(AIAssessmentRun, "crashed-process")
    assert crashed.status == "provider_error" and crashed.error_code == "executor_lost"
    assert db.get(ChatMessage, message.id).superseded_by_id is None
    assert resolve_messages(db, [message])[0]["availability"] == "available"


def test_r7_report_changes_only_independent_run_audit(v2db, monkeypatch):
    import services.ai.evidence as evidence
    import services.ai.orchestrator as orchestrator
    from services.ai.report import build_report
    db, customer, session = v2db
    history = assessment_history.record_assessment(db, customer, skip_if_unchanged=False)
    before = {column.name: deepcopy(getattr(history, column.name)) for column in AssessmentHistory.__table__.columns}
    monkeypatch.setattr(orchestrator, "get_chat_adapter", lambda: Adapter())
    def no_hits(db, package, question, customer):
        package["retrieval_status"] = "no_hits"; return {}
    monkeypatch.setattr(evidence, "retrieve", no_hits)
    report = build_report(db, customer)
    assert report.has_ai and not report.degraded
    db.refresh(history)
    assert before == {column.name: getattr(history, column.name) for column in AssessmentHistory.__table__.columns}
