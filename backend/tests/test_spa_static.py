from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import SPAStaticFiles


@pytest.fixture
def spa_client(tmp_path: Path) -> Iterator[TestClient]:
    static_dir = tmp_path / "static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>SPA INDEX</html>", encoding="utf-8")
    (static_dir / "favicon.svg").write_text("<svg>favicon</svg>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('app')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("OUTSIDE-STATIC-SECRET", encoding="utf-8")

    test_app = FastAPI()

    @test_app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    test_app.mount("/", SPAStaticFiles(directory=static_dir), name="frontend")
    with TestClient(test_app) as client:
        yield client


def test_static_files_and_spa_deep_links(spa_client: TestClient) -> None:
    assert spa_client.get("/assets/app.js").text == "console.log('app')"
    assert spa_client.get("/favicon.svg").text == "<svg>favicon</svg>"
    assert spa_client.get("/assets/missing.js").status_code == 404

    for path in ("/", "/customers", "/chat/session-1", "/knowledge", "/unknown.png"):
        response = spa_client.get(path)
        assert response.status_code == 200
        assert response.text == "<html>SPA INDEX</html>"


def test_api_routes_keep_json_semantics(spa_client: TestClient) -> None:
    assert spa_client.get("/api/health").json() == {"ok": True}

    response = spa_client.get("/api/not-found")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/secret.txt",
        "/%2e%2e%2fsecret.txt",
        "/%252e%252e%252fsecret.txt",
        "/..%5csecret.txt",
        "/%252e%252e%255csecret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
    ],
)
def test_spa_paths_cannot_escape_static_root(spa_client: TestClient, path: str) -> None:
    response = spa_client.get(path)

    assert response.status_code in {200, 404}
    assert "OUTSIDE-STATIC-SECRET" not in response.text
    if response.status_code == 200:
        assert response.text == "<html>SPA INDEX</html>"


@pytest.mark.parametrize(
    "path",
    [
        "/D:%5Csecret.txt",
        "/D:secret.txt",
        "/%5Csecret.txt",
        "/%5C%5Cattacker.invalid%5Cshare%5Csecret.txt",
        "/%5C%5C%3F%5CC:%5Csecret.txt",
    ],
)
def test_windows_absolute_paths_are_rejected(spa_client: TestClient, path: str) -> None:
    response = spa_client.get(path)

    assert response.status_code == 404
    assert "OUTSIDE-STATIC-SECRET" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/%00",
        "/%2500",
        "/%3A",
        "/foo%7Cbar",
        "/foo%3Fbar",
        "/foo%2Abar",
    ],
)
def test_invalid_filesystem_characters_are_rejected(spa_client: TestClient, path: str) -> None:
    response = spa_client.get(path)

    assert response.status_code == 404
    assert "OUTSIDE-STATIC-SECRET" not in response.text
