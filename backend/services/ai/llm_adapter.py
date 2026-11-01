"""统一 LLM 适配层。

结构化对话使用单次请求，向量化保留有界重试；两者统一走大模型兼容协议
（/chat/completions 与 /embeddings），换服务商只需改 ``.env`` 里的
BASE_URL / MODEL / API_KEY，不改业务代码。

可用性约定：
- 未配置 API Key、缺少 httpx、开关关闭、网络异常、重试耗尽
  → 一律抛 ``LLMUnavailableError``，由上层 ``chat_engine`` 降级为规则引擎回复。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

import config

try:  # httpx 是可选依赖：没装时适配层直接判定为不可用，系统仍可降级运行
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


# 进程级共享信号量：所有适配器实例共用，避免通过重复构造客户端绕过上限。
_CHAT_LLM_SEM = threading.BoundedSemaphore(config.CHAT_LLM_GLOBAL_CONCURRENCY)
_EMBEDDING_SEM = threading.BoundedSemaphore(config.EMBEDDING_GLOBAL_CONCURRENCY)
_STREAM_READER_JOIN_TIMEOUT = 0.5


@contextmanager
def _chat_slot(
    cancel_event: threading.Event | None,
    deadline: float | None,
    stop_event: threading.Event | None = None,
):
    """可取消地等待全局 LLM 槽位；租约必须由真实网络工作线程持有。"""
    acquired = False
    try:
        while not acquired:
            if stop_event is not None and stop_event.is_set():
                raise LLMError("generation cancelled")
            if cancel_event is not None and cancel_event.is_set():
                raise LLMError("generation cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise LLMError("AI 生成超过总时限，已取消")
            acquired = _CHAT_LLM_SEM.acquire(timeout=0.05)
        yield
    finally:
        if acquired:
            _CHAT_LLM_SEM.release()


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
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff = retry_backoff
        self.enabled = enabled
        # 由配置决定是否请求 usage；Chat 不做隐式参数重试。
        self._stream_usage = stream_usage
        # 兼容网关可能实现 /embeddings 却不接受 OpenAI 的 dimensions 参数。
        # 只有收到明确的参数拒绝后才缓存能力，避免每个 batch 重复触发 400。
        self._embedding_dimensions_supported: bool | None = None

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

    def _timeout(self, deadline: float | None = None):
        total = self.timeout
        if deadline is not None:
            total = max(0.1, min(total, deadline - time.monotonic()))
        return httpx.Timeout(total, connect=min(self.connect_timeout, total))

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

    @staticmethod
    def _rejects_parameter(detail: str, parameter: str) -> bool:
        """只在错误正文明确点名不支持的参数时做能力降级。"""
        lowered = (detail or "").lower()
        markers = (
            "unknown", "unsupported", "unrecognized", "not support",
            "not permitted", "extra field", "unexpected", "invalid parameter",
        )
        named = parameter.lower() in lowered
        if parameter == "dimensions":
            named = named or "dimension" in lowered
        return named and any(marker in lowered for marker in markers)


    def chat_completion_once(self, messages, *, max_tokens, cancel_event=None, deadline=None):
        """Exactly one HTTP attempt, buffering private output.

        No transport/parameter retries are hidden outside the run attempt budget.
        Reuse the cancellable transport and its global concurrency lease.
        """
        self._ensure_available()
        payload = {
            "model": self.model,
            "messages": [m.to_payload() if isinstance(m, LLMMessage) else dict(m) for m in messages],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": True,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        if self._stream_usage:
            payload["stream_options"] = {"include_usage": True}
        usage = {}
        try:
            text = "".join(self._iter_stream(payload, lambda u: usage.update(u),
                                           cancel_event=cancel_event, deadline=deadline))
        except _StreamFailure as exc:
            raise LLMUnavailableError("AI v2 provider request failed") from exc
        return ChatResult(content=text, model=self.model,
            tokens_used=int(usage.get("total_tokens") or estimate_tokens(text)), raw_usage=usage,
            finish_reason=usage.get("finish_reason", ""), latency_ms=0)

    def _post_with_retry(self, path: str, payload: dict) -> dict:
        last_error: Exception | None = None
        attempt = 0
        while True:
            # The v2 retrieval lease is checked after queueing and before each
            # physical request, including parameter and transport retries.
            if path.endswith("/embeddings"):
                gate = getattr(_embedding_observation, "before_http", None)
                if gate is not None:
                    gate()
            try:
                if path.endswith("/embeddings"):
                    observation = getattr(_embedding_observation, "value", None)
                    if observation is not None:
                        observation["http_attempts"] += 1
                with httpx.Client(timeout=self._timeout()) as client:
                    response = client.post(
                        f"{self.base_url}{path}", headers=self._headers(), json=payload
                    )
                if response.status_code >= 400:
                    detail = response.text[:300]
                    if (
                        response.status_code == 400
                        and path.endswith("/embeddings")
                        and "dimensions" in payload
                        and self._rejects_parameter(detail, "dimensions")
                    ):
                        self._embedding_dimensions_supported = False
                        payload = {k: v for k, v in payload.items() if k != "dimensions"}
                        last_error = LLMError(f"HTTP 400: {detail}")
                        continue
                    if self._retryable_status(response.status_code) and attempt < self.max_retries:
                        attempt += 1
                        last_error = LLMError(f"HTTP {response.status_code}: {detail}")
                        time.sleep(self.retry_backoff * (2 ** (attempt - 1)))
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
                    attempt += 1
                    time.sleep(self.retry_backoff * (2 ** (attempt - 1)))
                    continue
                raise LLMUnavailableError(f"LLM 请求失败：{last_error}") from exc

    # ── 流式 ────────────────────────────────────────────────────────────


    def _iter_stream(
        self, payload: dict, on_usage, *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> Iterator[str]:
        stream_usage: dict[str, Any] = {}
        events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=256)
        stop_reader = threading.Event()
        response_box: dict[str, Any] = {}
        client = httpx.Client(timeout=self._timeout(deadline))

        def _offer(kind: str, value: Any) -> bool:
            while not stop_reader.is_set():
                try:
                    events.put((kind, value), timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def _network_reader() -> None:
            try:
                # 整个请求（包括等待响应头）都在可关闭的工作线程中。主消费线程可在
                # cancel/deadline 到达时关闭 client，而不必等到响应头返回后才启动取消器。
                # 并发租约由真正执行 HTTP 的线程持有。即使底层 socket
                # 不响应 close，调用方返回也不会把该付费请求的槽位提前释放。
                with _chat_slot(cancel_event, deadline, stop_reader):
                    with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    ) as response:
                        response_box["response"] = response
                        if response.status_code >= 400:
                            body = response.read().decode("utf-8", "ignore")[:300]
                            _offer(
                                "error",
                                _StreamFailure(
                                    reason=self._http_error_message(response.status_code, body),
                                ),
                            )
                            return
                        for line in response.iter_lines():
                            if stop_reader.is_set() or not _offer("line", line):
                                return
            except BaseException as exc:  # 由消费线程统一处理传输失败
                _offer("error", exc)
            finally:
                _offer("done", None)

        reader = threading.Thread(
            target=_network_reader,
            name="llm-stream-reader",
            daemon=True,
        )

        def _close_transport() -> None:
            stop_reader.set()
            response = response_box.get("response")
            close_response = getattr(response, "close", None)
            if callable(close_response):
                try:
                    close_response()
                except Exception:
                    pass
            close_client = getattr(client, "close", None)
            if callable(close_client):
                try:
                    close_client()
                except Exception:
                    pass

        reader.start()
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    _close_transport()
                    raise LLMError("generation cancelled")
                if deadline is not None and time.monotonic() >= deadline:
                    _close_transport()
                    raise LLMError("AI 生成超过总时限，已取消")
                try:
                    kind, value = events.get(timeout=0.05)
                except queue.Empty:
                    if not reader.is_alive():
                        break
                    continue
                if kind == "done":
                    break
                if kind == "error":
                    raise value
                line = value
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
                    stream_usage.update(usage)
                    on_usage(dict(stream_usage))
                for choice in chunk.get("choices") or []:
                    if choice.get("finish_reason") and on_usage:
                        stream_usage["finish_reason"] = choice["finish_reason"]
                        on_usage(dict(stream_usage))
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield content
            reader.join(timeout=_STREAM_READER_JOIN_TIMEOUT)
        except (_StreamFailure, GeneratorExit):
            raise
        except LLMError:
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise LLMError("generation cancelled") from exc
            if deadline is not None and time.monotonic() >= deadline:
                raise LLMError("AI 生成超过总时限，已取消") from exc
            raise _StreamFailure(
                reason=str(exc)
            ) from exc
        finally:
            _close_transport()
            reader.join(timeout=_STREAM_READER_JOIN_TIMEOUT)

    # ── Embedding ───────────────────────────────────────────────────────

    def embed(self, texts: Sequence[str], *, dimensions: int | None = None) -> list[list[float]]:
        """批量向量化（大模型兼容 /embeddings 协议）。"""
        self._ensure_available()
        inputs = [t for t in texts]
        if not inputs:
            return []
        _embedding_observation.value = dict(http_attempts=0, model=self.model,
            total_tokens=sum(estimate_tokens(t) for t in inputs), usage_estimated=True)
        payload: dict[str, Any] = {"model": self.model, "input": inputs}
        if dimensions and self._embedding_dimensions_supported is not False:
            payload["dimensions"] = dimensions
        try:
            with _EMBEDDING_SEM:
                data = self._post_with_retry("/embeddings", payload)
        finally:
            if not _embedding_observation.value['http_attempts']:
                _embedding_observation.value.update(total_tokens=0, usage_estimated=False)
        usage = data.get("usage") or {}
        if usage.get("total_tokens") is not None:
            _embedding_observation.value.update(total_tokens=int(usage["total_tokens"]), usage_estimated=False)
        rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
        return [list(r.get("embedding") or []) for r in rows]


class _StreamFailure(Exception):
    def __init__(self, *, reason: str):
        super().__init__(reason)


# ── 单例与注入 ──────────────────────────────────────────────────────────────

_chat_adapter: CompatAdapter | None = None
_embedding_adapter: CompatAdapter | None = None
_embedding_observation = threading.local()


def get_embedding_usage():
    return dict(getattr(_embedding_observation, "value", {}))


def reset_embedding_usage():
    """A retrieval must not inherit a previous operation's thread-local cost."""
    _embedding_observation.value = {}


@contextmanager
def embedding_request_scope(before_http):
    previous = getattr(_embedding_observation, "before_http", None)
    _embedding_observation.before_http = before_http
    try:
        yield
    finally:
        _embedding_observation.before_http = previous


def get_chat_adapter() -> CompatAdapter:
    global _chat_adapter
    if _chat_adapter is None:
        _chat_adapter = CompatAdapter(
            name=config.LLM_PROVIDER,
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            model=config.LLM_MODEL,
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
