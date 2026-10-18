"""Constrained edits and dependency-aware selection over server-owned answer units.

The models can propose edits or select existing units, never replace the whole
answer through the selection protocol. Every resulting answer is checked again.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from . import guardrails
from .llm_adapter import LLMMessage
from .prompt_templates import get_internal_prompt


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("重复JSON字段")
        value[key] = item
    return value


def _decode(text):
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    value = json.loads(text, object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError("协议必须为对象")
    return value


def _compact(text):
    return "".join(text.translate(str.maketrans("＜＞", "<>")).split())


@dataclass(frozen=True)
class Unit:
    id: str
    kind: str
    text: str
    start: int
    end: int
    summary: bool = False
    item: dict | None = None


@dataclass
class Answer:
    draft: str
    units: list[Unit]

    def data(self):
        return [{"id": unit.id, "kind": unit.kind, "text": unit.text,
                 "summary": unit.summary, **({"item": unit.item} if unit.item is not None else {})}
                for unit in self.units]

    def locate(self, issues):
        """Resolve exact server quotes back to original spans, including multi-card lines.

        Ambiguous repeated quotes mark every overlap; an unlocated issue is never
        silently ignored or interpreted as permission to keep the whole draft.
        """
        normalized, positions = [], []
        for index, char in enumerate(self.draft):
            if not char.isspace():
                normalized.append(char.translate(str.maketrans("＜＞", "<>")))
                positions.append(index)
        normalized = "".join(normalized)
        bad = set()
        for issue in issues:
            claim = _compact(issue.get("claim", ""))
            if not claim:
                raise ValueError("问题缺少定位原文")
            cursor = 0
            found = False
            while (offset := normalized.find(claim, cursor)) >= 0:
                start, end = positions[offset], positions[offset + len(claim) - 1] + 1
                matches = {u.id for u in self.units if u.start < end and u.end > start}
                if not matches:
                    raise ValueError("错误位于无法安全处理的结构中")
                bad.update(matches)
                found = True
                cursor = offset + len(claim)
            if not found:
                raise ValueError("问题无法定位到单元")
        return bad

    def render(self, selected=None, replacements=None):
        selected = set(selected) if selected is not None else {u.id for u in self.units}
        replacements = replacements or {}
        parts, items = [], []
        for index, original in enumerate(self.units):
            if original.id not in selected:
                continue
            unit = replacements.get(original.id, original)
            if unit.kind == "heading":
                following = []
                for other in self.units[index + 1:]:
                    if other.kind == "heading":
                        break
                    following.append(other)
                if not any(other.id in selected and other.kind != "heading" for other in following):
                    continue
            if unit.item is not None:
                items.append(unit.item)
            else:
                parts.append(unit.text)
        if items:
            parts.append("```json\n" + json.dumps({"strategies": items}, ensure_ascii=False, indent=2) + "\n```")
        return "\n\n".join(parts).strip()


_SUMMARY = re.compile(r"总览|总体|综合(?:结论|判断)|摘要|综上|总结")


def parse_answer(draft: str) -> Answer:
    """Use complete paragraphs/list items and complete raw strategy objects as units."""
    units = []
    summary_section = False

    def add(kind, text, start, end, *, item=None, summary=False):
        if text.strip():
            units.append(Unit(f"u{len(units)}", kind, text.strip(), start, end, summary, item))

    def markdown(start, end):
        nonlocal summary_section
        pending_start = None
        pending_end = None

        def flush():
            nonlocal pending_start, pending_end
            if pending_start is not None:
                text = draft[pending_start:pending_end]
                add("markdown", text, pending_start, pending_end,
                    summary=summary_section or bool(_SUMMARY.match(text.strip().lstrip("*> "))))
                pending_start = pending_end = None

        offset = start
        for line in draft[start:end].splitlines(keepends=True):
            line_end = offset + len(line)
            if re.match(r"^\s*#{1,6}\s+", line):
                flush()
                summary_section = bool(_SUMMARY.search(line))
                add("heading", line, offset, line_end, summary=summary_section)
            elif not line.strip():
                flush()
            else:
                if re.match(r"^\s*(?:[-*+]\s+|\d+[.、)]\s*)", line):
                    flush()
                if pending_start is None:
                    pending_start = offset
                pending_end = line_end
            offset = line_end
        flush()

    cursor = 0
    fences = list(re.finditer(r"```(?:json)?[ \t]*\r?\n?(.*?)```", draft, re.S | re.I))
    for match in fences:
        markdown(cursor, match.start())
        snippet = match.group(1)
        payload = json.loads(snippet, object_pairs_hook=_unique)
        if isinstance(payload, dict):
            keys = [key for key in ("strategies", "items", "strategy_items") if key in payload]
            if len(keys) != 1 or set(payload) != set(keys):
                raise ValueError("策略块含无法独立验证的附加结构")
            raw_items = payload[keys[0]]
        else:
            raw_items = payload
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("策略块缺少完整条目")
        decoder = json.JSONDecoder(object_pairs_hook=_unique)
        search_from = 0
        for item in raw_items:
            if not isinstance(item, dict) or not str(item.get("title") or item.get("name") or "").strip():
                raise ValueError("策略项不完整")
            found = False
            for opening in re.finditer(r"\{", snippet[search_from:]):
                offset = search_from + opening.start()
                try:
                    decoded, length = decoder.raw_decode(snippet[offset:])
                except ValueError:
                    continue
                if decoded == item:
                    start = match.start(1) + offset
                    add("strategy", draft[start:start + length], start, start + length, item=item)
                    search_from = offset + length
                    found = True
                    break
            if not found:
                raise ValueError("策略原文无法定位")
        cursor = match.end()
    markdown(cursor, len(draft))
    if "```" in draft[cursor:] or not units or len(units) > 80:
        raise ValueError("草稿结构不完整或单元过多")
    return Answer(draft, units)


def _dependencies(entries, answer):
    ids = {u.id for u in answer.units}
    if not isinstance(entries, list) or len(entries) != len(ids):
        raise ValueError("单元审查不完整")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") not in ids or entry["id"] in result:
            raise ValueError("未知或重复单元id")
        dependencies = entry.get("depends_on")
        if (not isinstance(dependencies, list)
                or any(not isinstance(value, str) or value not in ids or value == entry["id"] for value in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise ValueError("非法依赖")
        result[entry["id"]] = set(dependencies)
    return result


def closure(removed, dependencies):
    removed = set(removed)
    while True:
        expanded = removed | {key for key, values in dependencies.items() if values & removed}
        if expanded == removed:
            return removed
        removed = expanded


def _anchors(entry, sources):
    anchors = entry.get("sources")
    if not isinstance(anchors, list) or not anchors:
        raise ValueError("保留/修订单元缺少来源支持")
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError("来源格式错误")
        source, evidence = anchor.get("source_id"), anchor.get("evidence")
        if not isinstance(source, str) or source not in sources or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("未知来源或空证据")
        if _compact(evidence) not in _compact(sources[source]):
            raise ValueError("证据无法定位")


def messages_for_patch(answer, known_bad, issues, sources):
    return _messages(get_internal_prompt("local_patch"), answer, known_bad, issues, sources)


def messages_for_review(answer, known_bad, issues, sources, *, unverified_units=None):
    return _messages(get_internal_prompt("unit_review"), answer, known_bad, issues, sources,
                     unverified_units=unverified_units or {})


def _messages(system, answer, known_bad, issues, sources, **extra):
    payload = {"units": answer.data(), "known_bad": sorted(known_bad), "issues": issues, "sources": sources}
    payload.update(extra)
    return [LLMMessage(role="system", content=system), LLMMessage(role="user", content=guardrails.wrap_untrusted(
        json.dumps(payload, ensure_ascii=False), "tool"))]


def apply_patches(text, answer, known_bad, sources):
    payload = _decode(text)
    if set(payload) != {"dependencies", "patches"}:
        raise ValueError("局部修订协议字段错误")
    dependencies = _dependencies(payload["dependencies"], answer)
    allowed = closure(known_bad, dependencies) | {unit.id for unit in answer.units if unit.summary}
    patches = payload["patches"]
    if not isinstance(patches, list) or not patches:
        raise ValueError("未提供可靠局部修订")
    units = {u.id: u for u in answer.units}
    replacements = {}
    for patch in patches:
        if not isinstance(patch, dict) or patch.get("id") not in allowed or patch["id"] in replacements:
            raise ValueError("修订越界或id重复")
        unit = units[patch["id"]]
        _anchors(patch, sources)
        if unit.item is not None:
            item = patch.get("item")
            if set(patch) != {"id", "item", "sources"} or not isinstance(item, dict) or set(item) != set(unit.item):
                raise ValueError("策略修订丢失原始字段")
            if not str(item.get("title") or item.get("name") or "").strip():
                raise ValueError("策略修订为空")
            replacements[unit.id] = replace(unit, item=item, text=json.dumps(item, ensure_ascii=False))
        else:
            value = patch.get("text")
            if set(patch) != {"id", "text", "sources"} or not isinstance(value, str) or not value.strip() or "```" in value:
                raise ValueError("非法Markdown修订")
            parsed = parse_answer(value)
            if len(parsed.units) != 1 or parsed.units[0].kind != unit.kind:
                raise ValueError("Markdown修订必须保持单个同类逻辑单元")
            replacements[unit.id] = replace(unit, text=value.strip())
    if not known_bad.issubset(replacements):
        raise ValueError("已知问题未全部修订")
    affected = closure(known_bad, dependencies)
    # A dependency or global conclusion cannot survive unchanged after its premise changes.
    removed = (affected | {u.id for u in answer.units if u.summary}) - set(replacements)
    rendered = answer.render({u.id for u in answer.units} - removed, replacements)
    return rendered, {"patched": sorted(replacements), "removed": sorted(removed),
                      "dependencies": {key: sorted(value) for key, value in dependencies.items()}}


def apply_review(text, answer, known_bad, sources):
    payload = _decode(text)
    if set(payload) != {"units"}:
        raise ValueError("单元审查协议字段错误")
    dependencies = _dependencies(payload["units"], answer)
    removed = set(known_bad)
    for entry in payload["units"]:
        if set(entry) != {"id", "decision", "depends_on", "sources"} or entry["decision"] not in ("keep", "remove"):
            raise ValueError("非法保留/移除协议")
        if entry["decision"] == "remove":
            removed.add(entry["id"])
        elif next(u for u in answer.units if u.id == entry["id"]).kind != "heading":
            _anchors(entry, sources)
    removed = closure(removed, dependencies)
    if removed:
        removed |= {unit.id for unit in answer.units if unit.summary}
        removed = closure(removed, dependencies)
    selected = {u.id for u in answer.units} - removed
    if not any(u.id in selected and u.kind != "heading" for u in answer.units):
        raise ValueError("没有可靠独立分析可保留")
    return answer.render(selected), {"removed": sorted(removed), "kept": sorted(selected),
                                     "dependencies": {key: sorted(value) for key, value in dependencies.items()}}
