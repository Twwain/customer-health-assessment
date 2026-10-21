"""报告恢复预算默认与聊天对齐，且保留独立配置及任务存活余量。"""

import runpy
from pathlib import Path

import pytest


def report_config(monkeypatch, **overrides):
    # 防止开发机.env影响默认值及边界验收，不更改运行进程的config模块。
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for name in ("REPORT_AGENT_MAX_ITERATIONS", "REPORT_TURN_TOKEN_BUDGET",
                 "REPORT_GENERATION_TIMEOUT_SECONDS", "PDF_JOB_TTL", "LLM_REPORT_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, str(value))
    return runpy.run_path(str(Path(__file__).parents[1] / "config.py"))


def test_report_recovery_defaults_match_chat_capacity(monkeypatch):
    cfg = report_config(monkeypatch)
    assert cfg["REPORT_AGENT_MAX_ITERATIONS"] == 16
    assert cfg["REPORT_TURN_TOKEN_BUDGET"] == 1_000_000
    assert cfg["REPORT_GENERATION_TIMEOUT_SECONDS"] == 600
    assert cfg["PDF_JOB_TTL"] >= 660


@pytest.mark.parametrize("rounds, expected", [(1, 1), (8, 8), (16, 16), (99, 21), (0, 1)])
def test_report_rounds_remain_independently_configurable(monkeypatch, rounds, expected):
    cfg = report_config(monkeypatch, REPORT_AGENT_MAX_ITERATIONS=rounds,
                        CHAT_AGENT_MAX_ITERATIONS=2)
    assert cfg["REPORT_AGENT_MAX_ITERATIONS"] == expected
    assert cfg["CHAT_AGENT_MAX_ITERATIONS"] == 2


def test_report_custom_budget_and_pdf_lifetime(monkeypatch):
    cfg = report_config(monkeypatch, REPORT_TURN_TOKEN_BUDGET=250_000,
                        REPORT_GENERATION_TIMEOUT_SECONDS=900, PDF_JOB_TTL=60)
    assert cfg["REPORT_TURN_TOKEN_BUDGET"] == 250_000
    assert cfg["REPORT_GENERATION_TIMEOUT_SECONDS"] == 900
    assert cfg["PDF_JOB_TTL"] == 960
