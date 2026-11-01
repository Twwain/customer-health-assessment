"""Exercise the live report assertions offline with the current renderer contract."""
from types import SimpleNamespace

import pytest

import test_live_llm as live_tests
from test_ai_protocol import v2db, valid_plan
from test_ai_completion import fixture
from models import KnowledgeChunk, KnowledgeDocument
from services.ai.contracts import build_contract
from services.ai.renderer import render


@pytest.mark.parametrize("source", ["valid", "none", "foreign", "missing"])
def test_live_report_checks_current_chunk_sources(v2db, tmp_path, monkeypatch, source):
    db, customer, _ = v2db
    doc = KnowledgeDocument(id=71, title="合成制度", status="canonical")
    other = KnowledgeDocument(id=72, title="其他合成制度", status="canonical")
    db.add_all([doc, other])
    db.flush()
    if source != "missing":
        db.add(KnowledgeChunk(id=71, document_id=72 if source == "foreign" else 71,
                              content="合成制度内容"))
    db.commit()
    package, catalog, _, _, _ = fixture(v2db)
    contract = build_contract(package, catalog, "report")
    plan = valid_plan(package, catalog, contract)
    plan["citation_refs"] = [] if source == "none" else ["src:71"]
    result = render(plan, package, catalog, contract)
    report = SimpleNamespace(
        has_ai=True, degraded=False, error=None, warnings=[],
        strategy_items=result["strategy_items"], references=result["references"],
        analysis=result["analysis"], timings={"llm_call_count": 1, "llm_calls": []},
    )
    monkeypatch.setattr("services.report_builder.build_report_data", lambda *args, **kwargs: report)

    def run_assertions():
        live_tests.test_live_report_ai(None, (db, customer, doc), lambda *args: None,
                                       tmp_path, monkeypatch)

    if source in ("foreign", "missing"):
        with pytest.raises(AssertionError):
            run_assertions()
    else:
        run_assertions()
