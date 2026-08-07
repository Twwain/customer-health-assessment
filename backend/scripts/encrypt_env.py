"""把 .env 中的敏感 Key 加密为 ``enc:`` 前缀的 Fernet 密文。

用途：部署到远程服务器时，磁盘上不保留明文 API Key。解密密钥（Fernet key）
单独存放在服务器 root 目录（如 /root/.ch_secret，权限 600），由
``docker-compose.yml`` 挂载进容器，后端启动时用 ``config._decrypt_env`` 解密。

用法：:

    python scripts/encrypt_env.py                     # 就地加密 .env（自动生成密钥到 ./.ch_secret）
    python scripts/encrypt_env.py --env .env.prod --key-file /root/.ch_secret
    python scripts/encrypt_env.py --dry-run           # 只打印将加密的键，不写文件

密钥文件不存在时自动生成（POSIX 权限 600）；已存在的密文键不会被重复加密。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from cryptography.fernet import Fernet


SENSITIVE_KEYS = ("LLM_API_KEY", "LLM_EMBEDDING_API_KEY", "EMBEDDING_API_KEY")


def _warn_if_key_inside_project(key_file: str) -> None:
    """密钥文件落在项目目录内时提示风险（默认位置已进 .gitignore/.dockerignore）。"""
    try:
        repo_root = Path(__file__).resolve().parents[2]
        key_abs = os.path.abspath(key_file)
        if os.path.commonpath([key_abs, str(repo_root)]) == str(repo_root):
            print(
                "提示：密钥文件位于项目目录内（已被 .gitignore / .dockerignore 忽略）。"
                "生产部署建议用 --key-file 放到项目外（如 /root/.ch_secret）。"
            )
    except (ValueError, OSError):
        pass


def _clean_value(raw: str) -> str:
    """按 dotenv 语义解析 .env 值：去掉引号包裹（含转义）与行内注释。

    例：`"sk-abc"` -> `sk-abc`；`sk-abc # comment` -> `sk-abc`；
    `"a #b"` -> `a #b`（引号内注释保留）；`"a\\"b"` -> `a"b`；
    `sk-abc#no-space` 保持原样（# 前必须有空白才算注释）。
    """
    v = raw.strip()
    if v.startswith('"'):
        end = v.rfind('"', 1)
        if end > 0:
            return v[1:end].replace('\\"', '"').replace("\\\\", "\\").strip()
    elif v.startswith("'"):
        end = v.rfind("'", 1)
        if end > 0:
            return v[1:end].strip()
    # 未加引号：去掉行内注释（要求 # 前有空白，符合 dotenv 惯例）
    return re.sub(r"\s+#.*$", "", v).strip()


def load_or_create_key(key_file: str) -> bytes:
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    try:
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except OSError:
        # 非 POSIX（如 Windows）不支持 600 权限，退回普通写入
        with open(key_file, "wb") as f:
            f.write(key)
    return key


def process_env(env_path: str, key_file: str, dry_run: bool = False) -> None:
    key = load_or_create_key(key_file)
    _warn_if_key_inside_project(key_file)
    f = Fernet(key)
    with open(env_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    out_lines: list[str] = []
    changed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = _clean_value(v)
        if k in SENSITIVE_KEYS and v and not v.startswith("enc:"):
            enc = f.encrypt(v.encode()).decode()
            out_lines.append(f"{k}=enc:{enc}\n")
            changed.append(k)
            continue
        out_lines.append(line)

    if dry_run:
        print("Dry-run：将加密的键：", changed if changed else "（无）")
        print("密钥文件：", key_file, "（已存在）" if os.path.exists(key_file) else "（将生成）")
        return

    with open(env_path, "w", encoding="utf-8") as fh:
        fh.writelines(out_lines)
    print(f"完成：加密 {len(changed)} 个键 -> {env_path}")
    print(f"解密密钥文件：{key_file}（请确保服务器上权限为 600）")


def main() -> None:
    parser = argparse.ArgumentParser(description="加密 .env 中的敏感 API Key")
    parser.add_argument("--env", default=".env", help="要加密的 .env 文件路径（默认 ./.env）")
    parser.add_argument("--key-file", default="./.ch_secret", help="Fernet 密钥文件路径（不存在则生成）")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写盘")
    args = parser.parse_args()

    if not os.path.exists(args.env):
        print(f"找不到 .env 文件：{args.env}", file=sys.stderr)
        sys.exit(1)
    process_env(args.env, args.key_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
