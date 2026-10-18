"""在事实核对之后生成用户文案，内部诊断和评分字段不参与改写。"""
from __future__ import annotations

import re

from services.scoring import load_scoring_config

from .guardrails import StreamingOutputSanitizer


REVIEW_NOTICE = "部分结论尚待核实，请确认关键信息后再用于决策。"
HISTORY_NOTICE = "这条历史答复未通过完整事实核实，原结论已停止展示。请重新生成，获取经过核实的结论或评分规则建议。"
_REVIEW_PREFIXES = ("草稿精炼达到 ", "关键事实核对未完成：")
# 旧消息的证据可跨多行甚至空行，直到下一条生成提示或消息末尾。
_LEGACY_REVIEW = re.compile(
    r"^>[ \t]*⚠️[ \t]*(?:草稿精炼达到 [^\n]*|关键事实核对未完成：[^\n]*)"
    r".*?(?=\n\n>[ \t]*(?:⚠️|💡)|\Z)", re.M | re.S,
)


def present_warning(warning: str) -> str:
    return REVIEW_NOTICE if warning.startswith(_REVIEW_PREFIXES) else warning


def clean_review_notes(text: str) -> str:
    """兼容旧助手消息；不删除其他警告、业务段落或报告入口提示。"""
    text = _LEGACY_REVIEW.sub(f"> ⚠️ {REVIEW_NOTICE}", text)
    duplicate = f"> ⚠️ {REVIEW_NOTICE}\n\n> ⚠️ {REVIEW_NOTICE}"
    while duplicate in text:
        text = text.replace(duplicate, f"> ⚠️ {REVIEW_NOTICE}")
    return text


class AnswerPresenter:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        for dimension in load_scoring_config().dimensions:
            for factor in dimension.factors:
                label = re.match(r"^([A-Z][A-Z0-9]*-\d+[A-Z]?)\s+(.+)$", factor.label)
                if label:
                    code, name = label.groups()
                    self.names[code.casefold()] = name
                else:
                    name = factor.label
                self.names[factor.field.casefold()] = name
        aliases = "|".join(re.escape(key) for key in sorted(self.names, key=len, reverse=True))
        self.pattern = re.compile(
            r"https?://[^\s<>]+|\]\([^\n)]*\)|"
            rf"(?<![A-Za-z0-9_/-])(?P<factor>{aliases})(?![A-Za-z0-9_-])", re.I,
        )

    def text(self, text: str, *, collapse_labels: bool = True) -> str:
        text = self.pattern.sub(
            lambda match: self.names[match['factor'].casefold()] if match['factor'] else match[0], text,
        )
        if collapse_labels:
            # “CI-05 价值量化与ROI呈现”及“价值量化与ROI呈现（CI-05）”只保留一次名称。
            for name in set(self.names.values()):
                escaped = re.escape(name)
                text = re.sub(escaped + rf"(?:[ \t]+{escaped}|[ \t]*[（(]{escaped}[）)])", name, text)
        return text


def present_answer(text: str) -> str:
    return AnswerPresenter().text(clean_review_notes(text))


def unverified_history(text: str) -> bool:
    return bool(_LEGACY_REVIEW.search(text) or re.search(
        r"(?m)^>[ \t]*⚠️[ \t]*" + re.escape(REVIEW_NOTICE) + r"[ \t]*$", text,
    ))


def present_history_answer(text: str) -> str:
    return f"> ⚠️ {HISTORY_NOTICE}" if unverified_history(text) else present_answer(text)


def present_strategies(items: list[dict]) -> list[dict]:
    presenter = AnswerPresenter()
    # reference/priority等机器字段保留，避免改变引用标题与动作契约。
    visible_fields = {"title", "reason", "action", "expected_outcome"}
    return [
        {key: presenter.text(value) if key in visible_fields and isinstance(value, str) else value
         for key, value in item.items()} if isinstance(item, dict) else item
        for item in items
    ]


class ReadableOutputStream:
    """只暂存末尾ASCII词，编号跨分片时也不会泄漏半个编号；中文继续流式输出。"""
    def __init__(self) -> None:
        self._sanitizer = StreamingOutputSanitizer()
        self._presenter = AnswerPresenter()
        self._pending = ""

    def _feed_names(self, text: str, *, final: bool = False) -> str:
        self._pending += text
        trailing = None if final else re.search(r"[A-Za-z0-9_:/?&=#.%+\-]+$", self._pending)
        cut = trailing.start() if trailing else len(self._pending)
        ready, self._pending = self._pending[:cut], self._pending[cut:]
        return self._presenter.text(ready, collapse_labels=False)

    def feed(self, text: str) -> str:
        return self._feed_names(self._sanitizer.feed(text))

    def flush(self) -> str:
        return self._feed_names(self._sanitizer.flush(), final=True)
