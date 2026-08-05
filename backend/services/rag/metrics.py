"""结构化知识指标服务（SOW §3.3.1 知识分层 / §5 KnowledgeMetric）。

行业基准值、续约率、客户画像统计等**精确数值**不进向量库，评估时按客户
行业 / 规模 / 地域精确查询，避免语义检索造成数值漂移。

本模块提供指标的精确查询、幂等写入、删除与 Prompt 注入格式化；
对话上下文由 ``services.ai.context_builder`` 在构建客户上下文时注入。
"""

from __future__ import annotations

from typing import Any

from models import KNOWLEDGE_CATEGORIES, KnowledgeMetric

# 指标分类限定为指标类（不接收"内部规范"/"对话沉淀"，叙事文本进向量库）
_METRIC_CATEGORIES = tuple(c for c in KNOWLEDGE_CATEGORIES if c.endswith("指标"))


def query_metrics(
    db: Any,
    *,
    industry: str | None = None,
    metric_key: str = "",
    dimension_key: str = "",
    global_only: bool = False,
    limit: int = 20,
) -> list[KnowledgeMetric]:
    """精确查询指标。

    - ``industry`` 非空：返回该行业指标 + 跨行业通用指标（industry 为空的记录），
      行业特定指标排在通用指标之前
    - ``global_only=True``：仅返回跨行业通用指标（客户无行业时的注入口径）
    - 两者都不传：返回全部指标（管理端列表口径）
    """
    q = db.query(KnowledgeMetric)
    if global_only:
        q = q.filter(KnowledgeMetric.industry == "")
    elif industry:
        q = q.filter(KnowledgeMetric.industry.in_([industry, ""]))
    if metric_key:
        q = q.filter(KnowledgeMetric.metric_key == metric_key)
    if dimension_key:
        q = q.filter(KnowledgeMetric.dimension_key == dimension_key)
    # industry 降序：非空行业名排在空串（通用指标）之前
    return q.order_by(KnowledgeMetric.industry.desc(), KnowledgeMetric.metric_key).limit(limit).all()


def upsert_metric(db: Any, **fields: Any) -> KnowledgeMetric:
    """按 (metric_key, industry, region, scale, period) 幂等写入：存在则更新，否则新建。"""
    category = fields.get("category") or "内部指标"
    if category not in _METRIC_CATEGORIES:
        raise ValueError(
            f"无效的指标分类：{category!r}，可选值：{' / '.join(_METRIC_CATEGORIES)}"
        )
    fields["category"] = category

    row = (
        db.query(KnowledgeMetric)
        .filter(
            KnowledgeMetric.metric_key == fields["metric_key"],
            KnowledgeMetric.industry == fields.get("industry", ""),
            KnowledgeMetric.region == fields.get("region", ""),
            KnowledgeMetric.scale == fields.get("scale", ""),
            KnowledgeMetric.period == fields.get("period", ""),
        )
        .first()
    )
    if row is None:
        row = KnowledgeMetric(**fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def delete_metric(db: Any, metric_id: int) -> bool:
    row = db.get(KnowledgeMetric, metric_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def format_metrics_context(metrics: list[KnowledgeMetric]) -> str:
    """渲染为 Prompt 注入的 Markdown 段落；空列表返回空串（不注入）。"""
    if not metrics:
        return ""
    lines = ["## 行业基准指标（结构化精确数据，可作为对比基线）"]
    for m in metrics:
        scope = "、".join(x for x in (m.industry or "全行业", m.region, m.scale) if x)
        period = f"（{m.period}）" if m.period else ""
        lines.append(f"- {m.metric_name or m.metric_key}：{m.metric_value}{m.unit}　[{scope}]{period}")
    return "\n".join(lines)
