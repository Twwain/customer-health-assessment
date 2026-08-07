#!/bin/bash
# 一键部署脚本 — 本地构建镜像 → 传输到服务器 → 加载并重启
#
# 用法：
#   DEPLOY_SERVER=root@<IP> bash deploy.sh                      # 常规部署（服务器无配置时自动同步）
#   DEPLOY_SERVER=root@<IP> SYNC_CONFIG=1 bash deploy.sh        # 强制重新同步部署配置
#   DEPLOY_SERVER=root@<IP> SYNC_CONFIG=0 bash deploy.sh        # 只更新镜像，不动服务器配置
#   DEPLOY_SERVER=root@<IP> SYNC_DATA=1 bash deploy.sh          # 额外把本地 data/（数据库/知识库）同步到服务器
#
# 说明：
#   - 部署配置指 docker-compose.yml、.env（来自加密的 .env.prod）、.ch_secret（密钥文件）
#   - 未生成 .env.prod / .ch_secret 时会报错并提示，绝不自动使用明文 .env
set -e

SERVER="${DEPLOY_SERVER:?请设置 DEPLOY_SERVER，例如 DEPLOY_SERVER=root@1.2.3.4}"
SSH_OPTS="-o LogLevel=ERROR"
PROJECT_DIR="/root/customer-health"
IMAGE_FILE="/tmp/ch-image.tar.gz"
SYNC_CONFIG="${SYNC_CONFIG:-auto}"
SYNC_DATA="${SYNC_DATA:-0}"
ENV_PROD="./.env.prod"
SECRET_FILE="./.ch_secret"
# 退出时清理本地临时镜像（成功路径下文件已删除，rm -f 幂等）
trap 'rm -f "$IMAGE_FILE"' EXIT

server_has_config() {
  ssh $SSH_OPTS "$SERVER" \
    "test -f $PROJECT_DIR/docker-compose.yml && test -f $PROJECT_DIR/.env && echo ok" 2>/dev/null \
    | grep -q ok
}

echo "=== 0/3 检查部署配置 ==="
if [ "$SYNC_CONFIG" = "1" ] || { [ "$SYNC_CONFIG" = "auto" ] && ! server_has_config; }; then
  [ -f "$ENV_PROD" ] || {
    echo "错误：未找到 $ENV_PROD（加密部署配置）。请先执行：" >&2
    echo "  cp .env .env.prod && python backend/scripts/encrypt_env.py --env .env.prod --key-file ./.ch_secret" >&2
    exit 1
  }
  grep -qE '^(export[[:space:]]+)?(LLM_API_KEY|LLM_EMBEDDING_API_KEY|EMBEDDING_API_KEY)=enc:' "$ENV_PROD" || {
    echo "错误：$ENV_PROD 中未发现 enc: 加密的 API Key，拒绝同步明文配置。" >&2
    echo "请先执行：python backend/scripts/encrypt_env.py --env .env.prod --key-file ./.ch_secret" >&2
    exit 1
  }
  [ -f "$SECRET_FILE" ] || {
    echo "错误：未找到 $SECRET_FILE（密钥文件）。请先执行上面的加密步骤生成。" >&2
    exit 1
  }
  echo "同步部署配置到服务器 $PROJECT_DIR ..."
  ssh $SSH_OPTS "$SERVER" "mkdir -p $PROJECT_DIR"
  scp $SSH_OPTS docker-compose.yml "$SERVER:$PROJECT_DIR/docker-compose.yml"
  scp $SSH_OPTS "$ENV_PROD" "$SERVER:$PROJECT_DIR/.env"
  scp $SSH_OPTS "$SECRET_FILE" "$SERVER:$PROJECT_DIR/.ch_secret"
  ssh $SSH_OPTS "$SERVER" "chmod 600 $PROJECT_DIR/.env $PROJECT_DIR/.ch_secret"
  echo "部署配置同步完成"
else
  echo "跳过配置同步（SYNC_CONFIG=$SYNC_CONFIG，服务器已有配置）"
fi

if [ "$SYNC_DATA" = "1" ]; then
  if [ -d "./data" ]; then
    echo "警告：将直接拷贝运行中的 data/（SQLite WAL 模式可能拿到不一致快照）。" >&2
    echo "建议先停止本地服务或用 sqlite3 的 .backup 命令生成一致副本后同步。" >&2
    echo "同步本地 data/ 到服务器 ..."
    scp $SSH_OPTS -r ./data "$SERVER:$PROJECT_DIR/"
  else
    echo "警告：本地无 data/ 目录，跳过数据同步"
  fi
fi

echo "=== 1/3 本地构建镜像 ==="
docker compose build

echo "=== 2/3 导出并传输镜像 ==="
APP_IMAGE="customer-health-assessment-app:latest"
docker image inspect "$APP_IMAGE" >/dev/null 2>&1 || {
  echo "错误：未找到镜像 $APP_IMAGE，请先执行 docker compose build。" >&2
  exit 1
}
docker save "$APP_IMAGE" | gzip > "$IMAGE_FILE"
scp $SSH_OPTS "$IMAGE_FILE" "$SERVER:/root/"
rm "$IMAGE_FILE"

echo "=== 3/3 服务器加载并重启 ==="
ssh $SSH_OPTS "$SERVER" \
  "docker load -i /root/ch-image.tar.gz && cd $PROJECT_DIR && docker compose up -d && docker compose ps"

echo "=== 部署完成 ==="
