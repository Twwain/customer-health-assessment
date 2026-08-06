"""LLM 降级兜底。

LLM 不可用时，对话不返回错误页，而是用**量化评估引擎**的既有结果拼出一份
可读的规则化回复：分数、维度明细、预警、规则建议、三层策略照常呈现，
只是不含 AI 推理与知识增强。前端据 ``degraded=true`` 显示黄色降级提示条。
"""

from __future__ import annotations

from .context_builder import TREND_LABEL, ChatContext
from .strategy import build_degraded_strategies, render_strategies_markdown

DEGRADED_BANNER = "> ⚠️ **AI 服务当前不可用，以下为规则引擎兜底结果**（不含知识增强与 AI 推理，恢复后可点「🔄 重新生成」）"

_LEVEL_CN = {"high": "高", "medium": "中", "low": "低"}


def _score_line(ctx: ChatContext) -> str:
    a = ctx.assessment
    if not a:
        return "当前会话未关联客户，无法给出量化结果。"
    line = f"**{a.customer_name}** 当前客情评分 **{a.total_score} / {a.max_score}**，等级「{a.level}」。"
    t = ctx.trend
    if t and t.previous_score is not None:
        line += f"较上次评估 {t.delta:+.1f} 分（{TREND_LABEL.get(t.trend, t.trend)}）。"
    return line


def _dimension_lines(ctx: ChatContext) -> list[str]:
    a = ctx.assessment
    if not a:
        return []
    lines = ["", "### 📊 维度明细"]
    for d in a.dimensions:
        lines.append(f"- **{d.name}** {d.score}/{d.max_score}")
    return lines


def _alert_lines(ctx: ChatContext) -> list[str]:
    a = ctx.assessment
    if not a:
        return []
    lines = ["", "### ⚠️ 风险预警"]
    if a.alerts:
        for alert in a.alerts:
            lines.append(f"- [{_LEVEL_CN.get(alert.level, alert.level)}] {alert.message}")
    else:
        lines.append("- 未触发预警规则")
    return lines


def _trend_lines(ctx: ChatContext) -> list[str]:
    t = ctx.trend
    if not t or not t.points:
        return ["", "### 📈 趋势判断", "- 暂无历史评估记录，本次为首个数据点"]
    series = " → ".join(f"{p.label} {p.total_score}" for p in t.points)
    judgement = {
        "up": "较上次回升，短期向好",
        "down": "较上次下滑，需关注",
        "flat": "基本持平",
    }.get(t.trend, "")
    return ["", "### 📈 趋势判断", f"- 最近 {len(t.points)} 次：{series}", f"- {judgement}"]


def _suggestion_lines(ctx: ChatContext) -> list[str]:
    a = ctx.assessment
    if not a or not a.suggestions:
        return []
    lines = ["", "### ✅ 规则引擎建议"]
    lines += [f"- {s}" for s in a.suggestions]
    return lines


def build_degraded_reply(scenario: str, ctx: ChatContext, question: str = "") -> tuple[str, list[dict]]:
    """返回 ``(Markdown 正文, 结构化策略条目)``。"""
    lines = [DEGRADED_BANNER, ""]
    items: list[dict] = []

    if scenario == "strategy":
        items = build_degraded_strategies(ctx.assessment)
        lines.append("## 📋 策略建议（规则引擎）")
        lines.append(_score_line(ctx))
        lines.append("")
        lines.append(render_strategies_markdown(items))

    elif scenario == "alert_analysis":
        lines.append("## ⚠️ 预警解读（规则引擎）")
        lines.append(_score_line(ctx))
        lines += _alert_lines(ctx)
        lines += _trend_lines(ctx)
        lines += _suggestion_lines(ctx)

    elif scenario == "assessment":
        lines.append("## 📊 评估结论（规则引擎）")
        lines.append(_score_line(ctx))
        lines += _dimension_lines(ctx)
        lines += _alert_lines(ctx)
        lines += _trend_lines(ctx)
        lines += _suggestion_lines(ctx)

    else:  # free_qa
        lines.append("AI 对话服务暂不可用，无法回答自由提问；以下是该客户的量化评估结果，供你先行判断。")
        if question:
            lines.append(f"（你的问题「{question[:60]}」已保留，服务恢复后可点「🔄 重新生成」重试）")
        lines.append("")
        lines.append(_score_line(ctx))
        lines += _alert_lines(ctx)
        lines += _suggestion_lines(ctx)

    return "\n".join(lines).strip(), items
