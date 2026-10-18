"""Prompt 模板加载器。

模板正文在 ``backend/prompt_templates.yaml``，占位符用 ``{{变量名}}``——
刻意不用 ``str.format``，避免模板里的 JSON 示例大括号被当成格式化占位符。
与 ``scoring_config.yaml`` 一致：按文件 mtime 热加载，改完不用重启。
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import yaml

import config

PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# 场景 → 模板 key（与 ChatSession.scenario / 前端快捷入口一致）
SCENARIOS = ("free_qa", "assessment", "strategy", "alert_analysis")
DEFAULT_SCENARIO = "free_qa"


class PromptTemplateError(RuntimeError):
    """模板文件缺失或结构非法。"""


@dataclass(frozen=True)
class InternalPrompt:
    key: str
    system: str

    def render(self, **variables: Any) -> str:
        required = set(PLACEHOLDER_RE.findall(self.system))
        missing = {key for key in required if key not in variables or variables[key] is None}
        if missing:
            raise PromptTemplateError(f"内部任务 `{self.key}` 缺少变量：{', '.join(sorted(missing))}")
        return render(self.system, variables)


def render(text: str, variables: dict[str, Any]) -> str:
    """替换 ``{{var}}``；未提供的变量替换为空串并清理多余空行。"""

    def _sub(match: re.Match) -> str:
        value = variables.get(match.group(1), "")
        return "" if value is None else str(value)

    rendered = PLACEHOLDER_RE.sub(_sub, text)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


@dataclass
class PromptTemplate:
    key: str
    name: str = ""
    description: str = ""
    system: str = ""
    user: str = ""
    temperature: float = 0.3
    max_tokens: int = 2048

    def render_system(self, **variables: Any) -> str:
        return render(self.system, variables)

    def render_user(self, **variables: Any) -> str:
        return render(self.user, variables)


@dataclass
class PromptTemplateSet:
    version: str = ""
    updated_at: str = ""
    description: str = ""
    guardrails: str = ""
    templates: dict[str, PromptTemplate] = field(default_factory=dict)
    internal_tasks: dict[str, InternalPrompt] = field(default_factory=dict)
    path: str = ""

    def get(self, key: str | None) -> PromptTemplate:
        """取模板；未知场景回落到自由问答，保证对话永不因配置缺失中断。"""
        if key and key in self.templates:
            return self.templates[key]
        if DEFAULT_SCENARIO in self.templates:
            return self.templates[DEFAULT_SCENARIO]
        raise PromptTemplateError(f"prompt_templates.yaml 缺少模板 `{key or DEFAULT_SCENARIO}`")

    @property
    def scenarios(self) -> list[str]:
        return [key for key in SCENARIOS if key in self.templates]

    def internal(self, key: str) -> InternalPrompt:
        try:
            return self.internal_tasks[key]
        except KeyError as exc:
            raise PromptTemplateError(f"prompt_templates.yaml 缺少内部任务 `{key}`") from exc


def parse_prompt_templates(raw: dict, path: str = "") -> PromptTemplateSet:
    if not isinstance(raw, dict):
        raise PromptTemplateError("prompt_templates.yaml 根节点必须是对象")

    defaults = raw.get("defaults") or {}
    guardrails = str(defaults.get("guardrails") or "").strip()
    default_temperature = float(defaults.get("temperature", 0.3))
    default_max_tokens = int(defaults.get("max_tokens", 2048))

    raw_templates = raw.get("templates") or {}
    if not isinstance(raw_templates, dict) or not raw_templates:
        raise PromptTemplateError("prompt_templates.yaml 必须配置至少一个 templates 条目")

    templates: dict[str, PromptTemplate] = {}
    for key, item in raw_templates.items():
        if not isinstance(item, dict):
            raise PromptTemplateError(f"模板 `{key}` 必须是对象")
        system = str(item.get("system") or "")
        if not system.strip():
            raise PromptTemplateError(f"模板 `{key}` 缺少 system 段")
        templates[str(key)] = PromptTemplate(
            key=str(key),
            name=str(item.get("name") or key),
            description=str(item.get("description") or ""),
            # 公共护栏在加载期就注入，避免每次渲染都要传；
            # 只替换 guardrails 占位符，保留 {{today}} 等运行时变量
            system=system.replace("{{guardrails}}", guardrails) if "{{guardrails}}" in system else system,
            user=str(item.get("user") or ""),
            temperature=float(item.get("temperature", default_temperature)),
            max_tokens=int(item.get("max_tokens", default_max_tokens)),
        )

    raw_internal = raw.get("internal_tasks", {})
    if not isinstance(raw_internal, dict):
        raise PromptTemplateError("internal_tasks 必须是对象")
    internal_tasks = {}
    for key, item in raw_internal.items():
        if not isinstance(item, dict) or not isinstance(item.get("system"), str) or not item["system"].strip():
            raise PromptTemplateError(f"内部任务 `{key}` 缺少有效 system 段")
        internal_tasks[str(key)] = InternalPrompt(str(key), item["system"])

    return PromptTemplateSet(
        version=str(raw.get("version") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        description=str(raw.get("description") or ""),
        guardrails=guardrails,
        templates=templates,
        internal_tasks=internal_tasks,
        path=path,
    )


# ── mtime 缓存（与 scoring 配置同款策略）────────────────────────────────────

_cache: dict[str, tuple[float, PromptTemplateSet]] = {}
_lock = threading.Lock()


def default_template_path() -> str:
    return config.PROMPT_TEMPLATE_PATH


def load_prompt_templates(path: str | None = None, force_reload: bool = False) -> PromptTemplateSet:
    path = os.path.abspath(path or default_template_path())
    if not os.path.exists(path):
        raise PromptTemplateError(f"Prompt 模板文件不存在：{path}")

    mtime = os.path.getmtime(path)
    with _lock:
        cached = _cache.get(path)
        if cached and not force_reload and cached[0] == mtime:
            return cached[1]

        try:
            with open(path, "r", encoding="utf-8") as fp:
                raw = yaml.safe_load(fp)
        except (OSError, yaml.YAMLError) as exc:
            raise PromptTemplateError(f"Prompt 模板读取失败：{path}") from exc
        template_set = parse_prompt_templates(raw, path=path)
        _cache[path] = (mtime, template_set)
        return template_set


def clear_prompt_cache() -> None:
    with _lock:
        _cache.clear()


def get_template(scenario: str | None, path: str | None = None) -> PromptTemplate:
    return load_prompt_templates(path).get(scenario)


def get_internal_prompt(key: str, path: str | None = None, **variables: Any) -> str:
    """调用时读取热加载模板；内部任务缺失不能回退到业务问答。"""
    return load_prompt_templates(path).internal(key).render(**variables)
