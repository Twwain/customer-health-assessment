"""PDF 实际入口记录非敏感报告成本，指标故障不影响下载。"""

from types import SimpleNamespace

import pytest

import config
from models import ChatTurnMetric
from routers import assessment as assessment_router
from services import report_builder
from services.ai.metrics import record_report_metric, summarize
from test_export_endpoints import (  # 复用隔离 SQLite / HTTP 夹具
    _offline_llm, _wait_ready, client, customer, db, engine, session_factory,
)


@pytest.fixture
def report_factory(monkeypatch):
    def build(db, customer, *, include_ai=True, **kwargs):
        return SimpleNamespace(
            has_ai=include_ai, degraded=False, warnings=[],
            assessment=object(), strategy_items=[], references=[], trend=None,
            timings={
                "llm_calls": [
                    {"phase": "reason", "total_tokens": 65000},
                    {"phase": "budget_final", "total_tokens": 35000},
                ],
                "tool_rounds": 2, "refine_rounds": 1,
                "verification": [{"status": "passed", "reason": ""}],
            },
        )

    monkeypatch.setattr(report_builder, "build_report_data", build)
    monkeypatch.setattr(assessment_router.pdf_gen, "generate", lambda *a, **kw: b"%PDF-test")
    return build


def _download(client, customer_id, mode, *, include_ai=True):
    endpoint = f"/api/assessment/{customer_id}/pdf"
    if mode == "async":
        created = client.post(endpoint + "/jobs", params={"include_ai": include_ai})
        assert created.status_code == 200
        job_id = created.json()["job_id"]
        assert _wait_ready(client, customer_id, job_id)["status"] == "ready"
        return client.get(endpoint + f"/jobs/{job_id}/download")
    return client.get(endpoint, params={"include_ai": include_ai})


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("degraded", [False, True])
def test_pdf_entries_persist_complete_report_usage(
    client, customer, db, monkeypatch, report_factory, mode, degraded,
):
    monkeypatch.setattr(config, "CHAT_TURN_TOKEN_BUDGET", 100)
    monkeypatch.setattr(config, "REPORT_TURN_TOKEN_BUDGET", 90000)

    def build(*args, **kwargs):
        report = report_factory(*args, **kwargs)
        report.degraded = degraded
        if degraded:
            report.warnings = ["已降级"]
            report.timings["verification"] = []
        return report

    monkeypatch.setattr(report_builder, "build_report_data", build)
    response = _download(client, customer.id, mode)
    assert response.status_code == 200
    assert response.content == b"%PDF-test"
    metric = db.query(ChatTurnMetric).one()
    assert metric.session_id is None
    assert metric.scenario == "report"
    assert metric.model == "offline"
    assert metric.tokens_used == 100000  # 包含最终收尾和降级前的所有付费调用
    assert metric.token_budget == 90000
    assert metric.token_overshoot == 10000
    assert metric.status == ("degraded" if degraded else "success")
    assert metric.warning_count == int(degraded)
    assert metric.llm_call_count == 2
    assert metric.tool_rounds == 2
    assert metric.refine_rounds == 1
    result = summarize(db)
    assert result["configured_token_budget"] == 100
    assert result["configured_report_token_budget"] == 90000
    assert result["by_scenario"]["report"]["tokens"]["p95"] == 100000


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_pdf_without_ai_does_not_record_llm_metric(client, customer, db, report_factory, mode):
    assert _download(client, customer.id, mode, include_ai=False).status_code == 200
    assert db.query(ChatTurnMetric).count() == 0


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_metric_database_failure_does_not_break_pdf(client, customer, engine, report_factory, mode):
    ChatTurnMetric.__table__.drop(engine)
    response = _download(client, customer.id, mode)
    assert response.status_code == 200
    assert response.content == b"%PDF-test"


@pytest.mark.parametrize("verification,code", [
    ([], "fact_check_unverified"),
    ([{"status": "unverified", "reason": "响应截断"}], "fact_check_unverified"),
    ([{"status": "issues", "reason": ""}], "fact_check_issues"),
    ([{"status": "passed", "reason": "部分目标未核对"}], "fact_check_unverified"),
])
def test_unverified_report_is_not_recorded_as_verified(db, report_factory, customer, verification, code):
    report = report_factory(db, customer)
    report.timings["verification"] = verification
    record_report_metric(db, report, elapsed_ms=12345)
    metric = db.query(ChatTurnMetric).one()
    assert metric.status == "partial"
    assert metric.error_code == code
    assert metric.latency_ms == 12345


def test_malformed_optional_metric_metadata_is_nonfatal(db, report_factory, customer):
    report = report_factory(db, customer)
    report.timings = {"llm_calls": [None, {"total_tokens": "bad"}], "tool_rounds": "bad"}
    record_report_metric(db, report, elapsed_ms=0)
    metric = db.query(ChatTurnMetric).one()
    assert metric.tokens_used == 0
    assert metric.status == "partial"
    report.timings = None
    record_report_metric(db, report, elapsed_ms=0)
    assert db.query(ChatTurnMetric).count() == 2
