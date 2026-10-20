"""基于本轮可见原文核对关键事实；不读取新客户、不扩大知识权限。"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field

import config
from . import guardrails
from .llm_adapter import LLMMessage
from .prompt_templates import get_internal_prompt
from .scoring_facts import build_scoring_facts, check_scoring_claims, format_scoring_facts, project_scores


@dataclass
class FactCheckResult:
    status: str  # passed / issues / unverified
    issues: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    verified_projections: list[dict] = field(default_factory=list)
    recheck_recommended: bool = False
    protocol_response: str = ""

    def feedback(self) -> str:
        return "\n".join(
            f"事实核对：草稿『{i['claim']}』；原文[{i['source_id']}]『{i['evidence']}』；{i['reason']}"
            for i in self.issues
        )


def build_sources(ctx, references: list[dict]) -> dict[str, str]:
    sources = {}
    if ctx.customer_text:
        sources["customer"] = ctx.customer_text
    if ctx.assessment is not None:
        a = ctx.assessment
        sources["assessment"] = json.dumps({
            "total_score": a.total_score, "max_score": a.max_score, "level": a.level,
            "dimensions": [{"name": d.name, "score": d.score, "max_score": d.max_score,
                            "normalized_score_100": round(d.score / d.max_score * 100, 1) if d.max_score else None}
                           for d in a.dimensions],
        }, ensure_ascii=False)
    if ctx.alert_text:
        sources["alerts"] = ctx.alert_text
    if ctx.scoring_facts:
        scoring_text = format_scoring_facts(ctx.scoring_facts)
        sources["scoring"] = scoring_text
        # customer用于生成时已带同一张事实表，核对时用独立source引用，避免付费重复上下文。
        visible = guardrails.sanitize_for_outbound(scoring_text)[0].replace("<", "＜").replace(">", "＞")
        if "customer" in sources:
            for representation in (visible, scoring_text):
                sources["customer"] = sources["customer"].replace(representation, "评分派生事实见scoring来源", 1)
    remaining = config.RAG_MODEL_CONTEXT_CHAR_BUDGET
    for ref in references:
        content = ref.get("_model_content") or ref.get("snippet") or ""
        if content and remaining > 0:
            sources[f"rag:{ref['id']}"] = f"《{ref.get('title', '')}》\n{content[:remaining]}"
            remaining -= min(len(content), remaining)
    if not references and ctx.knowledge_text:
        sources["knowledge"] = ctx.knowledge_text[:remaining]
    return {key: guardrails.sanitize_for_outbound(value)[0] for key, value in sources.items()}


def _segments(text: str) -> dict[str, str]:
    """编号只对应本次输入中的原文，不由模型构造或补全。"""
    parts = [line[start:start + 1200] for line in text.splitlines() if line.strip()
             for start in range(0, len(line), 1200)]
    return {str(index): part for index, part in enumerate(parts)}


def _draft_segments(text: str) -> dict[str, str]:
    """Keep each server quote inside one logical paragraph/card, even compact JSON.

    Source segmentation stays unchanged. Unparseable drafts retain the legacy
    check path, but cannot subsequently be locally edited as valid units.
    """
    from .local_quality import parse_answer
    try:
        answer = parse_answer(text)
    except (ValueError, TypeError):
        return _segments(text)
    parts = [unit.text[start:start + 1200] for unit in answer.units
             for start in range(0, len(unit.text), 1200)]
    return {str(index): part for index, part in enumerate(parts)}


def messages_for_check(draft: str, sources: dict[str, str], *, previous_check=None) -> list[LLMMessage]:
    payload = {
        "sources": [{"id": source_id, "segments": _segments(text)} for source_id, text in sources.items()],
        "draft_segments": _draft_segments(draft),
    }
    system = get_internal_prompt("fact_check")
    if previous_check is not None:
        payload["previous_check"] = {"status": previous_check.status, "issues": previous_check.issues,
                                     "reason": previous_check.reason,
                                     "invalid_response": previous_check.protocol_response}
        system += "\n\n" + get_internal_prompt("fact_recheck")
    data = json.dumps(payload, ensure_ascii=False)
    return [LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=guardrails.wrap_untrusted(data, "tool"))]


def _compact(value: str) -> str:
    return "".join(value.translate(str.maketrans("＜＞", "<>")).split())


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复的JSON字段")
        result[key] = value
    return result


def _historical_delta_is_observation(draft: str, match) -> bool:
    """Only exempt clear observations; unknown suffixes and future fields fail closed."""
    boundaries = '\n。；;！？!?'
    start = max(draft.rfind(char, 0, match.start()) for char in boundaries) + 1
    end = min((position for char in boundaries
               if (position := draft.find(char, match.end())) >= 0), default=len(draft))
    # Restrict JSON context to the actual containing string value, not neighboring fields.
    for field in re.finditer(r'"(?P<key>(?:[^"\\]|\\.)*)"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"', draft):
        if field.start('value') <= match.start() and match.end() <= field.end('value'):
            if json.loads('"' + field['key'] + '"') in {'action', 'expected_outcome'}:
                return False
            start, end = max(start, field.start('value')), min(end, field.end('value'))
            break
    future = (r'建议|预计|预估|预测|预期|目标|将|若|如果|假设|计划|有望|拟|期望|希望|争取|力争|'
              r'未来|下次|下期|下月|下季度|调整后|改善后|提升后')
    if re.search(future, draft[start:end]):
        return False
    headings = []
    for heading in re.finditer(r'(?m)^[ \t]*(#{1,6})[ \t]+([^\n]+)', draft[:match.start()]):
        level = len(heading[1])
        while headings and headings[-1][0] >= level:
            headings.pop()
        headings.append((level, heading[2]))
    if any(re.search(future, title) for _, title in headings):
        return False
    prior_lines = draft[:start].rstrip().splitlines()
    if prior_lines and re.fullmatch(
        r'\s*(?:>\s*)*(?:[-+]\s+|\d+[.、)]\s*)?(?:预期效果|预期目标|未来目标|目标|行动计划|计划)\s*[:：]?\s*',
        re.sub(r'[*_`]', '', prior_lines[-1]),
    ):
        return False
    # Arbitrary trailing text could qualify the number as a goal. Only simple
    # retrospective descriptions are safe to exempt without factor targets.
    suffix = draft[match.end():end].strip()
    return re.fullmatch(r'(?:[，,]\s*(?:回升|上升|下降|回落|改善|恶化)(?:明显|显著)?)?[）)」』”]*', suffix) is not None


def _score_aliases(facts):
    aliases = {"总分": "total", "综合评分": "total", "综合客情分": "total", "总增分": "total"}
    for dimension in facts["dimensions"]:
        aliases[dimension["key"].upper()] = dimension["key"]
        aliases[dimension["name"]] = dimension["key"]
    return aliases


def _projection_tail_is_complete(draft: str, end: int) -> bool:
    """未知单位/数量级/范围不属于已验证数字；空白和闭引号不截断它们。"""
    if re.match(r"[ \t]*\r?\n(?:[ \t]*\r?\n)*[ \t]*#{1,6}[ \t]+", draft[end:]):
        return True  # 下一 Markdown 章节是明确的结构边界，不是跨行单位。
    tail = draft[end:].lstrip()
    # JSON/Markdown 的闭合字符只在随后确实结束该数值项时充当边界。
    while tail and tail[0] in '\")]}）】》」』”’`':
        tail = tail[1:].lstrip()
    if not tail:
        return True
    # 逗号接数字可能是千位分隔或范围延续，不能当作独立句界。
    if re.match(r"[,，]\s*\d", tail):
        return False
    return tail[0] in '，,。；;！？!?'


def _score_expressions(draft: str, facts: dict) -> list[dict]:
    """只识别显式维度/总分推算，不将次数、日期或单人等级箭头当作总分。"""
    aliases = _score_aliases(facts)
    names = "|".join(re.escape(name) for name in sorted(aliases, key=len, reverse=True))
    subject = rf"(?<![A-Za-z0-9_\-])(?:{names})(?![A-Za-z_\-])"
    # 一个推算只归属于最近的计分主体；遇到“总分/另一维度”必须重新起算。
    prefix = rf"(?<![A-Za-z0-9_\-])(?P<name>{names})(?![A-Za-z_\-])(?:(?!{subject})[^\n。；;]){{0,28}}?"
    number = r"-?\d+(?:\.\d+)?"
    arrow = re.compile(prefix + rf"(?P<before>{number})(?:\s*/\s*(?P<before_max>{number}))?\s*(?:分)?\s*(?:→|->|⇒|提升至|提升到|提高至|提高到|升至|升到|到|至)\s*(?P<after>{number})(?![\d.%％])(?:\s*/\s*(?P<after_max>{number}))?(?:\s*分)?"
                       + r"(?:\s*[（(]\s*(?P<delta>[+-]?\d+(?:\.\d+)?)\s*(?:分)?\s*[）)])?", re.I)
    delta = re.compile(prefix + rf"(?:提升|提高|增加|增分|收益)(?:约|为|可达)?\s*\+?(?P<delta>{number})\s*分", re.I)
    signed_delta = re.compile(prefix + rf"\+\s*(?P<delta>\d+(?:\.\d+)?)\s*分", re.I)
    target = re.compile(prefix + rf"(?:可达|将达到|预计为|预计达到|目标为)\s*(?P<after>{number})\s*分", re.I)
    total_gain = re.compile(rf"(?<![A-Za-z0-9_\-])(?P<name>总增分)(?:约|为|[:：])?\s*\+?"
                            rf"(?P<delta>{number})(?![\d.%％])(?:\s*分)?", re.I)
    historical = re.compile(r"(?:(?:较|比)(?:上次(?:评估)?|上期|上月|上季度|去年同期)|环比|同比)"
                            r"\s*(?:提升|提高|增加|上升|变化)?\s*\+?\s*" + number + r"\s*分")
    results = []
    for pattern in (arrow, delta, signed_delta, target, total_gain):
        for match in pattern.finditer(draft):
            # Historical changes are verified against source history, not future factor targets.
            # Future/conditional language still requires an engine projection, even with a comparator.
            if pattern in (delta, signed_delta) and historical.search(match.group(0)):
                if _historical_delta_is_observation(draft, match):
                    continue
            if any(match.start() >= row["start"] and match.end() <= row["end"] for row in results):
                continue
            name = match["name"]
            key = next(value for alias, value in aliases.items() if alias.casefold() == name.casefold())
            # 原有宽松提取用于发现问题；正向裁决只允许纯数值语法，不让
            # “不应/制度要求/最低”等语义混进可放行的表达式范围。
            field = "before" if pattern is arrow else ("after" if pattern is target else "delta")
            leading = _compact(draft[match.end("name"):match.start(field)])
            pure_prefix = (
                r"(?:维度|得分|评分|加权分)?(?:由|从)?" if pattern is arrow else
                r"(?:维度|得分|评分)?(?:可达|将达到|预计为|预计达到|目标为)" if pattern is target else
                r"(?:约|为|[:：])?\+?" if pattern is total_gain else
                r"(?:维度|得分|评分)?\+" if pattern is signed_delta else
                r"(?:维度|得分|评分)?(?:提升|提高|增加|增分|收益)(?:约|为|可达)?\+?"
            )
            results.append({"claim": match.group(0), "dimension": key, "start": match.start(), "end": match.end(),
                            "numeric_only": re.fullmatch(pure_prefix, leading) is not None,
                            "complete_tail": _projection_tail_is_complete(draft, match.end()),
                            **{field: float(value) for field, value in match.groupdict().items() if field != "name" and value is not None}})
    return results


def _projection_scope(draft, expression):
    """JSON策略项或Markdown章节是保守的覆盖范围，不信模型自行缩小引用。"""
    decoder = json.JSONDecoder()
    objects = []
    for opening in re.finditer(r"\{", draft):
        if opening.start() > expression["start"]:
            break
        try:
            value, length = decoder.raw_decode(draft[opening.start():])
        except ValueError:
            continue
        end = opening.start() + length
        if isinstance(value, dict) and end >= expression["end"]:
            objects.append((opening.start(), end))
    if objects:
        start, end = min(objects, key=lambda bounds: bounds[1] - bounds[0])
        return draft[start:end]
    headings = [m.start() for m in re.finditer(r"(?m)^#{1,6}\s+", draft)]
    start = max((position for position in headings if position <= expression["start"]), default=0)
    end = min((position for position in headings if position > expression["end"]), default=len(draft))
    return draft[start:end]


def _target_anchor_text(value):
    if isinstance(value, (list, tuple)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _compact(unicodedata.normalize("NFKC", str(value))).casefold()


def _projection_target_evidence(draft, expression, changes, facts, *, scope_override=None):
    """覆盖相关段落的因子，目标值须锚定到对应因子原文；不确定则未完成。"""
    scope = _projection_scope(draft, expression) if scope_override is None else scope_override
    references = []
    for field, item in facts["factors"].items():
        field_id = re.escape(field).replace("_", r"[\s_\-－–]*")
        label = re.sub(r"^[A-Za-z]+[\s_\-－–]*\d+[A-Za-z]*\s*", "", item["label"]).strip()
        alternatives = field_id + ("|" + re.escape(label) if label else "")
        pattern = re.compile(r"(?<![A-Za-z0-9_])(?:" + alternatives + r")(?![A-Za-z0-9_])", re.I)
        references.extend((match.start(), match.end(), field) for match in pattern.finditer(scope))
    references.sort()
    required = {field for _, _, field in references
                if expression["dimension"] == "total" or facts["factors"][field]["dimension_key"] == expression["dimension"]}
    if not required or not required.issubset(changes):
        return None
    evidence = {}
    for index, (start, field_end, field) in enumerate(references):
        if field not in changes:
            continue
        end = references[index + 1][0] if index + 1 < len(references) else len(scope)
        fragment = scope[start:end]
        # 原值出现在“由A到B”的A位置不构成目标证据；需有目标方向或明确维持原值。
        target = re.escape(_target_anchor_text(changes[field]))
        cue = r"(?:→|->|⇒|提升到|提升至|升级至|改为|达到|升到|升至|到|至|目标(?:值)?(?:为|是|[:：])?|维持(?:不变)?(?:为)?|保持(?:不变)?(?:为)?)"
        normalized = _target_anchor_text(fragment)
        if "维持原值" in normalized:
            # Only an explicit, complete unchanged instruction is evidence. Negation,
            # alternatives and another target in the same fragment remain unverified.
            instruction = _target_anchor_text(scope[field_end:end])
            if (changes[field] != facts["factors"][field]["current_value"] or not re.fullmatch(
                r"[:：]?维持原值[，,。；;、.!！]*", instruction
            )):
                return None
            evidence[field] = fragment
            continue
        direction = re.search(cue, normalized)
        if scope_override is not None and direction and not re.match(
            r"[\"'‘’“”「」『』\\]*" + target + r"(?![\d.])", normalized[direction.end():],
        ):
            return None  # 同一因子的冲突目标不能由另一次匹配掩盖。
        if re.search(cue + r"[\"'‘’“”「」『』\\]*" + target + r"(?![\d.])", normalized):
            evidence[field] = fragment
    return evidence if set(evidence) == set(changes) else None


def _check_projections(payload, draft, sources, customer, facts) -> FactCheckResult:
    expressions = _score_expressions(draft, facts)
    entries = payload.get("score_projections", [])
    if not isinstance(entries, list) or len(entries) > 8:
        raise ValueError("score_projections必须为最多8项的列表")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("claim"), str) or not entry["claim"].strip():
            raise ValueError("评分推算引用不完整")
        if _compact(entry["claim"]) not in _compact(draft):
            raise ValueError("评分推算引用无法定位")
    issues = []
    verified = []
    missing = False
    for expression in expressions:
        # 计分对象取草稿实际表达式，不信模型dimension标签；同一目标集可核对同句的维度和总分。
        scope = _projection_scope(draft, expression)
        declared = None
        has_declared = False
        try:
            strategy = json.loads(scope, object_pairs_hook=_unique_object)
            if isinstance(strategy, dict) and "score_targets" in strategy:
                has_declared = True
                declared = strategy["score_targets"]
                scope = str(strategy.get("action", ""))
        except ValueError:
            has_declared = '"score_targets"' in scope
        matches = ([{"changes": declared}] if has_declared else
                   [entry for entry in entries if _compact(expression["claim"]) in _compact(entry["claim"])])
        if not matches or not isinstance(matches[0].get("changes"), dict) or not matches[0]["changes"]:
            missing = True
            continue
        if customer is None:
            missing = True
            continue
        target_evidence = _projection_target_evidence(
            draft, expression, matches[0]["changes"], facts,
            scope_override=scope if has_declared else None,
        )
        if target_evidence is None:
            missing = True
            continue
        try:
            projection = project_scores(customer, matches[0]["changes"])
        except (ValueError, TypeError):
            missing = True
            continue
        key = expression["dimension"]
        if key == "total":
            before, after = projection["before"]["total_score"], projection["after"]["total_score"]
            maximum = facts["max_score"]
        else:
            before = next(row["score"] for row in projection["before"]["dimensions"] if row["key"] == key)
            after = next(row["score"] for row in projection["after"]["dimensions"] if row["key"] == key)
            maximum = next(row["max_score"] for row in facts["dimensions"] if row["key"] == key)
        # 展示总分保留一位小数，真实增量使用引擎的未舍入总分差。
        change = projection["delta"]["total_score"] if key == "total" else projection["delta"]["dimensions"][key]
        expected = {"before": before, "after": after, "delta": change,
                    "before_max": maximum, "after_max": maximum}
        if any(not math.isclose(expression[field], expected[field], rel_tol=0, abs_tol=0.051)
               for field in expected if field in expression):
            source_id = f"projection:{len(issues)}"
            projection["target_evidence"] = target_evidence
            evidence = json.dumps(projection, ensure_ascii=False)
            sources[source_id] = evidence
            issues.append({"claim": expression["claim"], "source_id": source_id, "evidence": evidence,
                           "reason": f"按完整目标因子重算，{key}应从{before:g}变为{after:g}，变化{change:+g}；不得把目标分数或自行估算当作引擎收益。"})
        elif not expression["complete_tail"]:
            missing = True  # 引擎只识别到前缀，不能把未覆盖的完整数值项当作通过。
        elif expression["numeric_only"]:
            verified.append({key: expression[key] for key in ("claim", "start", "end")})
    return FactCheckResult("issues" if issues else ("unverified" if missing else "passed"), issues=issues,
                           reason="评分推算缺少可核验的完整合法目标或目标原文锚点，未标记通过" if missing else "",
                           verified_projections=verified)


def _engine_covers_issue(issue, draft, verified):
    """仅裁决精确数值引用；全部同文出现位置都须各自通过完整重算。"""
    if issue["source_id"] != "scoring" or "claim_id" in issue:
        return False
    claim = _compact(issue["claim"])
    proofs = [item for item in verified if _compact(item["claim"]) == claim]
    return bool(proofs) and len(proofs) == _compact(draft).count(claim)


def merge_local_checks(result: FactCheckResult, draft: str, *, customer=None, scoring_facts=None):
    """模型成功与否均执行硬校验；保留模型未完成原因，不重复已报告的问题。"""
    facts = scoring_facts if scoring_facts is not None else (build_scoring_facts(customer) if customer is not None else {})
    if not facts:
        return result
    issues = check_scoring_claims(draft, customer, facts)
    issues.extend(issue for issue in result.issues if issue not in issues)
    return FactCheckResult("issues" if issues else result.status, issues=issues[:8], reason=result.reason,
                           verified_projections=result.verified_projections,
                           recheck_recommended=result.recheck_recommended,
                           protocol_response=result.protocol_response)


def parse_result(text: str, draft: str, sources: dict[str, str], *, customer=None, scoring_facts=None) -> FactCheckResult:
    """校验引用锚点，拒绝核对模型编造的证据和不完整 JSON。"""
    facts = scoring_facts if scoring_facts is not None else (build_scoring_facts(customer) if customer is not None else {})
    local_issues = check_scoring_claims(draft, customer, facts) if facts else []
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    projection_issues = []
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
        issues = payload["issues"]
        if not isinstance(issues, list) or len(issues) > 8:
            raise ValueError("issues 必须为最多8项的列表")
        draft_segments = _draft_segments(draft)
        issues = [dict(issue) if isinstance(issue, dict) else issue for issue in issues]
        for issue in issues:
            if isinstance(issue, dict) and "claim_id" in issue:
                issue["claim"] = draft_segments.get(str(issue["claim_id"]), "")
                issue["evidence"] = _segments(sources.get(issue.get("source_id", ""), "")).get(str(issue.get("evidence_id", "")), "")
        entries = payload.get("score_projections", [])
        if isinstance(entries, list):
            payload["score_projections"] = [
                {**entry, "claim": draft_segments.get(str(entry["claim_id"]), "")}
                if isinstance(entry, dict) and "claim_id" in entry else entry
                for entry in entries
            ]
        projections = _check_projections(payload, draft, sources, customer, facts) if facts else FactCheckResult("passed")
        projection_issues = projections.issues
        for issue in issues:
            if not isinstance(issue, dict) or any(
                not isinstance(issue.get(k), str) or not issue[k].strip()
                for k in ("claim", "source_id", "evidence", "reason")
            ):
                raise ValueError("问题字段不完整")
            source = sources.get(issue["source_id"], "")
            # wrap_untrusted 将尖括号转为全角；核对模型逐字摘录的是该表示。
            # 只统一这两个字符和空白，不合并比较方向或放宽数值/来源校验。
            if _compact(issue["claim"]) not in _compact(draft) or not source or _compact(issue["evidence"]) not in _compact(source):
                raise ValueError("核对引用无法定位到草稿或原文")
        # 来源和JSON协议先校验。只撤销引擎完整覆盖的精确数值争议，整卡及
        # 其他事实仍需模型核对；局部证明不能覆盖别处缺失目标或确定性错误。
        issues = [issue for issue in issues if not _engine_covers_issue(
            issue, draft, projections.verified_projections,
        )]
        # issues 同时宣称“正确/不报告”时，整份模型核对结果不可信。
        # 不将其升级为强制删除依据，也不靠过滤条目反推通过；交给独立单元
        # 复核和最终整答核对。混合真实错误的说明同样保持未完成状态。
        nonissue_conclusion = (
            r"不构成(?:事实)?(?:矛盾|错误)|不属于事实错误|不应报错|不报(?:告|错)?|"
            r"(?<![不未非])(?:一致|正确|相符)|无误|"
            r"(?:未发现|不存在|没有|无)(?:实质|事实|任何|明显)?(?:错误|矛盾|冲突|问题)"
        )
        if any(re.search(nonissue_conclusion, issue["reason"]) for issue in issues):
            deterministic = (local_issues + projection_issues)[:8]
            return FactCheckResult(
                "issues" if deterministic else "unverified", issues=deterministic,
                reason="核对响应包含自我撤销或相互矛盾的问题结论，需重新核实",
                verified_projections=projections.verified_projections,
                recheck_recommended=True,
                protocol_response=text[:32000],
            )
        merged = local_issues + projections.issues + issues
        return FactCheckResult("issues" if merged else projections.status, issues=merged[:8], reason=projections.reason,
                               verified_projections=projections.verified_projections,
                               recheck_recommended=any(re.search(
                                   r"未(?:提及|包含|要求|限定|规定|明确|支持)|没有(?:提到|规定|要求)|不(?:包含|支持)",
                                   issue["reason"],
                               ) for issue in issues))
    except (ValueError, TypeError, KeyError):
        deterministic = (local_issues + projection_issues)[:8]
        return FactCheckResult("issues" if deterministic else "unverified", issues=deterministic,
                               reason="核对响应不完整或证据无法定位")
