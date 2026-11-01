"""对话安全护栏。

两个方向：
- **入向**：用户输入落库与送 LLM 前，脱敏密码/密钥/身份证/银行卡等敏感串，
  并限制长度（防止超长输入撑爆上下文与 Token 成本）。
- **出向**：LLM 回复落库与推流前做同样的脱敏，防止提示词注入把密钥回显出来。

注意：客户联系电话属业务数据但含个人信息，**不注入 LLM 上下文**，
这里只在自由文本里做兜底遮蔽。
"""

from __future__ import annotations

import re

import config

MASK = "***"

# 顺序敏感：先匹配"键: 值"形式，再匹配裸串
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # password: xxx / 密码=xxx / api_key: xxx / token 为 xxx
    (
        "credential",
        re.compile(
            r"((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|token|密码|口令|密钥)"
            r"\s*(?:[:=是为]|：)\s*)"
            r"(“[^”\r\n]*”?|‘[^’\r\n]*’?|[^\s，,；;、。！？“”‘’'\"]{4,})",
            re.IGNORECASE,
        ),
    ),
    # 主流大模型风格密钥
    ("api_key", re.compile(r"(?<![A-Za-z0-9_-])(sk-[A-Za-z0-9_-]{12,})(?![A-Za-z0-9_-])")),
    # 身份证（18 位）
    ("id_card", re.compile(r"(?<!\d)(\d{17}[\dXx])(?![A-Za-z0-9])")),
    # 银行卡（16–19 位连续数字）
    ("bank_card", re.compile(r"(?<!\d)(\d{16,19})(?!\d)")),
]

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")

def sanitize_text(text: str) -> tuple[str, list[str]]:
    """脱敏并返回命中的敏感类型列表。"""
    if not text:
        return "", []

    hits: list[str] = []
    cleaned = text
    for name, pattern in _PATTERNS:
        if name == "credential":
            cleaned, count = pattern.subn(lambda m: f"{m.group(1)}{MASK}", cleaned)
        else:
            cleaned, count = pattern.subn(MASK, cleaned)
        if count:
            hits.append(name)

    cleaned, count = _PHONE_RE.subn(lambda m: f"{m.group(1)}****{m.group(3)}", cleaned)
    if count:
        hits.append("phone")

    return cleaned, hits


def sanitize_input(text: str, max_chars: int | None = None) -> tuple[str, list[str]]:
    """入向：脱敏 + 截断。"""
    limit = max_chars or config.CHAT_MAX_INPUT_CHARS
    cleaned, hits = sanitize_text((text or "").strip())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "\n\n（输入过长已截断）"
        hits.append("truncated")
    return cleaned, hits


def sanitize_output(text: str) -> str:
    """出向：只脱敏不截断。"""
    cleaned, _ = sanitize_text(text or "")
    return cleaned


def sanitize_for_outbound(text: str, *, max_chars: int | None = None) -> tuple[str, list[str]]:
    """统一出站 DLP：在 Prompt 或 Embedding 离开进程前脱敏并可选截断。"""
    cleaned, hits = sanitize_text(text or "")
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n（出站内容过长已截断）"
        hits.append("truncated")
    return cleaned, hits
