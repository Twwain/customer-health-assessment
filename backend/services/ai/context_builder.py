"""LLM 上下文组装。

把「量化评估引擎」的输出（基础客情分、维度明细、预警、趋势、因子快照）
组装成结构化 Markdown 注入 Prompt——这是 AI 结论不跑偏的事实基座。

知识增强引擎（RAG）的上下文由 ``build_knowledge_context`` 注入：调用检索召回
canonical 知识切片，附带来源供 📎 溯源。Embedding 不可用时静默降级为
``NO_KNOWLEDGE_HINT``，配合护栏防止模型编造知识来源。
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

import config
from models import Customer
from schemas import AssessmentResponse, AssessmentTrendResponse
from services import assessment_history
from services.scoring import get_scoring_strategy, load_scoring_config

NO_KNOWLEDGE_HINT = (
    "## 知识库参考资料\n"
    "本次未检索到相关知识条目（知识增强引擎尚未接入或无匹配内容）。\n"
    "请仅依据上述量化评估数据作答，**不要虚构任何规范条款、案例或行业基准数值**。"
)

TREND_LABEL = {"up": "↑ 上升", "down": "↓ 下降", "flat": "→ 持平"}
MAX_ALERTS_IN_AI_CONTEXT = 12
_ALERT_LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class ChatContext:
    """一轮对话的事实上下文。"""

    customer: Customer | None = None
    assessment: AssessmentResponse | None = None
    trend: AssessmentTrendResponse | None = None
    customer_text: str = ""
    alert_text: str = ""
    knowledge_text: str = NO_KNOWLEDGE_HINT
    references: list[dict] = field(default_factory=list)

    @property
    def has_customer(self) -> bool:
        return self.customer is not None


# ── 分段渲染 ────────────────────────────────────────────────────────────────


def _alerts_for_ai(assessment: AssessmentResponse) -> tuple[list, int]:
    """按等级稳定排序并限制注入量，完整预警仍保留在 assessment 中。"""
    ordered = sorted(
        enumerate(assessment.alerts),
        key=lambda pair: (_ALERT_LEVEL_ORDER.get(pair[1].level, 9), pair[0]),
    )
    selected = [alert for _, alert in ordered[:MAX_ALERTS_IN_AI_CONTEXT]]
    return selected, max(0, len(ordered) - len(selected))


def _suggestions_for_ai(assessment: AssessmentResponse, selected_alerts: list) -> tuple[list[str], int]:
    """只保留已展示预警的建议与机会建议，避免建议泄露被裁剪的预警。"""
    scoring_config = load_scoring_config()
    alert_rules = (
        *scoring_config.alerts,
        *scoring_config.score_alerts,
        *scoring_config.trend_alerts,
    )
    alert_suggestion_by_id = {rule.id: rule.suggestion for rule in alert_rules}
    all_alert_suggestions = {rule.suggestion for rule in alert_rules if rule.suggestion}

    candidates: list[str] = []
    for alert in selected_alerts:
        suggestion = alert_suggestion_by_id.get(alert.id, "")
        if suggestion and suggestion in assessment.suggestions and suggestion not in candidates:
            candidates.append(suggestion)

    # 非预警规则产生的建议属于机会点，排在风险处置建议之后。
    for suggestion in assessment.suggestions:
        if suggestion not in all_alert_suggestions and suggestion not in candidates:
            candidates.append(suggestion)

    unique_total = len(dict.fromkeys(assessment.suggestions))
    selected = candidates[:MAX_ALERTS_IN_AI_CONTEXT]
    return selected, max(0, unique_total - len(selected))


def fmt_score(value: float) -> str:
    """分数展示口径：最多两位小数并去尾零（0.233333→0.23，30.0→30）。

    与 ``rules._stringify`` 的浮点约定一致，对话回复与 LLM 上下文统一走这里。
    不用 :g——有效数字超 6 位会退化为科学计数法（1234567.8→1.23457e+06）。
    """
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _customer_profile(c: Customer) -> str:
    """客户基础信息。"""
    lines = [
        "## 客户基础信息",
        f"- 客户名称：{c.customer_name}",
        f"- 所属行业：{c.industry or '未填写'}",
        f"- 对接人角色：{'已指定' if c.contact_person else '未指定'}",
        f"- 合作年限：{c.cooperation_years} 年",
        f"- 沟通频率：{c.contact_frequency or '未填写'}",
    ]

    lines += [
        f"- 年度合同金额：{c.contract_amount} 万元",
        f"- 增长潜力：{c.growth_potential or '未填写'}",
    ]

    if c.notes:
        lines.append(f"- 备注：{c.notes}")

    return "\n".join(lines)


def _assessment_section(a: AssessmentResponse) -> str:
    lines = [
        "## 量化评估引擎输出（基础客情分）",
        f"- 总分：{fmt_score(a.total_score)} / {fmt_score(a.max_score)}，等级「{a.level}」，评分配置版本 {a.config_version or 'n/a'}",
        "- 维度明细：",
    ]
    # 只注入维度得分，不逐条罗列因子明细（完整因子明细会让结论淹没在数据里）
    for d in a.dimensions:
        lines.append(f"  - {d.name}：{fmt_score(d.score)}/{fmt_score(d.max_score)}")

    selected_alerts: list = []
    if a.alerts:
        lines.append("- 规则引擎预警：")
        level_cn = {"high": "高", "medium": "中", "low": "低"}
        selected_alerts, omitted = _alerts_for_ai(a)
        for alert in selected_alerts:
            lines.append(f"  - [{level_cn.get(alert.level, alert.level)}] {alert.message}")
        if omitted:
            lines.append(f"  - 其余 {omitted} 项预警未展开，请以结构化评估结果为准")
    else:
        lines.append("- 规则引擎预警：无")

    if a.suggestions:
        lines.append("- 规则引擎建议（供参考，可在此基础上深化）：")
        suggestions, omitted = _suggestions_for_ai(a, selected_alerts)
        for s in suggestions:
            lines.append(f"  - {s}")
        if omitted:
            lines.append(f"  - 其余 {omitted} 项建议未展开")

    return "\n".join(lines)


def _trend_section(t: AssessmentTrendResponse | None) -> str:
    if not t or not t.points:
        return "## 历史趋势\n暂无历史评估记录，无法判断走势（本次为首次评估）。"

    series = " → ".join(f"{p.label} {fmt_score(p.total_score)}" for p in t.points)
    lines = [
        "## 历史趋势",
        f"- 最近 {len(t.points)} 次评估：{series}",
        f"- 趋势：{TREND_LABEL.get(t.trend, t.trend)}（较上次 {t.delta:+.1f} 分）"
        if t.previous_score is not None
        else "- 趋势：仅一次评估记录，暂无对比",
    ]
    return "\n".join(lines)


def build_alert_context(
    assessment: AssessmentResponse | None,
    trend: AssessmentTrendResponse | None,
) -> str:
    """预警解读场景的补充上下文。"""
    if not assessment:
        return ""

    lines = ["## 待解读的预警"]
    if assessment.alerts:
        level_cn = {"high": "高", "medium": "中", "low": "低"}
        alerts, omitted = _alerts_for_ai(assessment)
        for alert in alerts:
            lines.append(f"- [{level_cn.get(alert.level, alert.level)}] {alert.message}（规则 id：{alert.id}）")
        if omitted:
            lines.append(f"- 其余 {omitted} 项预警未展开，请优先解读以上高等级预警")
    else:
        lines.append("- 当前无触发的预警规则，请说明客户整体处于可控状态，并指出需要持续观察的指标。")

    if trend and trend.points:
        low = min(p.total_score for p in trend.points)
        high = max(p.total_score for p in trend.points)
        lines.append(f"- 历史区间：最低 {fmt_score(low)} 分、最高 {fmt_score(high)} 分，当前 {fmt_score(trend.latest_score)} 分")

    return "\n".join(lines)


def build_knowledge_context(
    query: str = "",
    *,
    customer: Customer | None = None,
    db: Session | None = None,
    embed_func=None,
    store=None,
    timings: dict[str, int] | None = None,
) -> tuple[str, list[dict]]:
    """RAG 上下文。

    调用检索召回 canonical 知识切片，返回 ``(文本, 引用列表)``。
    引用列表带 title / score / chunk_id / document_id / item_id，供前端 📎 溯源抽屉定位原文。

    ``embed_func`` / ``store`` 可注入（测试或特定场景）；缺省走默认向量库与 Embedding 适配器。

    Embedding 不可用 / 无命中 / 异常时静默降级为 ``NO_KNOWLEDGE_HINT``，
    不让 RAG 故障影响对话基础功能。
    """
    if not query or not query.strip():
        return NO_KNOWLEDGE_HINT, []
    try:
        from services.rag.retriever import retrieve_knowledge
    except Exception:  # pragma: no cover - 导入失败兜底
        return NO_KNOWLEDGE_HINT, []

    rag_started = time.monotonic()
    try:
        chunks = retrieve_knowledge(
            query, customer=customer, top_k=config.RAG_TOP_K, db=db, embed_func=embed_func,
            store=store, timings=timings,
        )
    except Exception:  # pragma: no cover - 检索失败兜底
        if timings is not None:
            timings["rag_ms"] = int((time.monotonic() - rag_started) * 1000)
        return NO_KNOWLEDGE_HINT, []
    if timings is not None:
        timings["rag_ms"] = int((time.monotonic() - rag_started) * 1000)

    if not chunks:
        return NO_KNOWLEDGE_HINT, []

    lines = ["## 知识库参考资料（RAG 检索命中，作答时请标注来源）"]
    references: list[dict] = []
    for i, c in enumerate(chunks, 1):
        lines.append(f"### 参考 {i}（{c.category} · 《{c.item_title}》）")
        lines.append(c.content)
        references.append(
            {
                "id": f"{c.document_id}:{c.chunk_index}",
                "title": c.item_title,
                "category": c.category,
                "score": round(c.score, 4),
                "snippet": (c.hit_content or c.content)[:200],
                "chunk_id": c.chunk_index,
                "document_id": c.document_id,
                "item_id": c.item_id,
            }
        )
    return "\n\n".join(lines), references


# ── 对外入口 ────────────────────────────────────────────────────────────────


def _metrics_section(db: Session, customer: Customer) -> str:
    """行业基准指标段落。

    客户有行业 → 注入该行业 + 通用指标；无行业 → 仅注入通用指标，
    避免把其他行业的特定基准错配给该客户。
    """
    try:
        from services.rag.metrics import format_metrics_context, query_metrics

        industry = (customer.industry or "").strip()
        if industry:
            metrics = query_metrics(db, industry=industry, limit=6)
        else:
            metrics = query_metrics(db, global_only=True, limit=6)
        return format_metrics_context(metrics)
    except Exception:  # pragma: no cover - 指标查询失败不影响上下文构建
        return ""


def build_context(
    db: Session,
    customer: Customer | None,
    *,
    assessment: AssessmentResponse | None = None,
    include_trend: bool = True,
    query: str = "",
    today: datetime.date | None = None,
    retrieve_knowledge: bool = True,
    rag_timings: dict[str, int] | None = None,
) -> ChatContext:
    """组装一轮对话所需的全部事实上下文。

    ``retrieve_knowledge=False``：跳过 RAG 检索（Agent 场景由 Agent Loop 自行检索，
    避免同一轮对话双重检索——embedding 双倍调用 + hit_count 重复计数）。
    """
    today = today or datetime.date.today()

    if not retrieve_knowledge:
        knowledge_text, references = NO_KNOWLEDGE_HINT, []
    if customer is None:
        # 未关联客户的自由问答同样走 RAG 检索（知识库问答不依赖客户上下文）
        if retrieve_knowledge:
            knowledge_text, references = build_knowledge_context(query, db=db, timings=rag_timings)
        return ChatContext(
            customer_text="## 客户上下文\n本次会话未关联具体客户，请基于通用客情方法论作答。",
            knowledge_text=knowledge_text,
            references=references,
        )

    if assessment is None:
        assessment = get_scoring_strategy().evaluate(customer)
    assessment_history.apply_trend_alerts(db, customer, assessment)

    trend = None
    if include_trend:
        trend = assessment_history.build_trend(
            db, customer, limit=config.CHAT_TREND_POINTS, fallback_assessment=assessment
        )

    sections = [
        _customer_profile(customer),
        _assessment_section(assessment),
        _trend_section(trend),
    ]
    metrics_text = _metrics_section(db, customer)
    if metrics_text:
        sections.append(metrics_text)
    customer_text = "\n\n".join(sections)
    if retrieve_knowledge:
        knowledge_text, references = build_knowledge_context(
            query, customer=customer, db=db, timings=rag_timings
        )

    return ChatContext(
        customer=customer,
        assessment=assessment,
        trend=trend,
        customer_text=customer_text,
        alert_text=build_alert_context(assessment, trend),
        knowledge_text=knowledge_text,
        references=references,
    )
