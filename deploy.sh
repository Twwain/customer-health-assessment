#!/bin/bash
# 一键部署脚本 — 本地构建镜像 → 传输到服务器 → 加载并重启
set -e

SERVER="root@8.140.217.197"
SSH_OPTS="-o LogLevel=ERROR"
PROJECT_DIR="/root/customer-health"
IMAGE_FILE="/tmp/ch-image.tar.gz"

echo "=== 1/3 本地构建镜像 ==="
docker compose build

echo "=== 2/3 导出并传输镜像 ==="
docker save customer-health-assessment-app | gzip > "$IMAGE_FILE"
scp $SSH_OPTS "$IMAGE_FILE" "$SERVER:/root/"
rm "$IMAGE_FILE"

echo "=== 3/3 服务器加载并重启 ==="
ssh $SSH_OPTS "$SERVER" "docker load -i /root/ch-image.tar.gz && cd $PROJECT_DIR && docker compose up -d && docker compose ps"

echo "=== 部署完成 ==="
