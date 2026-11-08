#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
PID_FILE="data/gateway.pid"
if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "已停止"
else
    echo "没有 pid 文件，尝试按端口查找"
    pkill -f "gateway.py" 2>/dev/null || echo "没有在跑的 gateway"
fi