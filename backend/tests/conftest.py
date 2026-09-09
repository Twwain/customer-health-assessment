import sys
from pathlib import Path

import pytest

import config

sys.path.insert(0, str(Path(__file__).parent.parent))
# 允许测试模块间共享工厂（tests/factories.py 等）
sys.path.insert(0, str(Path(__file__).parent))


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", help="运行真实 LLM/Embedding 接口测试（产生 API 费用）")
    parser.addoption("--live-first-token-seconds", type=float, default=30.0)
    parser.addoption("--live-total-seconds", type=float, default=90.0)
    parser.addoption("--run-live-chroma", action="store_true", help="同时使用 --run-live，运行临时 Chroma 链路")
    parser.addoption("--run-live-eval", action="store_true", help="同时使用 --run-live，运行检索评测集")
    parser.addoption("--live-eval-repeats", type=int, default=3)
    parser.addoption("--live-eval-p95-seconds", type=float, default=5.0)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: 需要显式启用的真实外部模型测试")
    config.addinivalue_line("markers", "live_chroma: 低频临时 Chroma 在线测试")
    config.addinivalue_line("markers", "live_eval: 低频检索质量与性能评测")


def pytest_collection_modifyitems(config, items):
    for item in items:
        for marker, option in (("live_chroma", "--run-live-chroma"), ("live_eval", "--run-live-eval")):
            if item.get_closest_marker(marker) and not config.getoption(option):
                item.add_marker(pytest.mark.skip(reason=f"低频在线测试需 {option} 和 --run-live"))
    if not config.getoption("--run-live"):
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(pytest.mark.skip(reason="真实接口测试需 --run-live"))


@pytest.fixture(autouse=True)
def _offline_embedding(monkeypatch, request):
    """测试默认离线：清除 embedding key，避免沙箱网络超时拖慢/污染用例。

    live 用例显式启用后保留配置；其余用例注入假 embed_func。
    """
    if not (request.node.get_closest_marker("live") and request.config.getoption("--run-live")):
        monkeypatch.setattr(config, "EMBEDDING_API_KEY", "")
