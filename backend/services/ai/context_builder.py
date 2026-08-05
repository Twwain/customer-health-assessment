"""LLM 上下文组装（SOW §3.2.1 上下文管理 / §3.4 双引擎结合）。

把「量化评估引擎」的输出（基础客情分、维度明细、预警、趋势、因子快照）
组装成结构化 Markdown 注入 Prompt——这是 AI 结论不跑偏的事实基座。

知识增强引擎（RAG）的上下文由 ``build_knowledge_context`` 注入：调用检索召回
canonical 知识切片，附带来源供 📎 溯源。Embedding 不可用时静默降级为
``NO_KNOWLEDGE_HINT``，配合护栏防止模型编造知识来源（SOW §7）。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

import config
from models import Customer
from schemas import AssessmentResponse, AssessmentTrendResponse
from services import assessment_history
from services.scoring import get_scoring_strategy

NO_KNOWLEDGE_HINT = (
    "## 知识库参考资料\n"
    "本次未检索到相关知识条目（知识增强引擎尚未接入或无匹配内容）。\n"
    "请仅依据上述量化评估数据作答，**不要虚构任何规范条款、案例或行业基准数值**。"
)

TREND_LABEL = {"up": "↑ 上升", "down": "↓ 下降", "flat": "→ 持平"}


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


def _fmt_date(value) -> str:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value or "未记录")


def _customer_profile(c: Customer, today: datetime.date) -> str:
    """客户基础信息。刻意不含联系电话等个人隐私字段（SOW §7）。"""
    lines = [
        "## 客户基础信息",
        f"- 客户名称：{c.customer_name}",
        f"- 所属行业：{c.industry or '未填写'}",
        f"- 对接人角色：{'已指定' if c.contact_person else '未指定'}",
        f"- 合作年限：{c.cooperation_years} 年",
        f"- 沟通频率：{c.contact_frequency or '未填写'}",
    ]

    if c.last_contact_date:
        days = (today - c.last_contact_date).days
        lines.append(f"- 最近联系：{_fmt_date(c.last_contact_date)}（距今 {days} 天）")
    else:
        lines.append("- 最近联系：无记录")

    lines += [
        f"- 年度合同金额：{c.contract_amount} 万元",
        f"- 回款状态：{c.payment_status or '未填写'}",
        f"- 增长潜力：{c.growth_potential or '未填写'}",
        f"- 竞品介入：{'是' if c.competitor_involvement else '否'}",
        f"- 风险信号：{c.risk_signals or '无'}",
    ]
    if c.notes:
        lines.append(f"- 备注：{c.notes}")

    extras = {k: v for k, v in (c.custom_fields or {}).items() if v not in (None, "")}
    if extras:
        detail = "；".join(f"{k}={v}" for k, v in extras.items())
        lines.append(f"- 扩展因子：{detail}")

    return "\n".join(lines)


def _assessment_section(a: AssessmentResponse) -> str:
    lines = [
        "## 量化评估引擎输出（基础客情分）",
        f"- 总分：{a.total_score} / {a.max_score}，等级「{a.level}」，评分配置版本 {a.config_version or 'n/a'}",
        "- 维度明细：",
    ]
    for d in a.dimensions:
        detail = "；".join(d.details) if d.details else "无扣分/加分明细"
        lines.append(f"  - {d.name}：{d.score}/{d.max_score} —— {detail}")

    if a.alerts:
        lines.append("- 规则引擎预警：")
        level_cn = {"high": "高", "medium": "中", "low": "低"}
        for alert in a.alerts:
            lines.append(f"  - [{level_cn.get(alert.level, alert.level)}] {alert.message}")
    else:
        lines.append("- 规则引擎预警：无")

    if a.suggestions:
        lines.append("- 规则引擎建议（供参考，可在此基础上深化）：")
        for s in a.suggestions:
            lines.append(f"  - {s}")

    return "\n".join(lines)


def _trend_section(t: AssessmentTrendResponse | None) -> str:
    if not t or not t.points:
        return "## 历史趋势\n暂无历史评估记录，无法判断走势（本次为首次评估）。"

    series = " → ".join(f"{p.label} {p.total_score}" for p in t.points)
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
        for alert in assessment.alerts:
            lines.append(f"- [{level_cn.get(alert.level, alert.level)}] {alert.message}（规则 id：{alert.id}）")
    else:
        lines.append("- 当前无触发的预警规则，请说明客户整体处于可控状态，并指出需要持续观察的指标。")

    if trend and trend.points:
        low = min(p.total_score for p in trend.points)
        high = max(p.total_score for p in trend.points)
        lines.append(f"- 历史区间：最低 {low} 分、最高 {high} 分，当前 {trend.latest_score} 分")

    return "\n".join(lines)


def build_knowledge_context(
    query: str = "",
    *,
    customer: Customer | None = None,
    db: Session | None = None,
    embed_func=None,
    store=None,
) -> tuple[str, list[dict]]:
    """RAG 上下文（SOW §3.3 / §3.4 双引擎结合）。

    调用检索召回 canonical 知识切片，返回 ``(文本, 引用列表)``。
    引用列表带 title / score / chunk_id / document_id / item_id，供前端 📎 溯源抽屉定位原文。

    ``embed_func`` / ``store`` 可注入（测试或特定场景）；缺省走默认向量库与 Embedding 适配器。

    Embedding 不可用 / 无命中 / 异常时静默降级为 ``NO_KNOWLEDGE_HINT``，
    不让 RAG 故障影响对话基础功能（SOW §7）。
    """
    if not query or not query.strip():
        return NO_KNOWLEDGE_HINT, []
    try:
        from services.rag.retriever import retrieve_knowledge
    except Exception:  # pragma: no cover - 导入失败兜底
        return NO_KNOWLEDGE_HINT, []

    try:
        chunks = retrieve_knowledge(
            query, customer=customer, top_k=config.RAG_TOP_K, db=db, embed_func=embed_func, store=store
        )
    except Exception:  # pragma: no cover - 检索失败兜底
        return NO_KNOWLEDGE_HINT, []

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
                "snippet": c.content[:200],
                "chunk_id": c.chunk_index,
                "document_id": c.document_id,
                "item_id": c.item_id,
            }
        )
    return "\n\n".join(lines), references


# ── 对外入口 ────────────────────────────────────────────────────────────────


def _metrics_section(db: Session, customer: Customer) -> str:
    """行业基准指标段落（SOW §3.3.1 结构化知识层：精确数值走 SQLite 精确查询）。

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
            knowledge_text, references = build_knowledge_context(query, db=db)
        return ChatContext(
            customer_text="## 客户上下文\n本次会话未关联具体客户，请基于通用客情方法论作答。",
            knowledge_text=knowledge_text,
            references=references,
        )

    if assessment is None:
        assessment = get_scoring_strategy().evaluate(customer)

    trend = None
    if include_trend:
        trend = assessment_history.build_trend(
            db, customer, limit=config.CHAT_TREND_POINTS, fallback_assessment=assessment
        )

    sections = [
        _customer_profile(customer, today),
        _assessment_section(assessment),
        _trend_section(trend),
    ]
    metrics_text = _metrics_section(db, customer)
    if metrics_text:
        sections.append(metrics_text)
    customer_text = "\n\n".join(sections)
    if retrieve_knowledge:
        knowledge_text, references = build_knowledge_context(query, customer=customer, db=db)

    return ChatContext(
        customer=customer,
        assessment=assessment,
        trend=trend,
        customer_text=customer_text,
        alert_text=build_alert_context(assessment, trend),
        knowledge_text=knowledge_text,
        references=references,
    )
