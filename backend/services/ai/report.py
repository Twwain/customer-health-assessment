"""PDF adapter consumes the same committed result used by chat."""
from services.ai.orchestrator import execute
from services.ai.repository import available_result
from services.ai.protocol import ProtocolError


def build_report(db, customer, *, cancelled=None, deadline=None, job_id=None):
    from services.report_builder import ReportData
    run = execute(db, customer.id, "report", question="生成客情维护策略报告", cancelled=cancelled, deadline=deadline, job_id=job_id)
    if run.status != "succeeded" and not run.validated_result.get("degraded"):
        raise ProtocolError(run.error_code or "incomplete")
    result = available_result(db, run, customer_id=customer.id, job_id=job_id)
    if not result:
        raise ProtocolError("source_unavailable")
    from schemas import AssessmentResponse, AssessmentTrendResponse
    return ReportData(assessment=AssessmentResponse.model_validate(result["assessment"]),
        trend=AssessmentTrendResponse.model_validate(result["trend"]), strategy_items=result["strategy_items"],
        references=result["references"], has_ai=True, degraded=bool(result.get("degraded")),
        analysis=result.get("analysis", {}), hypotheses=result.get("hypotheses", []),
        warnings=(["AI 服务不可用，已使用规则兜底；本次 AI 任务未完成。"] if result.get("degraded") else [])+
                 list(result.get("fixed_blocks", {}).values()),
        timings=dict(run_id=run.run_id, llm_calls=run.usage["calls"], llm_call_count=run.usage["llm_call_count"]))
