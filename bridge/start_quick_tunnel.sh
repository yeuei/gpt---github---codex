#!/usr/bin/env bash
set -euo pipefail

bridge_root="$(cd "$(dirname "$0")" && pwd)"
cloudflared_bin="${CLOUDFLARED_BIN:-}"
if [[ -z "$cloudflared_bin" ]] && command -v cloudflared >/dev/null 2>&1; then
  cloudflared_bin="$(command -v cloudflared)"
fi
if [[ -z "$cloudflared_bin" && -x "/Users/fy/.codex/bin/cloudflared" ]]; then
  cloudflared_bin="/Users/fy/.codex/bin/cloudflared"
fi
if [[ -z "$cloudflared_bin" || ! -x "$cloudflared_bin" ]]; then
  echo "cloudflared is required. Install it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" >&2
  exit 2
fi
if [[ -z "${GITEE_ACCESS_TOKEN:-}" ]]; then
  echo "GITEE_ACCESS_TOKEN is not set; export it in this shell first." >&2
  exit 2
fi

port="${BRIDGE_PORT:-48765}"
log_file="$(mktemp -t gitee-bridge-cloudflared.XXXXXX.log)"
cleanup() {
  [[ -n "${bridge_pid:-}" ]] && kill "$bridge_pid" 2>/dev/null || true
  [[ -n "${tunnel_pid:-}" ]] && kill "$tunnel_pid" 2>/dev/null || true
  rm -f "$log_file"
}
trap cleanup EXIT INT TERM

"$cloudflared_bin" tunnel --url "http://127.0.0.1:$port" >"$log_file" 2>&1 &
tunnel_pid=$!
public_url=""
for _ in {1..30}; do
  public_url="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$log_file" | head -n 1 || true)"
  [[ -n "$public_url" ]] && break
  sleep 1
done
if [[ -z "$public_url" ]]; then
  echo "Could not obtain a trycloudflare.com URL. Tunnel log:" >&2
  sed -n '1,80p' "$log_file" >&2
  exit 1
fi

echo "ChatGPT MCP endpoint: $public_url/mcp"
echo "Open the endpoint in ChatGPT; the OAuth page will ask for the Bridge pairing code."
PYTHONUNBUFFERED=1 BRIDGE_PUBLIC_URL="$public_url" BRIDGE_PORT="$port" GITEE_MCP_URL="${GITEE_MCP_URL:-https://api.gitee.com/mcp}" \
  python3 "$bridge_root/gitee_bridge.py" &
bridge_pid=$!
wait "$bridge_pid"
