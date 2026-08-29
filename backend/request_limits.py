"""公网匿名部署的入口资源限制 ASGI 中间件。"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _RequestBodyTooLarge(Exception):
    pass


class UploadBodyLimitMiddleware:
    """在 multipart 解析前限制知识上传请求体，避免超大请求先完整落盘。"""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max(1, int(max_body_bytes))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/knowledge/upload"
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"")
        try:
            content_length = int(raw_length) if raw_length else None
        except ValueError:
            content_length = None
        if content_length is not None and content_length > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit_mb = self.max_body_bytes // (1024 * 1024)
        response = JSONResponse(
            status_code=413,
            content={"detail": f"上传请求体超过 {limit_mb}MB 上限"},
        )
        await response(scope, receive, send)


_AI_ROUTES = (
    re.compile(
        r"^/api/chat/sessions/[^/]+/"
        r"(?:messages|evaluate|strategy|alert-analysis|regenerate)$"
    ),
    re.compile(r"^/api/knowledge/(?:search|upload|reindex|reindex/jobs)$"),
    re.compile(r"^/api/assessment/[^/]+/pdf(?:/jobs)?$"),
)


class AIRateLimitMiddleware:
    """单进程、按客户端 IP 的 AI 重型接口固定滑动窗口限流。"""

    def __init__(self, app: ASGIApp, *, requests_per_minute: int) -> None:
        self.app = app
        self.limit = max(1, int(requests_per_minute))
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._requests_seen = 0

    @staticmethod
    def _is_limited(scope: Scope) -> bool:
        if scope["type"] != "http" or scope.get("method") not in {"GET", "POST"}:
            return False
        path = scope.get("path", "")
        return any(pattern.fullmatch(path) for pattern in _AI_ROUTES)

    def _allow(self, client_ip: str, now: float) -> tuple[bool, int]:
        cutoff = now - 60.0
        with self._lock:
            self._requests_seen += 1
            hits = self._hits.setdefault(client_ip, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = max(1, int(60 - (now - hits[0])))
                return False, retry_after
            hits.append(now)
            # 定期清理不再活跃的 IP，避免长期运行时字典只增不减。
            if self._requests_seen % 256 == 0:
                stale = [ip for ip, values in self._hits.items() if not values or values[-1] <= cutoff]
                for ip in stale:
                    self._hits.pop(ip, None)
            return True, 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_limited(scope):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        allowed, retry_after = self._allow(client_ip, time.monotonic())
        if not allowed:
            response = JSONResponse(
                status_code=429,
                content={"detail": "AI 请求过于频繁，请稍后重试"},
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
