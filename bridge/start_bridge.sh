#!/usr/bin/env bash
set -euo pipefail

bridge_root="$(cd "$(dirname "$0")" && pwd)"
if [[ -z "${GITEE_ACCESS_TOKEN:-}" ]]; then
  echo "GITEE_ACCESS_TOKEN is not set; export it in this shell first." >&2
  exit 2
fi

export BRIDGE_HOST="${BRIDGE_HOST:-127.0.0.1}"
export BRIDGE_PORT="${BRIDGE_PORT:-48765}"
export GITEE_MCP_URL="${GITEE_MCP_URL:-https://api.gitee.com/mcp}"
exec python3 "$bridge_root/gitee_bridge.py"
