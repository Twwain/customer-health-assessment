#!/bin/bash
# 客情评估智能体 - 一键启动
# 用法: bash start.sh    (Ctrl+C 同时停止前后端)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cleanup() {
    echo ""
    echo "正在停止所有服务..."
    kill $BACKEND_PID 2>/dev/null
    kill $FRONTEND_PID 2>/dev/null
    wait $BACKEND_PID 2>/dev/null
    wait $FRONTEND_PID 2>/dev/null
    echo "已停止"
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "==================================="
echo "  客情评估智能体"
echo "  后端: http://localhost:8000"
echo "  前端: http://localhost:5173"
echo "  Ctrl+C 停止所有服务"
echo "==================================="

cd "$SCRIPT_DIR/backend"
python -m uvicorn main:app --port 8000 &
BACKEND_PID=$!

cd "$SCRIPT_DIR/frontend"
npx vite --host 0.0.0.0 --port 5173 &
FRONTEND_PID=$!

# 等前端就绪后自动打开浏览器
sleep 5
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    # Git Bash / Cygwin on Windows
    start http://localhost:5173
elif command -v open &>/dev/null; then
    open http://localhost:5173
elif command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:5173
fi

wait
