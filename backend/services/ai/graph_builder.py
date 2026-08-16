"""评估 / 策略 Agent Loop。

用状态图编排 reason → retrieve → critique → refine 的多轮推理循环：
- reason：基于量化事实 + 知识检索，调用 LLM 生成评估结论 / 策略
- retrieve：RAG 工具召回知识（Agent 自带检索能力，而非仅依赖上下文注入）
- critique：自批判（启发式，可升级为 LLM 批判），检测缺策略块 / 缺引用
- refine：根据批判精炼，最多迭代 ``max_iterations`` 次，带死循环检测
- 任意一步 LLM 不可用 → 降级为规则引擎兜底，不中断对话

知识溯源：retrieve 命中的切片经 ``tools.to_reference`` 转为引用，随结论返回，
供前端 📎 溯源抽屉定位原文。

设计说明（偏差，同 Step 4 思路）：原方案指定 LangGraph 作为编排框架。本期以
**自包含的轻量状态机**实现等价逻辑——LangGraph 为重量级依赖、不利于轻量自包含，
且自包含实现更易测试、不引入重量级依赖；节点语义一致
（reason / retrieve / critique / refine），后续可平滑替换为 ``langgraph.StateGraph``
而无需改动 ``tools`` / ``chat_engine``。
"""

from __future__ import annotations

import datetime
import time
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
    timings: dict[str, Any] = field(default_factory=dict)


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

    def __init__(
        self, adapter=None, max_iterations: int = 2, tools_enabled: bool = True,
        on_event=None, thinking_enabled: bool = False, cancel_event=None,
    ) -> None:
        self._adapter = adapter or get_chat_adapter()
        self._max = max(1, max_iterations)
        self._tools_enabled = tools_enabled
        self._on_event = on_event
        self._thinking_enabled = thinking_enabled
        self._cancel_event = cancel_event
        self._tool_warning = ""
        self._llm_calls: list[dict[str, Any]] = []
        self._tool_rounds = 0
        self._refine_rounds = 0
        self._tool_ms = 0
        self._retrieve_ms = 0
        self._rag_timings: dict[str, int] = {}

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise LLMError("generation cancelled")

    def _consume_stream(
        self, stream, *, phase: str, messages, max_tokens: int, usage: dict[str, Any]
    ) -> str:
        """消费一次模型流并记录 TTFT/总耗时；不改变现有缓冲行为。"""
        started = time.monotonic()
        first_delta_ms: int | None = None
        chunks: list[str] = []
        try:
            for delta in stream:
                self._check_cancelled()
                if first_delta_ms is None:
                    first_delta_ms = int((time.monotonic() - started) * 1000)
                chunks.append(delta)
                if self._on_event:
                    self._on_event("delta", {"text": delta, "phase": phase})
            self._check_cancelled()
            return "".join(chunks)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            self._llm_calls.append(
                {
                    "phase": phase,
                    "ttft_ms": first_delta_ms,
                    "duration_ms": duration_ms,
                    "message_count": len(messages),
                    "prompt_chars": sum(
                        len(str(m.content if isinstance(m, LLMMessage) else m.get("content", "")))
                        for m in messages
                    ),
                    "output_chars": sum(len(c) for c in chunks),
                    "max_tokens": max_tokens,
                    "thinking_enabled": self._thinking_enabled,
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": completion_tokens,
                    "total_tokens": int(usage.get("total_tokens") or 0),
                    "completion_tokens_per_second": (
                        round(completion_tokens * 1000 / duration_ms, 2)
                        if completion_tokens and duration_ms else None
                    ),
                }
            )

    # ── 节点 ────────────────────────────────────────────────────────────────
    def _call_llm(self, messages: list[LLMMessage], template) -> str:
        usage: dict[str, Any] = {}
        max_tokens = min(template.max_tokens, config.LLM_MAX_TOKENS)
        stream = self._adapter.stream_chat_completion(
            messages,
            temperature=template.temperature,
            max_tokens=max_tokens,
            on_usage=lambda value: usage.update(value),
            extra={"thinking": {"type": "enabled" if self._thinking_enabled else "disabled"}},
        )
        return self._consume_stream(
            stream, phase="refine", messages=messages, max_tokens=max_tokens, usage=usage
        )

    def _reason(
        self, ctx, references, question, today, template, *, customer=None, db=None, tools_enabled=None
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
            tools_enabled=tools_enabled,
        )

    def _call_llm_with_tools(
        self, messages, template, *, customer=None, db=None, exclude_ids=None, tools_enabled=None
    ) -> tuple[str, list[dict]]:
        """生成正文；模型请求工具时执行并回填结果，返回 (正文, 工具引用)。"""
        tool_rounds = 0
        refs: list[dict] = []
        exclude = set(exclude_ids or [])
        tools_enabled = self._tools_enabled if tools_enabled is None else tools_enabled
        while True:
            tool_calls: list[dict] = []
            usage: dict[str, Any] = {}
            max_tokens = min(template.max_tokens, config.LLM_MAX_TOKENS)
            stream = self._adapter.stream_chat_completion(
                messages,
                temperature=template.temperature,
                max_tokens=max_tokens,
                tools=tools.TOOL_SCHEMAS if tools_enabled else None,
                on_tool_calls=lambda tcs: tool_calls.extend(tcs),
                on_usage=lambda value: usage.update(value),
                extra={"thinking": {"type": "enabled" if self._thinking_enabled else "disabled"}},
            )
            phase = "reason" if tool_rounds == 0 else "tool_followup"
            text = self._consume_stream(
                stream, phase=phase, messages=messages, max_tokens=max_tokens, usage=usage
            )
            self._check_cancelled()
            if not tool_calls:
                return text, refs
            if text and self._on_event:
                self._on_event("replace", {"text": "", "reason": "tool_followup"})
            tool_rounds += 1
            self._tool_rounds += 1
            tool_started = time.monotonic()
            messages, round_refs = tools.append_tool_results(
                messages, tool_calls, customer=customer, db=db, exclude_ids=exclude
            )
            self._tool_ms += int((time.monotonic() - tool_started) * 1000)
            refs.extend(round_refs)
            exclude.update(r["id"] for r in round_refs)
            if tool_rounds >= tools.MAX_TOOL_ROUNDS:
                # 已回填本轮工具结果：不带工具收尾一次，避免空回复
                self._tool_warning = "工具调用次数已达上限，已基于已获取信息作答"
                usage = {}
                stream = self._adapter.stream_chat_completion(
                    messages,
                    temperature=template.temperature,
                    max_tokens=max_tokens,
                    tools=None,
                    on_usage=lambda value: usage.update(value),
                    extra={"thinking": {"type": "enabled" if self._thinking_enabled else "disabled"}},
                )
                final_text = self._consume_stream(
                    stream, phase="tool_final", messages=messages,
                    max_tokens=max_tokens, usage=usage,
                )
                # 当前工具轮的草稿已经通过 replace 从客户端清除；收尾请求
                # 才是基于工具结果生成的正式答案，持久化时也只保留它。
                return final_text, refs

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
        retrieve_enabled: bool = True,
    ) -> AgentResult:
        self._tool_warning = ""
        self._llm_calls = []
        self._tool_rounds = 0
        self._refine_rounds = 0
        self._tool_ms = 0
        self._retrieve_ms = 0
        self._rag_timings = {}
        max_iter = max_iterations or self._max
        today = datetime.date.today().isoformat()
        template = get_template(scenario)
        warnings: list[str] = []

        # ⓪ 量化工具：对话路径下评分/画像已由 chat_engine
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
        if retrieve_enabled:
            self._check_cancelled()
            retrieve_started = time.monotonic()
            retrievals = tools.rag_retrieve(
                question, customer, db, k=5, embed_func=embed_func, store=store,
                timings=self._rag_timings,
            )
            self._retrieve_ms = int((time.monotonic() - retrieve_started) * 1000)
            references = [tools.to_reference(c) for c in retrievals]
        else:
            references = list(ctx.references)
        self._check_cancelled()

        # ② reason
        try:
            draft, reason_refs = self._reason(
                ctx, references, question, today, template, customer=customer, db=db
            )
            if not draft.strip():
                # 空草稿重试一次（不带工具）：避免模型偶发空响应直接带崩整个流程
                self._tool_warning = "首次生成返回为空，已自动重试"
                retry_draft, retry_refs = self._reason(
                    ctx,
                    references,
                    question,
                    today,
                    template,
                    customer=customer,
                    db=db,
                    tools_enabled=False,
                )
                draft = retry_draft
                reason_refs = list(reason_refs) + retry_refs
            known = {r.get("id") for r in references}
            references = list(references) + [
                r for r in reason_refs if r.get("id") not in known
            ]
        except (LLMUnavailableError, LLMError) as exc:
            return self._degrade(scenario, ctx, question, references, exc)

        if not draft.strip():
            return self._degrade(
                scenario,
                ctx,
                question,
                references,
                LLMUnavailableError("AI 连续多次返回空内容"),
            )

        # ③ critique → ④ refine 循环（带死循环检测）
        # 检查项按场景裁剪：仅 strategy 要求 json 策略块；仅检索有命中时要求知识引用
        has_knowledge = bool(references)
        text = draft
        iterations = 1
        critique_text = tools.critique(text, ctx, scenario=scenario, has_knowledge=has_knowledge)
        while tools.needs_refine(critique_text) and iterations < max_iter:
            iterations += 1
            self._refine_rounds += 1
            if self._on_event:
                self._on_event("replace", {"text": "", "reason": "refine"})
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

        if not text.strip():
            return self._degrade(
                scenario,
                ctx,
                question,
                references,
                LLMUnavailableError("AI 连续多次返回空内容"),
            )

        return AgentResult(
            text=text,
            references=references,
            degraded=False,
            degraded_items=[],
            warnings=warnings,
            iterations=iterations,
            model=getattr(self._adapter, "model", ""),
            timings=self._timings(),
        )

    def _timings(self) -> dict[str, Any]:
        return {
            "rag_ms": self._retrieve_ms,
            **self._rag_timings,
            "llm_calls": list(self._llm_calls),
            "llm_call_count": len(self._llm_calls),
            "tool_rounds": self._tool_rounds,
            "tool_ms": self._tool_ms,
            "refine_rounds": self._refine_rounds,
        }

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
            timings=self._timings(),
        )
