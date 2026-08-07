"""密钥加密相关测试：config._decrypt_env / _load_secret_key 与 encrypt_env 脚本。"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import config
from services.scoring.config_loader import strip_sub_dimension_annotation, sub_dimension_of


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "encrypt_env.py"


def _load_encrypt_script():
    spec = importlib.util.spec_from_file_location("encrypt_env", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── _decrypt_env ─────────────────────────────────────────────────────────


def test_decrypt_env_plain_value_passthrough(monkeypatch):
    monkeypatch.delenv("CH_SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY_FILE", raising=False)
    assert config._decrypt_env("sk-plain") == "sk-plain"
    assert config._decrypt_env("") == ""


def test_decrypt_env_with_env_key_roundtrip(monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv("CH_SECRET_KEY", key.decode())
    token = Fernet(key).encrypt(b"sk-secret").decode()
    assert config._decrypt_env(f"enc:{token}") == "sk-secret"


def test_decrypt_env_missing_key_returns_raw_with_warning(monkeypatch, caplog):
    monkeypatch.delenv("CH_SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY_FILE", raising=False)
    with caplog.at_level(logging.WARNING, logger="config"):
        assert config._decrypt_env("enc:some-token") == "enc:some-token"
    assert any("未找到 Fernet 密钥" in r.message for r in caplog.records)


def test_decrypt_env_wrong_key_returns_raw_with_warning(monkeypatch, caplog):
    key = Fernet.generate_key()
    monkeypatch.setenv("CH_SECRET_KEY", key.decode())
    token = Fernet.generate_key()  # 用另一把密钥加密
    cipher = Fernet(token).encrypt(b"sk-secret").decode()
    with caplog.at_level(logging.WARNING, logger="config"):
        assert config._decrypt_env(f"enc:{cipher}", "LLM_API_KEY") == f"enc:{cipher}"
    assert any("解密失败" in r.message for r in caplog.records)
    assert any("LLM_API_KEY" in r.message for r in caplog.records)


# ── _load_secret_key ─────────────────────────────────────────────────────


def test_load_secret_key_from_file(monkeypatch, tmp_path):
    key = Fernet.generate_key()
    key_file = tmp_path / "ch_secret"
    key_file.write_bytes(key + b"\n")
    monkeypatch.delenv("CH_SECRET_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY_FILE", str(key_file))
    assert config._load_secret_key() == key


def test_load_secret_key_falls_back_to_default(monkeypatch, tmp_path):
    key = Fernet.generate_key()
    monkeypatch.delenv("CH_SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config._load_secret_key() is None
    (tmp_path / ".ch_secret").write_bytes(key)
    assert config._load_secret_key() == key


def test_load_secret_key_ignores_directory_without_crash(monkeypatch, tmp_path):
    """Docker 把不存在的宿主文件挂成目录时，读取应跳过而非抛异常。"""
    key_dir = tmp_path / "ch_secret_dir"
    key_dir.mkdir()
    monkeypatch.delenv("CH_SECRET_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY_FILE", str(key_dir))
    monkeypatch.chdir(tmp_path)
    assert config._load_secret_key() is None


# ── encrypt_env 脚本解析 ─────────────────────────────────────────────────


def test_encrypt_env_clean_value():
    mod = _load_encrypt_script()
    assert mod._clean_value("sk-abc") == "sk-abc"
    assert mod._clean_value('"sk-abc"') == "sk-abc"
    assert mod._clean_value("'sk-abc'") == "sk-abc"
    assert mod._clean_value("sk-abc # comment") == "sk-abc"
    assert mod._clean_value('"sk-abc" # comment') == "sk-abc"
    assert mod._clean_value('"a #b"') == "a #b"
    assert mod._clean_value('"a\\"b"') == 'a"b'
    assert mod._clean_value("sk-abc#no-space") == "sk-abc#no-space"
    assert mod._clean_value("  sk-abc  ") == "sk-abc"


def test_encrypt_script_roundtrip_with_config(monkeypatch, tmp_path):
    mod = _load_encrypt_script()
    env_path = tmp_path / ".env"
    env_path.write_text('LLM_API_KEY="sk-abc" # 行内注释\n', encoding="utf-8")
    key_file = tmp_path / "ch_secret"
    mod.process_env(str(env_path), str(key_file))
    monkeypatch.setenv("CH_SECRET_KEY", key_file.read_bytes().decode())
    raw = next(
        line.partition("=")[2].strip()
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("LLM_API_KEY=")
    )
    assert raw.startswith("enc:")
    assert config._decrypt_env(raw) == "sk-abc"


def test_encrypt_script_is_idempotent(tmp_path):
    mod = _load_encrypt_script()
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_API_KEY=sk-abc\n", encoding="utf-8")
    key_file = tmp_path / "ch_secret"
    mod.process_env(str(env_path), str(key_file))
    first = env_path.read_text(encoding="utf-8")
    mod.process_env(str(env_path), str(key_file))
    assert env_path.read_text(encoding="utf-8") == first


# ── 二级维度解析（前后端共用后端实现）────────────────────────────────────


def test_sub_dimension_of():
    assert sub_dimension_of("原子指标：已识别决策链人数占比（二级维度：决策链覆盖度）") == "决策链覆盖度"
    assert sub_dimension_of("加权平均（-1~3，二级维度：决策链覆盖度）") == "决策链覆盖度"
    assert sub_dimension_of("无标注") == ""
    assert sub_dimension_of("") == ""


def test_strip_sub_dimension_annotation():
    assert (
        strip_sub_dimension_annotation("原子指标：已识别决策链人数占比（二级维度：决策链覆盖度）")
        == "原子指标：已识别决策链人数占比"
    )
    assert (
        strip_sub_dimension_annotation("加权平均（-1~3，二级维度：决策链覆盖度）")
        == "加权平均（-1~3）"
    )
    assert (
        strip_sub_dimension_annotation("是否识别 EB（Economic Buyer）（MEDDIC，二级维度：决策链覆盖度）")
        == "是否识别 EB（Economic Buyer）（MEDDIC）"
    )
    assert strip_sub_dimension_annotation("活跃度：高，二级维度：合作深度") == "活跃度：高"
    assert strip_sub_dimension_annotation("无标注") == "无标注"
    assert strip_sub_dimension_annotation("") == ""
