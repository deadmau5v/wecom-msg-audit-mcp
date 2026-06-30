#!/usr/bin/env bash
# 一键重启 MCP 服务（默认使用 tmux 后台运行，可通过环境变量定制）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SESSION_NAME="${MCP_TMUX_SESSION:-wx-mcp}"
SERVER_PATTERN="${MCP_SERVER_PATTERN:-mcp_server.py}"

# MCP 启动参数（可通过同名环境变量覆盖）
export MCP_TRANSPORT="${MCP_TRANSPORT:-http}"
export MCP_HOST="${MCP_HOST:-127.0.0.1}"
export MCP_PORT="${MCP_PORT:-8331}"

echo "Stopping MCP server (pattern: $SERVER_PATTERN)..."
pkill -f "$SERVER_PATTERN" 2>/dev/null || true
sleep 2

if pgrep -f "$SERVER_PATTERN" >/dev/null; then
    echo "Force killing..."
    pkill -9 -f "$SERVER_PATTERN" 2>/dev/null || true
    sleep 1
fi

tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

echo "Starting MCP server in tmux session '$SESSION_NAME' (http://$MCP_HOST:$MCP_PORT)..."
tmux new-session -d -s "$SESSION_NAME" \
    "MCP_TRANSPORT=$MCP_TRANSPORT MCP_HOST=$MCP_HOST MCP_PORT=$MCP_PORT uv run python mcp_server.py"

sleep 3
echo ""
tmux capture-pane -t "$SESSION_NAME" -p | tail -15
