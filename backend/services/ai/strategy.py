"""策略结构化处理。

本文件负责两件事：
1. 从 LLM 输出里剥离 ```json 代码块，解析成结构化策略条目（前端 StrategyItem 直接渲染）；
2. LLM 不可用时，用规则引擎的预警与建议生成三层降级策略。

M4 接入 LangGraph Agent Loop 后，生成逻辑迁到 graph_builder，
本文件的**解析与降级**能力仍然复用。
"""

from __future__ import annotations

import json
import re
from typing import Any

from schemas import AssessmentResponse
from services.scoring import load_scoring_config

PRIORITIES = ("recommended", "alternative", "long_term")
URGENCIES = ("high", "medium", "low")

PRIORITY_ALIASES = {
    "recommended": "recommended",
    "推荐": "recommended",
    "推荐策略": "recommended",
    "high": "recommended",
    "p0": "recommended",
    "alternative": "alternative",
    "备选": "alternative",
    "备选策略": "alternative",
    "medium": "alternative",
    "p1": "alternative",
    "long_term": "long_term",
    "long-term": "long_term",
    "长期": "long_term",
    "长期建议": "long_term",
    "low": "long_term",
    "p2": "long_term",
}

URGENCY_ALIASES = {
    "high": "high",
    "高": "high",
    "紧急": "high",
    "medium": "medium",
    "mid": "medium",
    "中": "medium",
    "low": "low",
    "低": "low",
}

# 兼容 ```json / ``` 两种围栏；按围栏整体截取后交给 json.loads，
# 支持嵌套对象（如 {"strategies": [...]}），避免非贪婪匹配截断内层花括号
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

_ALERT_PRIORITY = {
    "high": ("recommended", "high"),
    "medium": ("alternative", "medium"),
    "low": ("long_term", "low"),
}


def _norm_priority(value: Any) -> str:
    return PRIORITY_ALIASES.get(str(value or "").strip().lower(), "recommended")


def _norm_urgency(value: Any) -> str:
    return URGENCY_ALIASES.get(str(value or "").strip().lower(), "medium")


def normalize_item(raw: dict) -> dict | None:
    """把任意形态的策略字典规整成  约定的字段集。"""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or raw.get("name") or "").strip()
    if not title:
        return None
    return {
        "priority": _norm_priority(raw.get("priority") or raw.get("level")),
        "title": title,
        "urgency": _norm_urgency(raw.get("urgency")),
        "reason": str(raw.get("reason") or "").strip(),
        "action": str(raw.get("action") or "").strip(),
        "expected_outcome": str(
            raw.get("expected_outcome") or raw.get("expected") or raw.get("outcome") or ""
        ).strip(),
        "reference": str(raw.get("reference") or raw.get("ref") or "").strip(),
    }


def _sort_key(item: dict) -> tuple[int, int]:
    return (
        PRIORITIES.index(item["priority"]) if item["priority"] in PRIORITIES else 9,
        URGENCIES.index(item["urgency"]) if item["urgency"] in URGENCIES else 9,
    )


def _extract_items(payload: object) -> list:
    """从解析后的 JSON payload 提取策略条目：list 原样返回，dict 兼容多个键名。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("strategies", "items", "strategy_items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def split_strategy_payload(text: str) -> tuple[str, list[dict]]:
    """返回 ``(展示正文, 结构化策略列表)``。

    正文里的 json 代码块只用于机器解析，不应展示给用户，因此会被剥离。
    解析失败时返回原文 + 空列表——宁可少结构化，也不能把对话搞崩。
    """
    if not text:
        return "", []

    items: list[dict] = []
    body = text
    for match in list(_JSON_FENCE_RE.finditer(text)):
        snippet = match.group(1).strip()
        try:
            payload = json.loads(snippet)
        except json.JSONDecodeError:
            # 解析失败的围栏块同样从展示正文剥离，避免把原始 JSON 暴露给用户
            body = body.replace(match.group(0), "")
            continue

        raw_items = _extract_items(payload)
        if not raw_items:
            continue

        for raw in raw_items:
            item = normalize_item(raw)
            if item:
                items.append(item)
        body = body.replace(match.group(0), "")

    # 容错：输出被 max_tokens 截断导致围栏未闭合时，尝试解析从最后一个 ```json 到末尾的片段
    if not items:
        fence_m = re.search(r"(?:^|\n)```json", text)
        if fence_m is not None:
            # match.end() 指向围栏结束（含前导换行），紧凑写法 ```json{...} 也不丢字符
            tail = text[fence_m.end() :].strip()
            head = text[: fence_m.start()].rstrip()
            if tail:
                try:
                    # 容忍 JSON 块后附带说明文字（如"本页面仅显示策略摘要…"）
                    payload, end = json.JSONDecoder().raw_decode(tail)
                except json.JSONDecodeError:
                    payload = None
                if payload is not None:
                    raw_items = _extract_items(payload)
                    for raw in raw_items or []:
                        item = normalize_item(raw)
                        if item:
                            items.append(item)
                    # 剔除未闭合代码块与其中的 JSON，但保留其后附带的说明文字
                    note = tail[end:].strip()
                    body = head
                    if note:
                        body = f"{head}\n\n{note}" if head else note
                else:
                    # 解析失败：整个未闭合围栏尾巴从展示正文剔除，避免暴露残缺 JSON
                    body = head
            else:
                # 围栏后没有任何内容：也把空围栏从展示正文剔除
                body = head

    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    items.sort(key=_sort_key)
    return body, items


# ── 降级生成（LLM 不可用）────────────────────────────────────────────────────


def build_degraded_strategies(assessment: AssessmentResponse | None) -> list[dict]:
    """用规则引擎的预警 + 建议拼出三层策略，字段与 LLM 输出保持一致。"""
    if assessment is None:
        return []

    rules = {rule.id: rule for rule in load_scoring_config().alerts}
    items: list[dict] = []

    for alert in assessment.alerts:
        priority, urgency = _ALERT_PRIORITY.get(alert.level, ("alternative", "medium"))
        rule = rules.get(alert.id)
        suggestion = (rule.suggestion if rule else "") or "由客户经理结合现场情况制定具体动作"
        items.append(
            {
                "priority": priority,
                "title": suggestion.replace("建议", "", 1).strip() or alert.message,
                "urgency": urgency,
                "reason": alert.message,
                "action": suggestion,
                "expected_outcome": "消除该项预警，对应维度分数回升",
                "reference": "规则引擎（LLM 不可用时的兜底建议）",
            }
        )

    # 机会类建议（如高增长潜力）落到长期建议层
    alert_suggestions = {i["action"] for i in items}
    for suggestion in assessment.suggestions:
        if suggestion in alert_suggestions:
            continue
        items.append(
            {
                "priority": "long_term",
                "title": suggestion.replace("建议", "", 1).strip() or suggestion,
                "urgency": "low",
                "reason": "规则引擎识别到的机会点",
                "action": suggestion,
                "expected_outcome": "扩大合作面，提升商业价值维度得分",
                "reference": "规则引擎（LLM 不可用时的兜底建议）",
            }
        )

    if not items:
        items.append(
            {
                "priority": "long_term",
                "title": "保持现有服务节奏并持续观察",
                "urgency": "low",
                "reason": f"当前客情评分 {assessment.total_score}，未触发任何预警规则",
                "action": "按既定周期回访，关注满意度与回款两项先行指标",
                "expected_outcome": "维持健康度不下滑",
                "reference": "规则引擎（LLM 不可用时的兜底建议）",
            }
        )

    items.sort(key=_sort_key)
    return items


_GROUP_TITLES = {
    "recommended": "✅ 推荐策略",
    "alternative": "□ 备选策略",
    "long_term": "💡 长期建议",
}
_URGENCY_CN = {"high": "高", "medium": "中", "low": "低"}


def render_strategies_markdown(items: list[dict]) -> str:
    """把结构化策略渲染成精简 Markdown（降级回复复用，每条一行标题 + 行动）。"""
    if not items:
        return ""

    lines: list[str] = []
    index = 1
    for priority in PRIORITIES:
        group = [i for i in items if i["priority"] == priority]
        if not group:
            continue
        lines.append(f"### {_GROUP_TITLES[priority]}")
        for item in group:
            action = item.get("action") or ""
            suffix = f"：{action}" if action else ""
            lines.append(
                f"{index}. **{item['title']}**（紧急度：{_URGENCY_CN.get(item['urgency'], '中')}）{suffix}"
            )
            index += 1
        lines.append("")

    return "\n".join(lines).strip()


# ── M4 Agent Loop 接入────────────────────────────────────────────


def generate(
    scenario: str,
    ctx,
    customer,
    db,
    *,
    adapter=None,
    question: str = "",
    max_iterations: int = 2,
    tools_enabled: bool = True,
    embed_func=None,
    store=None,
    on_event=None,
    thinking_enabled: bool = False,
    retrieve_enabled: bool = True,
    cancel_event=None,
):
    """运行评估 / 策略 Agent Loop，返回 ``graph_builder.AgentResult``。

    由 ``chat_engine`` 在 assessment / strategy / alert_analysis 场景下调用；
    解析与降级能力仍复用本文件的 ``split_strategy_payload`` / ``build_degraded_*``。
    """
    from .graph_builder import AssessmentStrategyAgent

    agent = AssessmentStrategyAgent(
        adapter=adapter, max_iterations=max_iterations, tools_enabled=tools_enabled,
        on_event=on_event,
        thinking_enabled=thinking_enabled,
        cancel_event=cancel_event,
    )
    return agent.run(
        scenario,
        ctx,
        customer,
        db,
        question=question,
        embed_func=embed_func,
        store=store,
        retrieve_enabled=retrieve_enabled,
    )
