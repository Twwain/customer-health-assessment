"""入口请求体与 AI 突发限流中间件测试。"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from request_limits import AIRateLimitMiddleware, UploadBodyLimitMiddleware


def test_upload_body_limit_rejects_before_endpoint():
    app = FastAPI()
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=10)
    called = False

    @app.post("/api/knowledge/upload")
    async def upload(request: Request):
        nonlocal called
        called = True
        return {"size": len(await request.body())}

    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge/upload",
            content=b"12345678901",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 413
    assert called is False


def test_upload_body_limit_counts_stream_when_content_length_is_invalid():
    app = FastAPI()
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=10)
    completed = False

    @app.post("/api/knowledge/upload")
    async def upload(request: Request):
        nonlocal completed
        await request.body()
        completed = True
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge/upload",
            content=b"12345678901",
            headers={"Content-Length": "invalid"},
        )
    assert response.status_code == 413
    assert completed is False


def test_upload_body_limit_does_not_affect_other_routes():
    app = FastAPI()
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=10)

    @app.post("/api/other")
    async def other(request: Request):
        return {"size": len(await request.body())}

    with TestClient(app) as client:
        response = client.post("/api/other", content=b"x" * 100)
    assert response.status_code == 200
    assert response.json()["size"] == 100


def test_ai_rate_limit_is_per_ip_and_only_covers_heavy_routes():
    app = FastAPI()
    app.add_middleware(AIRateLimitMiddleware, requests_per_minute=2)

    @app.post("/api/chat/sessions/demo/messages")
    async def generate():
        return {"ok": True}

    @app.get("/api/knowledge/status")
    async def status():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.post("/api/chat/sessions/demo/messages").status_code == 200
        assert client.post("/api/chat/sessions/demo/messages").status_code == 200
        limited = client.post("/api/chat/sessions/demo/messages")
        assert client.get("/api/knowledge/status").status_code == 200
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
