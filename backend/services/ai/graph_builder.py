"""评估 / 策略 Agent Loop（SOW §3.4 / §4.2 services/ai/graph_builder.py）。

用状态图编排 reason → retrieve → critique → refine 的多轮推理循环：
- reason：基于量化事实 + 知识检索，调用 LLM 生成评估结论 / 策略
- retrieve：RAG 工具召回知识（Agent 自带检索能力，而非仅依赖上下文注入）
- critique：自批判（启发式，可升级为 LLM 批判），检测缺策略块 / 缺引用
- refine：根据批判精炼，最多迭代 ``max_iterations`` 次，带死循环检测
- 任意一步 LLM 不可用 → 降级为规则引擎兜底（SOW §7），不中断对话

知识溯源：retrieve 命中的切片经 ``tools.to_reference`` 转为引用，随结论返回，
供前端 📎 溯源抽屉定位原文（SOW §3.4 结构化输出 + 知识溯源）。

设计说明（偏差，同 Step 4 思路）：SOW 指定 LangGraph 作为编排框架。本期以
**自包含的轻量状态机**实现等价逻辑——LangGraph 为重量级依赖、不利于轻量自包含，
且自包含实现更易测试、不引入重量级依赖；节点语义与 SOW 一致
（reason / retrieve / critique / refine），后续可平滑替换为 ``langgraph.StateGraph``
而无需改动 ``tools`` / ``chat_engine``。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

import config
from models import Customer
from services.ai.context_builder import ChatContext
from services.ai.fallback import build_degraded_reply
from services.ai.llm_adapter import (
    LLMError,
    LLMMessage,
    LLMUnavailableError,
    get_chat_adapter,
)
from services.ai.prompt_templates import get_template
from . import tools


@dataclass
class AgentResult:
    text: str
    references: list[dict]
    degraded: bool
    degraded_items: list[dict]
    warnings: list[str]
    iterations: int
    model: str


def _format_retrievals(references: list[dict]) -> str:
    if not references:
        return ""
    lines = ["## 知识库参考资料（RAG 检索命中，作答时请标注来源）"]
    for i, ref in enumerate(references, 1):
        lines.append(f"### 参考 {i}（{ref.get('category', '')} · 《{ref.get('title', '')}》）")
        lines.append(ref.get("snippet", "") or "")
    return "\n\n".join(lines)


class AssessmentStrategyAgent:
    """评估 / 策略 Agent：检索增强 + 自批判精炼。"""

    def __init__(self, adapter=None, max_iterations: int = 2) -> None:
        self._adapter = adapter or get_chat_adapter()
        self._max = max(1, max_iterations)
        self._tool_warning = ""

    # ── 节点 ────────────────────────────────────────────────────────────────
    def _call_llm(self, messages: list[LLMMessage], template) -> str:
        chunks: list[str] = []
        stream = self._adapter.stream_chat_completion(
            messages,
            temperature=template.temperature,
            max_tokens=template.max_tokens,
        )
        for delta in stream:
            chunks.append(delta)
        return "".join(chunks)

    def _reason(
        self, ctx, references, question, today, template, *, customer=None, db=None
    ) -> tuple[str, list[dict]]:
        system_text = template.render_system(today=today)
        knowledge = _format_retrievals(references) or ctx.knowledge_text
        user_text = template.render_user(
            today=today,
            customer_context=ctx.customer_text,
            knowledge_context=knowledge,
            alert_context=ctx.alert_text,
            question=question,
        )
        exclude_ids = {ref.get("id") for ref in references}
        return self._call_llm_with_tools(
            [LLMMessage(role="system", content=system_text), LLMMessage(role="user", content=user_text)],
            template,
            customer=customer,
            db=db,
            exclude_ids=exclude_ids,
        )

    def _call_llm_with_tools(
        self, messages, template, *, customer=None, db=None, exclude_ids=None
    ) -> tuple[str, list[dict]]:
        """生成正文；模型请求工具时执行并回填结果，返回 (正文, 工具引用)。"""
        tool_rounds = 0
        refs: list[dict] = []
        exclude = set(exclude_ids or [])
        tools_enabled = config.LLM_TOOLS_ENABLED
        while True:
            tool_calls: list[dict] = []
            stream = self._adapter.stream_chat_completion(
                messages,
                temperature=template.temperature,
                max_tokens=template.max_tokens,
                tools=tools.TOOL_SCHEMAS if tools_enabled else None,
                on_tool_calls=lambda tcs: tool_calls.extend(tcs),
            )
            chunks: list[str] = []
            for delta in stream:
                chunks.append(delta)
            if not tool_calls:
                return "".join(chunks), refs
            tool_rounds += 1
            messages, round_refs = tools.append_tool_results(
                messages, tool_calls, customer=customer, db=db, exclude_ids=exclude
            )
            refs.extend(round_refs)
            exclude.update(r["id"] for r in round_refs)
            if tool_rounds >= tools.MAX_TOOL_ROUNDS:
                # 已回填本轮工具结果：不带工具收尾一次，避免空回复
                self._tool_warning = "工具调用次数已达上限，已基于已获取信息作答"
                stream = self._adapter.stream_chat_completion(
                    messages,
                    temperature=template.temperature,
                    max_tokens=template.max_tokens,
                    tools=None,
                )
                for delta in stream:
                    chunks.append(delta)
                return "".join(chunks), refs

    def _refine(self, ctx, references, draft, critique_text, question, today, template, scenario: str = "strategy") -> str:
        system_text = template.render_system(today=today)
        knowledge = _format_retrievals(references) or ctx.knowledge_text
        format_hint = (
            "保留 Markdown 正文与 ```json 策略块。"
            if scenario == "strategy"
            else "保留原输出结构（Markdown 正文）。"
        )
        refine_prompt = (
            "你上一轮的草稿如下：\n"
            "---\n"
            f"{draft}\n"
            "---\n"
            "草稿存在的问题：\n"
            f"{critique_text}\n\n"
            f"请基于原始要求输出改进后的完整回答（{format_hint}）"
        )
        user_text = template.render_user(
            today=today,
            customer_context=ctx.customer_text,
            knowledge_context=knowledge,
            alert_context=ctx.alert_text,
            question=f"{question}\n\n{refine_prompt}",
        )
        return self._call_llm(
            [LLMMessage(role="system", content=system_text), LLMMessage(role="user", content=user_text)],
            template,
        )

    # ── 编排 ────────────────────────────────────────────────────────────────
    def run(
        self,
        scenario: str,
        ctx: ChatContext,
        customer: Customer | None,
        db: Any | None,
        *,
        question: str = "",
        embed_func=None,
        store=None,
        max_iterations: int | None = None,
    ) -> AgentResult:
        self._tool_warning = ""
        max_iter = max_iterations or self._max
        today = datetime.date.today().isoformat()
        template = get_template(scenario)
        warnings: list[str] = []

        # ⓪ 量化工具（SOW §3.4.1 工具②③）：对话路径下评分/画像已由 chat_engine
        # 注入 ctx；Agent 独立调用（测试 / 报告整合 / 批处理）时由工具自行补齐
        if customer is not None:
            if ctx.assessment is None:
                ctx.assessment = tools.score_query(customer, db)
            if not ctx.customer_text:
                profile = tools.profile_query(customer)
                ctx.customer_text = "## 客户画像（工具查询）\n" + "；".join(
                    f"{k}={v}" for k, v in profile.items()
                )

        # ① retrieve（Agent 自带 RAG 工具）
        retrievals = tools.rag_retrieve(
            question, customer, db, k=5, embed_func=embed_func, store=store
        )
        references = [tools.to_reference(c) for c in retrievals]

        # ② reason
        try:
            draft, reason_refs = self._reason(
                ctx, references, question, today, template, customer=customer, db=db
            )
            known = {r.get("id") for r in references}
            references = list(references) + [
                r for r in reason_refs if r.get("id") not in known
            ]
        except (LLMUnavailableError, LLMError) as exc:
            return self._degrade(scenario, ctx, question, references, exc)

        # ③ critique → ④ refine 循环（带死循环检测）
        # 检查项按场景裁剪：仅 strategy 要求 json 策略块；仅检索有命中时要求知识引用
        has_knowledge = bool(references)
        text = draft
        iterations = 1
        critique_text = tools.critique(text, ctx, scenario=scenario, has_knowledge=has_knowledge)
        while tools.needs_refine(critique_text) and iterations < max_iter:
            iterations += 1
            try:
                text = self._refine(
                    ctx, references, draft, critique_text, question, today, template, scenario=scenario
                )
            except (LLMUnavailableError, LLMError) as exc:
                return self._degrade(scenario, ctx, question, references, exc)
            critique_text = tools.critique(text, ctx, scenario=scenario, has_knowledge=has_knowledge)
            draft = text

        if tools.needs_refine(critique_text):
            warnings.append(f"草稿精炼达到 {max_iter} 次上限，输出可能未完全满足：{critique_text.strip()}")
        if self._tool_warning:
            warnings.append(self._tool_warning)

        return AgentResult(
            text=text,
            references=references,
            degraded=False,
            degraded_items=[],
            warnings=warnings,
            iterations=iterations,
            model=getattr(self._adapter, "model", ""),
        )

    def _degrade(self, scenario, ctx, question, references, exc) -> AgentResult:
        text, items = build_degraded_reply(scenario, ctx, question)
        return AgentResult(
            text=text,
            references=references,
            degraded=True,
            degraded_items=items,
            warnings=[f"LLM 不可用，已降级为规则引擎：{exc}"],
            iterations=0,
            model="",
        )
