import sys
from pathlib import Path

import pytest

import config

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _offline_embedding(monkeypatch):
    """测试默认离线：清除 embedding key，避免沙箱网络超时拖慢/污染用例。

    需要真实 embedding 的用例自行注入 embed_func（见 test_rag 的 fake_embed）。
    """
    monkeypatch.setattr(config, "EMBEDDING_API_KEY", "")
