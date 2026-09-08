#!/usr/bin/env bash
set -euo pipefail

bridge_root="$(cd "$(dirname "$0")" && pwd)"
# Codex can retain an older inherited environment after the user rotates a
# launchctl value. Prefer the current user launchd values when they exist, but
# never print either secret or its value.
launchctl_token="$(launchctl getenv GITEE_ACCESS_TOKEN 2>/dev/null || true)"
if [[ -n "$launchctl_token" ]]; then
  export GITEE_ACCESS_TOKEN="$launchctl_token"
fi
unset launchctl_token
launchctl_write_enabled="$(launchctl getenv BRIDGE_WRITE_ENABLED 2>/dev/null || true)"
if [[ -n "$launchctl_write_enabled" ]]; then
  export BRIDGE_WRITE_ENABLED="$launchctl_write_enabled"
fi
unset launchctl_write_enabled
if [[ -z "${GITEE_ACCESS_TOKEN:-}" ]]; then
  echo "GITEE_ACCESS_TOKEN is not set; export it in this shell first." >&2
  exit 2
fi

export BRIDGE_HOST="${BRIDGE_HOST:-127.0.0.1}"
export BRIDGE_PORT="${BRIDGE_PORT:-48765}"
export GITEE_MCP_URL="${GITEE_MCP_URL:-https://api.gitee.com/mcp}"
exec python3 "$bridge_root/gitee_bridge.py"
