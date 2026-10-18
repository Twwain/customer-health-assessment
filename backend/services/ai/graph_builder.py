"""评估 / 策略 Agent Loop。

用状态图编排 reason → retrieve → critique → refine 的多轮推理循环：
- reason：基于量化事实 + 知识检索，调用 LLM 生成评估结论 / 策略
- retrieve：RAG 工具召回知识（Agent 自带检索能力，而非仅依赖上下文注入）
- critique：本地格式检查 + 独立模型对照原文核对关键事实
- refine：只在发现问题时修订一次，修订后再核对，不继续循环
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
import json
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
    estimate_tokens,
    get_chat_adapter,
    stream_chat_completion_controlled,
)
from services.ai.prompt_templates import get_internal_prompt, get_template
from . import tools, fact_check, local_quality


def _thinking_extra(enabled: bool, effort: str | None = None) -> dict[str, Any]:
    extra = {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if enabled and effort:
        extra["reasoning_effort"] = effort
    return extra


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
    remaining = config.RAG_MODEL_CONTEXT_CHAR_BUDGET
    for i, ref in enumerate(references, 1):
        if remaining <= 0:
            break
        lines.append(f"### 参考 {i}（{ref.get('category', '')} · 《{ref.get('title', '')}》）")
        content = ref.get("_model_content") or ref.get("snippet", "") or ""
        content = content[:remaining]
        remaining -= len(content)
        lines.append(tools.guardrails.wrap_untrusted(content, "rag"))
    return "\n\n".join(lines)


def _display_references(references: list[dict]) -> list[dict]:
    """移除只供模型使用的正文，前端和消息记录只接收 Reference DTO。"""
    return [{k: v for k, v in ref.items() if not k.startswith("_")} for ref in references]


class AssessmentStrategyAgent:
    """评估 / 策略 Agent：检索增强 + 自批判精炼。"""

    def __init__(
        self, adapter=None, max_iterations: int = 2, tools_enabled: bool = True,
        on_event=None, thinking_enabled: bool = False, cancel_event=None,
        deadline: float | None = None,
        reasoning_effort: str | None = None, output_token_limit: int | None = None,
        token_budget: int | None = None,
    ) -> None:
        self._adapter = adapter or get_chat_adapter()
        self._max = min(2, max(1, max_iterations))
        self._tools_enabled = tools_enabled
        self._on_event = on_event
        self._thinking_enabled = thinking_enabled
        self._cancel_event = cancel_event
        self._deadline = deadline
        self._reasoning_effort = reasoning_effort
        self._output_token_limit = output_token_limit
        self._token_budget = token_budget if token_budget is not None else config.CHAT_TURN_TOKEN_BUDGET
        self._verification: list[dict] = []
        self._tool_warning = ""
        self._llm_calls: list[dict[str, Any]] = []
        self._tool_rounds = 0
        self._refine_rounds = 0
        self._tool_ms = 0
        self._retrieve_ms = 0
        self._rag_timings: dict[str, int] = {}
        self._total_tokens = 0
        self._token_budget_exceeded = False
        self._budget_finalized = False
        self._tool_budget = tools.ToolBudget()
        self._local_repairs: list[dict] = []

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise LLMError("generation cancelled")
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise LLMError("AI 生成超过总时限，已取消")

    def _consume_stream(
        self, stream, *, phase: str, messages, max_tokens: int, usage: dict[str, Any],
        visible: bool = True,
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
                if visible and self._on_event:
                    self._on_event("delta", {"text": delta, "phase": phase})
            self._check_cancelled()
            return "".join(chunks)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            reported_tokens = int(usage.get("total_tokens") or 0)
            estimated = reported_tokens == 0
            call_tokens = reported_tokens or estimate_tokens(
                "\n".join(
                    str(m.content if isinstance(m, LLMMessage) else m.get("content", ""))
                    for m in messages
                )
                + "".join(chunks)
            )
            self._total_tokens += call_tokens
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
                    "thinking_enabled": self._thinking_enabled if visible else False,
                    "reasoning_effort": self._reasoning_effort if visible and self._thinking_enabled else None,
                    "finish_reason": usage.get("finish_reason"),
                    "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": completion_tokens,
                    "total_tokens": call_tokens,
                    "tokens_estimated": estimated,
                    "completion_tokens_per_second": (
                        round(completion_tokens * 1000 / duration_ms, 2)
                        if completion_tokens and duration_ms else None
                    ),
                }
            )
            self._token_budget_exceeded = (
                self._total_tokens >= self._token_budget
            )
            if self._on_event:
                self._on_event(
                    "usage",
                    {
                        "tokens_used": self._total_tokens,
                        "token_budget_exceeded": self._token_budget_exceeded,
                        "tokens_estimated": estimated,
                        "llm_call": self._llm_calls[-1],
                    },
                )

    # ── 节点 ────────────────────────────────────────────────────────────────
    def _generation_limit(self, template) -> int:
        return self._output_token_limit or min(template.max_tokens, config.LLM_MAX_TOKENS)

    def _max_tokens(self, template) -> int:
        # 探索使用剩余软预算；最终回答可独立使用一次该场景的生成额度。
        return max(1, min(self._generation_limit(template), self._token_budget - self._total_tokens))

    def _finish_answer(self, messages, template, *, draft: str = "") -> str:
        self._check_cancelled()
        over_budget = self._token_budget_exceeded
        if self._budget_finalized:
            raise LLMError("本轮已完成预算收尾，不再追加模型调用")
        if over_budget:
            self._budget_finalized = True
            self._tool_warning = "本轮 token 预算已用尽，已停止探索并使用独立额度完成最终回答"
        final_messages = list(messages)
        if draft:
            final_messages.append(LLMMessage(
                role="assistant", content=tools.guardrails.wrap_untrusted(draft, "tool")
            ))
        final_messages.append(LLMMessage(
            role="user", content=get_internal_prompt("finish_answer", format_hint="保留要求的完整输出结构；")
        ))
        if self._on_event:
            self._on_event("replace", {"text": "", "reason": "final_answer"})
        usage = {}
        limit = self._generation_limit(template)
        stream = stream_chat_completion_controlled(
            self._adapter, final_messages, temperature=template.temperature,
            max_tokens=limit, tools=None, on_usage=lambda value: usage.update(value),
            extra=_thinking_extra(self._thinking_enabled, self._reasoning_effort),
            cancel_event=self._cancel_event, deadline=self._deadline,
        )
        text = self._consume_stream(
            stream, phase="budget_final" if over_budget else "tool_final",
            messages=final_messages, max_tokens=limit, usage=usage,
        )
        self._budget_finalized = self._token_budget_exceeded
        return text

    def _verify(self, draft, ctx, references) -> fact_check.FactCheckResult:
        if self._token_budget_exceeded:
            result = fact_check.FactCheckResult("unverified", reason="本次模型调用预算已用尽")
        elif len(draft) > config.AI_FACT_CHECK_MAX_DRAFT_CHARS:
            result = fact_check.FactCheckResult("unverified", reason="正文超过核对长度上限，未截断后冒充完整核对")
        else:
            if self._on_event:
                self._on_event("stage", {"stage": "verification", "message": "正在依据原文核对关键事实"})
            sources = fact_check.build_sources(ctx, references)
            usage = {}
            limit = max(1, min(config.AI_FACT_CHECK_MAX_TOKENS, self._token_budget - self._total_tokens))
            deadline = time.monotonic() + config.AI_FACT_CHECK_TIMEOUT_SECONDS
            if self._deadline is not None:
                deadline = min(deadline, self._deadline - 0.5)
            try:
                messages = fact_check.messages_for_check(draft, sources)
                stream = stream_chat_completion_controlled(
                    self._adapter, messages, temperature=0, max_tokens=limit,
                    tools=None, extra=_thinking_extra(False),
                    on_usage=lambda value: usage.update(value),
                    cancel_event=self._cancel_event, deadline=deadline,
                )
                text = self._consume_stream(stream, phase="verification", messages=messages,
                                            max_tokens=limit, usage=usage, visible=False)
                if time.monotonic() >= deadline or usage.get("finish_reason") == "length":
                    result = fact_check.FactCheckResult("unverified", reason="核对时间或输出预算已耗尽")
                else:
                    result = fact_check.parse_result(text, draft, sources, customer=ctx.customer,
                                                     scoring_facts=ctx.scoring_facts or None)
            except Exception as exc:
                # 核对服务异常不把已经付费生成的正文误判为不可用。
                result = fact_check.FactCheckResult("unverified", reason=str(exc))
        result = fact_check.merge_local_checks(result, draft, customer=ctx.customer,
                                              scoring_facts=ctx.scoring_facts or None)
        self._verification.append({"status": result.status, "issues": result.issues, "reason": result.reason})
        return result

    def _call_llm(self, messages: list[LLMMessage], template) -> str:
        if self._token_budget_exceeded:
            return self._finish_answer(messages, template)
        usage: dict[str, Any] = {}
        max_tokens = self._max_tokens(template)
        stream = stream_chat_completion_controlled(
            self._adapter,
            messages,
            temperature=template.temperature,
            max_tokens=max_tokens,
            on_usage=lambda value: usage.update(value),
            extra=_thinking_extra(self._thinking_enabled, self._reasoning_effort),
            cancel_event=self._cancel_event,
            deadline=self._deadline,
        )
        text = self._consume_stream(
            stream, phase="refine", messages=messages, max_tokens=max_tokens, usage=usage
        )
        if self._token_budget_exceeded:
            return self._finish_answer(messages, template, draft=text)
        return text

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
            max_tokens = self._max_tokens(template)
            stream = stream_chat_completion_controlled(
                self._adapter,
                messages,
                temperature=template.temperature,
                max_tokens=max_tokens,
                tools=tools.TOOL_SCHEMAS if tools_enabled else None,
                on_tool_calls=lambda tcs: tool_calls.extend(tcs),
                on_usage=lambda value: usage.update(value),
                extra=_thinking_extra(self._thinking_enabled, self._reasoning_effort),
                cancel_event=self._cancel_event,
                deadline=self._deadline,
            )
            phase = "reason" if tool_rounds == 0 else "tool_followup"
            text = self._consume_stream(
                stream, phase=phase, messages=messages, max_tokens=max_tokens, usage=usage
            )
            self._check_cancelled()
            if not tool_calls:
                if self._token_budget_exceeded:
                    return self._finish_answer(messages, template, draft=text), refs
                return text, refs
            if text and self._on_event:
                self._on_event("replace", {"text": "", "reason": "tool_followup"})
            tool_rounds += 1
            self._tool_rounds += 1
            tool_started = time.monotonic()
            messages, round_refs = tools.append_tool_results(
                messages, tool_calls, customer=customer, db=db, exclude_ids=exclude,
                budget=self._tool_budget,
            )
            self._tool_ms += int((time.monotonic() - tool_started) * 1000)
            refs.extend(round_refs)
            exclude.update(r["id"] for r in round_refs)
            if self._token_budget_exceeded or tool_rounds >= tools.MAX_TOOL_ROUNDS or self._tool_budget.calls_remaining <= 0:
                # 已回填本轮工具结果：不带工具收尾一次，避免空回复
                self._tool_warning = "工具调用次数已达上限（或总预算耗尽），已基于已获取信息作答"
                final_text = self._finish_answer(messages, template)
                # 当前工具轮的草稿已经通过 replace 从客户端清除；收尾请求
                # 才是基于工具结果生成的正式答案，持久化时也只保留它。
                return final_text, refs

    def _quality_call(self, messages, *, phase: str) -> str:
        """One bounded, invisible quality operation sharing the real turn budget."""
        self._check_cancelled()
        if self._token_budget_exceeded:
            raise LLMError("本次模型调用预算已用尽")
        limit = max(1, min(config.AI_FACT_CHECK_MAX_TOKENS, self._token_budget - self._total_tokens))
        deadline = time.monotonic() + config.AI_FACT_CHECK_TIMEOUT_SECONDS
        if self._deadline is not None:
            deadline = min(deadline, self._deadline - 0.5)
        usage = {}
        stream = stream_chat_completion_controlled(
            self._adapter, messages, temperature=0, max_tokens=limit, tools=None,
            extra=_thinking_extra(False), on_usage=lambda value: usage.update(value),
            cancel_event=self._cancel_event, deadline=deadline,
        )
        result = self._consume_stream(stream, phase=phase, messages=messages, max_tokens=limit,
                                      usage=usage, visible=False)
        if time.monotonic() >= deadline or usage.get("finish_reason") == "length":
            raise LLMError("局部核验时间或输出预算已耗尽")
        return result

    def _repair_locally(self, ctx, references, draft, verification) -> str:
        if len(draft) > config.AI_FACT_CHECK_MAX_DRAFT_CHARS:
            raise ValueError("正文超过局部核验长度上限")
        answer = local_quality.parse_answer(draft)
        bad = answer.locate(verification.issues)
        sources = fact_check.build_sources(ctx, references)
        messages = local_quality.messages_for_patch(answer, bad, verification.issues, sources)
        response = self._quality_call(messages, phase="local_repair")
        text, diagnostic = local_quality.apply_patches(response, answer, bad, sources)
        if any(fact_check._compact(issue["claim"]) in fact_check._compact(text)
               for issue in verification.issues):
            raise ValueError("局部修订仍保留已确认错误的原文")
        self._local_repairs.append({"operation": "patch", "status": "applied", **diagnostic})
        return text

    def _select_reliable(self, ctx, references, draft, verification):
        """Select existing independent units once; never generate business text here."""
        if len(draft) > config.AI_FACT_CHECK_MAX_DRAFT_CHARS:
            raise ValueError("正文超过局部核验长度上限")
        answer = local_quality.parse_answer(draft)
        bad = answer.locate(verification.issues)
        sources = fact_check.build_sources(ctx, references)
        unverified_units = {}
        unit_checks = []
        for unit in answer.units:
            if unit.kind == "heading":
                continue
            checked = fact_check.parse_result(
                '{"issues":[],"score_projections":[]}', unit.text, dict(sources), customer=ctx.customer,
                scoring_facts=ctx.scoring_facts or None,
            )
            if checked.issues:
                bad.add(unit.id)
            if checked.status == "unverified" or checked.reason:
                unverified_units[unit.id] = checked.reason or "评分推算未能完整核算"
            if checked.status != "passed" or checked.reason:
                unit_checks.append({"id": unit.id, "status": checked.status,
                                    "issues": checked.issues, "reason": checked.reason})
        if unit_checks:
            self._local_repairs.append({"operation": "local_unit_checks", "status": "completed", "units": unit_checks})
        messages = local_quality.messages_for_review(answer, bad, verification.issues, sources,
                                                    unverified_units=unverified_units)
        response = self._quality_call(messages, phase="unit_review")
        text, diagnostic = local_quality.apply_review(response, answer, bad | set(unverified_units), sources)
        self._local_repairs.append({"operation": "selection", "status": "applied", **diagnostic})
        return text

    def _refine(self, ctx, references, draft, critique_text, question, today, template, scenario: str = "strategy",
                verification=None) -> str:
        if verification is not None and verification.issues:
            return self._repair_locally(ctx, references, draft, verification)
        system_text = template.render_system(today=today)
        knowledge = _format_retrievals(references) or ctx.knowledge_text
        format_hint = (
            "保留 Markdown 正文与 ```json 策略块。"
            if scenario == "strategy"
            else "保留原输出结构（Markdown 正文）。"
        )
        refine_prompt = get_internal_prompt(
            "refine_answer", format_hint=format_hint,
            revision_data=tools.guardrails.wrap_untrusted(
                json.dumps({"draft": draft, "issues": critique_text}, ensure_ascii=False), "tool"
            ),
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
        self._total_tokens = 0
        self._token_budget_exceeded = False
        self._budget_finalized = False
        self._tool_budget = tools.ToolBudget()
        self._verification = []
        self._local_repairs = []
        max_iter = min(2, max(1, max_iterations or self._max))
        iterations = 1
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
                ctx.customer_text = "## 客户画像（工具查询）\n" + tools.guardrails.wrap_untrusted(
                    "；".join(f"{k}={v}" for k, v in profile.items()),
                    "customer",
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
            if not draft.strip() and self._llm_calls and self._llm_calls[-1].get("finish_reason") == "length":
                return self._degrade(scenario, ctx, question, references,
                                     LLMUnavailableError("生成 token 上限已耗尽且正文为空，请检查该场景的思考预算"))
            if not draft.strip() and not self._token_budget_exceeded and max_iter > 1:
                # 空草稿重试一次（不带工具）：避免模型偶发空响应直接带崩整个流程
                self._tool_warning = "首次生成返回为空，已自动重试"
                iterations = 2  # 空答重试也占用唯一修订机会。
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
        verification = self._verify(text, ctx, references)
        critique_text = "\n".join(filter(None, [
            tools.critique(text, ctx, scenario=scenario, has_knowledge=has_knowledge),
            verification.feedback(),
        ]))
        if (
            not self._budget_finalized
            and tools.needs_refine(critique_text)
            and iterations < max_iter
        ):
            iterations += 1
            self._refine_rounds += 1
            if not verification.issues and self._on_event:
                self._on_event("replace", {"text": "", "reason": "refine"})
            try:
                refined = self._refine(
                    ctx, references, draft, critique_text, question, today, template, scenario=scenario,
                    verification=verification,
                )
                if not refined.strip():
                    raise ValueError("修订返回空正文")
                text = refined
                verification = self._verify(text, ctx, references)
            except Exception as exc:
                # A failed edit must not erase previously generated independent analysis.
                self._local_repairs.append({"operation": "patch", "status": "failed", "reason": str(exc)})
                self._check_cancelled()
            critique_text = "\n".join(filter(None, [
                tools.critique(text, ctx, scenario=scenario, has_knowledge=has_knowledge),
                verification.feedback(),
            ]))

        # One selection and one final whole-answer check are the last recovery path.
        # Unknown/unverified content is not automatically discarded or accepted.
        if verification.status != "passed" or verification.reason:
            try:
                selected = self._select_reliable(ctx, references, text, verification)
                selected_check = self._verify(selected, ctx, references)
                if selected_check.status == "passed" and not selected_check.reason:
                    text, verification = selected, selected_check
                    critique_text = tools.critique(text, ctx, scenario=scenario, has_knowledge=has_knowledge)
                else:
                    self._local_repairs.append({"operation": "final_check", "status": "failed",
                                                "issues": selected_check.issues, "reason": selected_check.reason})
                    verification = selected_check
            except Exception as exc:
                self._local_repairs.append({"operation": "selection", "status": "failed", "reason": str(exc)})
                self._check_cancelled()

        if verification.status == "unverified" or verification.reason:
            warnings.append(f"关键事实核对未完成：{verification.reason}")
        if tools.needs_refine(critique_text):
            warnings.append("部分输出格式尚未完整满足要求，可按需重新生成。")
        if self._tool_warning:
            warnings.append(self._tool_warning)

        if verification.status != "passed" or verification.reason:
            # 仍有矛盾或无法完成核实，不把已知有问题的模型正文作为正式结论交付。
            # 规则结果不冒充AI核对通过，内部诊断、真实用量及失败状态仍保留。
            safe_text, safe_items = build_degraded_reply(scenario, ctx, question, quality_failure=True)
            if self._on_event:
                self._on_event("replace", {"text": "", "reason": "quality_fallback"})
                self._on_event("delta", {"text": safe_text, "phase": "quality_fallback"})
            return AgentResult(
                text=safe_text, references=_display_references(references), degraded=True,
                degraded_items=safe_items,
                warnings=["AI 关键结论未通过完整核实，已使用评分结果与规则建议。"],
                iterations=iterations, model=getattr(self._adapter, "model", ""),
                timings={**self._timings(), "answer_source": "rules_after_quality_check"},
            )

        # A surviving overview is not a completed strategy. Keep it, but use
        # explicit rule actions and preserve the old answer during regeneration.
        from .strategy import split_strategy_payload
        if scenario == "strategy" and text.strip() and not split_strategy_payload(text)[1]:
            _, rule_items = build_degraded_reply(scenario, ctx, question, quality_failure=True)
            return AgentResult(
                text=text, references=_display_references(references), degraded=True,
                degraded_items=rule_items,
                warnings=["概况已通过核实；未形成可核实的 AI 行动策略，以下行动来自规则建议。"],
                iterations=iterations, model=getattr(self._adapter, "model", ""),
                timings={**self._timings(), "answer_source": "ai_summary_with_rule_strategies"},
            )

        if not text.strip():
            return self._degrade(
                scenario,
                ctx,
                question,
                references,
                LLMUnavailableError("AI 连续多次返回空内容"),
            )

        repaired = any(item.get("status") == "applied" for item in self._local_repairs)
        if repaired:
            warnings = [warning for warning in warnings if not warning.startswith("关键事实核对未完成")]
            warnings.append("已修正或移除部分依据不足的结论，保留已核实的分析。")
            if self._on_event:
                self._on_event("replace", {"text": "", "reason": "local_quality_repair"})
                self._on_event("delta", {"text": text, "phase": "local_quality_repair"})
        return AgentResult(
            text=text,
            references=_display_references(references),
            degraded=False,
            degraded_items=[],
            warnings=warnings,
            iterations=iterations,
            model=getattr(self._adapter, "model", ""),
            timings={**self._timings(), **({"answer_source": "ai_with_local_repairs"} if repaired else {})},
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
            "verification": list(self._verification),
            "local_repairs": list(self._local_repairs),
        }

    def _degrade(self, scenario, ctx, question, references, exc) -> AgentResult:
        text, items = build_degraded_reply(scenario, ctx, question)
        return AgentResult(
            text=text,
            references=_display_references(references),
            degraded=True,
            degraded_items=items,
            warnings=[f"LLM 不可用，已降级为规则引擎：{exc}"],
            iterations=0,
            model="",
            timings=self._timings(),
        )
