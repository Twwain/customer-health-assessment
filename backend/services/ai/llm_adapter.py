"""统一 LLM 适配层。

只暴露一个 ``chat_completion`` 语义：对话与向量化统一走大模型兼容协议
（/chat/completions 与 /embeddings），换服务商只需改 ``.env`` 里的
BASE_URL / MODEL / API_KEY，不改业务代码。

可用性约定：
- 未配置 API Key、缺少 httpx、开关关闭、网络异常、重试耗尽
  → 一律抛 ``LLMUnavailableError``，由上层 ``chat_engine`` 降级为规则引擎回复。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

import config

try:  # httpx 是可选依赖：没装时适配层直接判定为不可用，系统仍可降级运行
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


# ── 异常 ────────────────────────────────────────────────────────────────────


class LLMError(RuntimeError):
    """LLM 调用异常基类。"""


class LLMUnavailableError(LLMError):
    """LLM 不可用（未配置 / 网络故障 / 重试耗尽）→ 触发降级。"""


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass
class LLMMessage:
    role: str
    content: str

    def to_payload(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatResult:
    content: str = ""
    model: str = ""
    tokens_used: int = 0
    finish_reason: str = ""
    latency_ms: int = 0
    raw_usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def normalize_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """把大模型响应的 tool_calls 归一化为 {id, name, arguments} 列表。"""
    normalized: list[dict[str, Any]] = []
    for tc in raw or []:
        function = tc.get("function") or {}
        normalized.append(
            {
                "id": tc.get("id") or "",
                "name": function.get("name") or "",
                "arguments": function.get("arguments") or "{}",
            }
        )
    return normalized


def estimate_tokens(text: str) -> int:
    """粗略 Token 估算：中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk + (len(text) - cjk) / 4)


# ── 大模型兼容适配器 ───────────────────────────────────────────────────────


class CompatAdapter:
    """大模型兼容协议客户端（任意兼容网关通用）。"""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        timeout: float = 60.0,
        connect_timeout: float = 8.0,
        max_retries: int = 2,
        retry_backoff: float = 0.6,
        enabled: bool = True,
        stream_usage: bool = True,
    ):
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff = retry_backoff
        self.enabled = enabled
        # 部分网关不认 stream_options，握手报 400 后自动关闭并重试
        self._stream_usage = stream_usage

    # ── 可用性 ──────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url and httpx is not None)

    def unavailable_reason(self) -> str:
        if not self.enabled:
            return "LLM 已通过 LLM_ENABLED 关闭"
        if httpx is None:
            return "未安装 httpx 依赖"
        if not self.base_url:
            return "未配置 BASE_URL"
        if not self.api_key:
            return "未配置 API Key"
        return ""

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "available": self.available,
            "reason": self.unavailable_reason(),
        }

    def _ensure_available(self) -> None:
        if not self.available:
            raise LLMUnavailableError(self.unavailable_reason() or "LLM 不可用")

    # ── HTTP ────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _timeout(self):
        return httpx.Timeout(self.timeout, connect=self.connect_timeout)

    def _payload(
        self,
        messages: Sequence[LLMMessage | dict],
        *,
        stream: bool,
        temperature: float | None,
        max_tokens: int | None,
        extra: dict | None,
        tools: Sequence[dict] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_payload() if isinstance(m, LLMMessage) else dict(m) for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }
        if stream and self._stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = list(tools)
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    @staticmethod
    def _http_error_message(status_code: int, detail: str) -> str:
        """把 HTTP 错误翻译成可排障的提示。

        4xx 多为配置问题（Key 错 / 模型名错 / 请求参数非法），重试无意义，
        提示语需要与"服务不可用"区分开，避免被降级文案掩盖。
        """
        if status_code in (401, 403):
            return f"LLM 认证失败（HTTP {status_code}），请检查 API Key 配置：{detail}"
        if status_code == 404:
            return f"LLM 接口或模型不存在（HTTP 404），请检查 BASE_URL / 模型名：{detail}"
        if status_code == 400:
            return f"LLM 请求参数被拒绝（HTTP 400），请检查模型名与请求体：{detail}"
        return f"LLM 返回 HTTP {status_code}：{detail}"

    # ── 非流式 ──────────────────────────────────────────────────────────

    def chat_completion(
        self,
        messages: Sequence[LLMMessage | dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict | None = None,
        tools: Sequence[dict] | None = None,
    ) -> ChatResult:
        """一次性返回完整回复。"""
        self._ensure_available()
        payload = self._payload(
            messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=extra,
            tools=tools,
        )
        started = time.monotonic()
        data = self._post_with_retry("/chat/completions", payload)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        usage = data.get("usage") or {}
        return ChatResult(
            content=content,
            model=data.get("model") or self.model,
            tokens_used=int(usage.get("total_tokens") or 0) or estimate_tokens(content),
            finish_reason=choice.get("finish_reason") or "",
            latency_ms=int((time.monotonic() - started) * 1000),
            raw_usage=usage,
            tool_calls=normalize_tool_calls(message.get("tool_calls")),
        )

    def _post_with_retry(self, path: str, payload: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self._timeout()) as client:
                    response = client.post(
                        f"{self.base_url}{path}", headers=self._headers(), json=payload
                    )
                if response.status_code >= 400:
                    detail = response.text[:300]
                    if response.status_code == 400 and "tools" in payload:
                        # 供应商不支持 function calling：去掉 tools 重试一次，
                        # 避免整条对话被降级为规则引擎。
                        payload = {k: v for k, v in payload.items() if k != "tools"}
                        last_error = LLMError(f"HTTP 400: {detail}")
                        continue
                    if self._retryable_status(response.status_code) and attempt < self.max_retries:
                        last_error = LLMError(f"HTTP {response.status_code}: {detail}")
                        time.sleep(self.retry_backoff * (2**attempt))
                        continue
                    raise LLMUnavailableError(
                        self._http_error_message(response.status_code, detail)
                    )
                return response.json()
            except LLMUnavailableError:
                raise
            except Exception as exc:  # httpx.TimeoutException / ConnectError / JSON 解析等
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (2**attempt))
                    continue
        raise LLMUnavailableError(f"LLM 请求失败：{last_error}")

    # ── 流式 ────────────────────────────────────────────────────────────

    def stream_chat_completion(
        self,
        messages: Sequence[LLMMessage | dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict | None = None,
        on_usage=None,
        tools: Sequence[dict] | None = None,
        on_tool_calls=None,
    ) -> Iterator[str]:
        """逐 Token 产出文本增量。

        ``on_usage`` 可选回调，流结束时回传 usage 字典（用于 Token 统计）。
        首个增量到达前的错误一律转成 ``LLMUnavailableError``，便于上层降级；
        已开始输出后再断流则抛 ``LLMError``（此时已有部分内容，不再重来一遍）。
        """
        self._ensure_available()

        attempt = 0
        while True:
            payload = self._payload(
                messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
                extra=extra,
                tools=tools,
            )
            try:
                yield from self._iter_stream(payload, on_usage, on_tool_calls)
                return
            except _StreamStarted as exc:  # 已经吐过字，不重试，交给上层收尾
                raise LLMError(str(exc.__cause__ or exc)) from exc
            except _StreamRetry as exc:
                if exc.disable_tools and tools:
                    # 供应商不支持 function calling：去掉 tools 重试一次，
                    # 避免整条对话被降级为规则引擎。
                    tools = None
                    continue
                # 网关不认 stream_options：关掉后立刻重来一次，不计入重试次数
                if exc.disable_stream_usage and self._stream_usage:
                    self._stream_usage = False
                    continue
                if exc.retryable and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(self.retry_backoff * (2**attempt))
                    continue
                raise LLMUnavailableError(f"LLM 流式请求失败：{exc.reason}") from exc

    def _iter_stream(self, payload: dict, on_usage, on_tool_calls=None) -> Iterator[str]:
        emitted = False
        tool_acc: dict[int, dict[str, Any]] = {}
        try:
            with httpx.Client(timeout=self._timeout()) as client:
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", "ignore")[:300]
                        raise _StreamRetry(
                            reason=self._http_error_message(response.status_code, body),
                            disable_stream_usage=(
                                response.status_code == 400 and "stream_options" in payload
                            ),
                            disable_tools=(response.status_code == 400 and "tools" in payload),
                            retryable=self._retryable_status(response.status_code),
                        )
                    for line in response.iter_lines():
                        if not line:
                            continue
                        line = line.strip()
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if not line or line == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        usage = chunk.get("usage")
                        if usage and on_usage:
                            on_usage(usage)
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            content = delta.get("content")
                            if content:
                                emitted = True
                                yield content
                            for tc in delta.get("tool_calls") or []:
                                emitted = True
                                index = int(tc.get("index", 0))
                                entry = tool_acc.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                if tc.get("id"):
                                    entry["id"] = tc["id"]
                                function = tc.get("function") or {}
                                if function.get("name"):
                                    entry["name"] += function["name"]
                                entry["arguments"] += function.get("arguments") or ""
            if tool_acc and on_tool_calls:
                on_tool_calls([tool_acc[i] for i in sorted(tool_acc)])
        except (_StreamRetry, GeneratorExit):
            raise
        except Exception as exc:
            if emitted:
                raise _StreamStarted() from exc
            raise _StreamRetry(
                reason=str(exc), disable_stream_usage=False, disable_tools=False, retryable=True
            ) from exc

    # ── Embedding ───────────────────────────────────────────────────────

    def embed(self, texts: Sequence[str], *, dimensions: int | None = None) -> list[list[float]]:
        """批量向量化（大模型兼容 /embeddings 协议）。"""
        self._ensure_available()
        inputs = [t for t in texts]
        if not inputs:
            return []
        payload: dict[str, Any] = {"model": self.model, "input": inputs}
        if dimensions:
            payload["dimensions"] = dimensions
        data = self._post_with_retry("/embeddings", payload)
        rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
        return [list(r.get("embedding") or []) for r in rows]


class _StreamRetry(Exception):
    def __init__(
        self, *, reason: str, disable_stream_usage: bool, disable_tools: bool, retryable: bool
    ):
        super().__init__(reason)
        self.reason = reason
        self.disable_stream_usage = disable_stream_usage
        self.disable_tools = disable_tools
        self.retryable = retryable


class _StreamStarted(Exception):
    """流已经开始输出后才出错，不重试。"""


# ── 单例与注入 ──────────────────────────────────────────────────────────────

_chat_adapter: CompatAdapter | None = None
_embedding_adapter: CompatAdapter | None = None


def get_chat_adapter() -> CompatAdapter:
    global _chat_adapter
    if _chat_adapter is None:
        _chat_adapter = CompatAdapter(
            name=config.LLM_PROVIDER,
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            model=config.LLM_MODEL,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=config.LLM_MAX_TOKENS,
            timeout=config.LLM_TIMEOUT,
            connect_timeout=config.LLM_CONNECT_TIMEOUT,
            max_retries=config.LLM_MAX_RETRIES,
            retry_backoff=config.LLM_RETRY_BACKOFF,
            enabled=config.LLM_ENABLED,
            stream_usage=config.LLM_STREAM_USAGE,
        )
    return _chat_adapter


def get_embedding_adapter() -> CompatAdapter:
    global _embedding_adapter
    if _embedding_adapter is None:
        _embedding_adapter = CompatAdapter(
            name=config.EMBEDDING_PROVIDER,
            base_url=config.EMBEDDING_BASE_URL,
            api_key=config.EMBEDDING_API_KEY,
            model=config.EMBEDDING_MODEL,
            timeout=config.LLM_TIMEOUT,
            connect_timeout=config.LLM_CONNECT_TIMEOUT,
            max_retries=config.LLM_MAX_RETRIES,
            retry_backoff=config.LLM_RETRY_BACKOFF,
            enabled=config.EMBEDDING_ENABLED,
        )
    return _embedding_adapter


def set_chat_adapter(adapter) -> None:
    """注入自定义/测试适配器。"""
    global _chat_adapter
    _chat_adapter = adapter


def set_embedding_adapter(adapter) -> None:
    global _embedding_adapter
    _embedding_adapter = adapter


def reset_adapters() -> None:
    """清空单例，下次访问按最新配置重建（配置热更新 / 测试隔离）。"""
    global _chat_adapter, _embedding_adapter
    _chat_adapter = None
    _embedding_adapter = None


def llm_status() -> dict[str, Any]:
    """供 `/api/chat/status` 使用：前端据此显示"AI 就绪 / 已降级"状态条。"""
    chat = get_chat_adapter()
    chat_status = chat.status() if hasattr(chat, "status") else {"available": True}
    embedding = get_embedding_adapter()
    return {
        "available": bool(chat_status.get("available")),
        "degraded": not bool(chat_status.get("available")),
        "provider": chat_status.get("provider", ""),
        "model": chat_status.get("model", ""),
        "reason": chat_status.get("reason", ""),
        "embedding_provider": embedding.name,
        "embedding_model": embedding.model,
        "embedding_available": embedding.available,
    }


def as_messages(items: Iterable[tuple[str, str] | dict | LLMMessage]) -> list[LLMMessage]:
    """把 (role, content) / dict / LLMMessage 混合列表统一成 LLMMessage。"""
    result: list[LLMMessage] = []
    for item in items:
        if isinstance(item, LLMMessage):
            result.append(item)
        elif isinstance(item, dict):
            result.append(LLMMessage(role=item.get("role", "user"), content=item.get("content", "")))
        else:
            role, content = item
            result.append(LLMMessage(role=role, content=content))
    return result
