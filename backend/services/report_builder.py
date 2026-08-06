"""Step 7 (M5) 报告整合：把 AI 策略建议 + 知识溯源 + 健康分趋势汇总为报告数据。

复用既有能力，不重复造轮子：
- 评分引擎算量化评估（services.scoring）
- assessment_history.build_trend 取趋势点位（含 fallback 到当前评估）
- context_builder.build_context 组装事实上下文
- ai.strategy.generate 跑策略 Agent Loop（含 LLM 不可用降级）
- ai.strategy.split_strategy_payload / build_degraded_strategies 解析与兜底

设计要点：报告导出是「读取型」操作，不写库、不持久化会话；任何 AI 异常都降级为
规则引擎建议，保证 PDF 一定能导出。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config
from models import Customer
from schemas import AssessmentResponse, AssessmentTrendResponse
from services import assessment_history
from services.scoring import get_scoring_strategy


@dataclass
class ReportData:
    assessment: AssessmentResponse
    trend: AssessmentTrendResponse | None = None
    strategy_items: list[dict] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)
    degraded: bool = False
    has_ai: bool = False
    error: str | None = None


def build_report_data(
    db,
    customer: Customer,
    *,
    include_ai: bool = True,
    scenario: str = "strategy",
) -> ReportData:
    """汇总单客户报告的全部数据；``include_ai=False`` 时只产出量化评估 + 趋势。"""
    engine = get_scoring_strategy()
    assessment = engine.evaluate(customer)
    trend = assessment_history.build_trend(
        db, customer, limit=config.CHAT_TREND_POINTS, fallback_assessment=assessment
    )

    data = ReportData(assessment=assessment, trend=trend)
    if not include_ai:
        return data

    # 懒导入，避免在无需 AI 的路径上硬耦合 LLM / RAG
    from services.ai import context_builder, llm_adapter, strategy as strategy_mod

    adapter = llm_adapter.get_chat_adapter()
    if not getattr(adapter, "available", False):
        # LLM 不可用：直接走规则引擎兜底（与对话降级同款建议）
        data.strategy_items = strategy_mod.build_degraded_strategies(assessment)
        data.degraded = True
        data.has_ai = True
        return data

    question = (
        "请基于以上量化评估与历史趋势，为该客户生成分层（推荐 / 备选 / 长期）的客情维护"
        "策略建议，并尽量标注知识来源。"
    )
    try:
        ctx = context_builder.build_context(db, customer, assessment=assessment, query=question)
        gen = strategy_mod.generate(scenario, ctx, customer, db, adapter=adapter, question=question)
        _body, items = strategy_mod.split_strategy_payload(gen.text)
        data.strategy_items = gen.degraded_items if gen.degraded else items
        data.references = list(gen.references or [])
        data.degraded = gen.degraded
        data.has_ai = True
        if not data.strategy_items:
            # AI 已参与但未产出结构化策略：用规则引擎建议兜底，并如实标注
            data.strategy_items = strategy_mod.build_degraded_strategies(assessment)
            if not gen.degraded:
                data.error = "AI 未返回结构化策略建议，已用规则引擎建议补充"
    except Exception as exc:  # 兜底：绝不能让报告导出失败
        data.strategy_items = strategy_mod.build_degraded_strategies(assessment)
        data.degraded = True
        data.has_ai = True
        data.error = f"AI 建议生成失败，已降级为规则引擎：{exc}"

    return data
