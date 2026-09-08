#!/usr/bin/env python3
"""Small OAuth-protected MCP bridge for ChatGPT Web -> Gitee MCP.

The bridge deliberately has no third-party dependencies.  It exposes a
Streamable HTTP MCP endpoint locally and forwards requests to Gitee's official
remote MCP server while keeping the Gitee PAT on this machine.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, Optional, Tuple


DEFAULT_UPSTREAM = "https://api.gitee.com/mcp"
DEFAULT_GITEE_API = "https://gitee.com/api/v5"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48765
PAIRING_TTL_SECONDS = 5 * 60
AUTH_CODE_TTL_SECONDS = 5 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
SAFE_TOOL_RE = re.compile(r"^(?:get|list|search|find|check|view|read|show|compare|count|fetch)_[a-z0-9_]+$")
WRITE_TOOL_NAMES = frozenset({"create_repository", "create_branch", "create_or_update_file", "commit_files"})


def _csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _safe_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    upstream_url: str
    gitee_api_url: str
    public_url: str
    gitee_token: str
    allowed_repositories: frozenset[str]
    allowed_tools: frozenset[str]
    pairing_code: str
    write_enabled: bool

    @classmethod
    def from_env(cls) -> "Config":
        public_url = os.environ.get("BRIDGE_PUBLIC_URL", "").strip().rstrip("/")
        pairing = os.environ.get("BRIDGE_PAIRING_CODE", "").strip().upper()
        if pairing and not re.fullmatch(r"[A-Z0-9]{8}", pairing):
            raise ValueError("BRIDGE_PAIRING_CODE must be exactly 8 letters/digits")
        return cls(
            host=os.environ.get("BRIDGE_HOST", DEFAULT_HOST),
            port=int(os.environ.get("BRIDGE_PORT", str(DEFAULT_PORT))),
            upstream_url=os.environ.get("GITEE_MCP_URL", DEFAULT_UPSTREAM).rstrip("/"),
            gitee_api_url=os.environ.get("GITEE_API_URL", DEFAULT_GITEE_API).rstrip("/"),
            public_url=public_url,
            gitee_token=os.environ.get("GITEE_ACCESS_TOKEN", "").strip(),
            allowed_repositories=frozenset(_csv(os.environ.get("BRIDGE_ALLOWED_REPOSITORIES", ""))),
            allowed_tools=frozenset(_csv(os.environ.get("BRIDGE_ALLOWED_TOOLS", ""))),
            pairing_code=pairing or "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)),
            write_enabled=os.environ.get("BRIDGE_WRITE_ENABLED", "").strip().lower() in {"1", "true", "yes"},
        )


class PairingManager:
    def __init__(self, code: str) -> None:
        self.code = code
        self.expires_at = time.time() + PAIRING_TTL_SECONDS
        self.used = False
        self.failures = 0
        self.lock = threading.Lock()

    def consume(self, candidate: str) -> bool:
        with self.lock:
            if self.used or time.time() >= self.expires_at or self.failures >= 5:
                return False
            if not _safe_equal(candidate.strip().upper(), self.code):
                self.failures += 1
                return False
            self.used = True
            return True


class OAuthStore:
    def __init__(self) -> None:
        self.clients: Dict[str, dict[str, Any]] = {}
        self.auth_codes: Dict[str, dict[str, Any]] = {}
        self.access_tokens: Dict[str, dict[str, Any]] = {}
        self.refresh_tokens: Dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def register(self, client_name: str, redirect_uris: Iterable[str]) -> dict[str, Any]:
        uris = [str(uri) for uri in redirect_uris if isinstance(uri, str) and uri]
        if not uris:
            raise ValueError("redirect_uris is required")
        client_id = "cli_" + secrets.token_urlsafe(18)
        client = {"client_id": client_id, "client_name": client_name or "ChatGPT", "redirect_uris": uris}
        with self.lock:
            self.clients[client_id] = client
        return client

    def get_client(self, client_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            return self.clients.get(client_id)

    def issue_code(self, client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
        code = "ac_" + secrets.token_urlsafe(24)
        with self.lock:
            self.auth_codes[code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "challenge": challenge,
                "state": state,
                "expires_at": time.time() + AUTH_CODE_TTL_SECONDS,
                "used": False,
            }
        return code

    def redeem_code(self, code: str, client_id: str, redirect_uri: str, verifier: str) -> Optional[dict[str, Any]]:
        with self.lock:
            item = self.auth_codes.get(code)
            if not item or item["used"] or item["expires_at"] < time.time():
                return None
            if item["client_id"] != client_id or item["redirect_uri"] != redirect_uri:
                return None
            if not _safe_equal(_pkce_s256(verifier), item["challenge"]):
                return None
            item["used"] = True
            return dict(item)

    def issue_tokens(self, client_id: str) -> dict[str, Any]:
        access = "at_" + secrets.token_urlsafe(32)
        refresh = "rt_" + secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.access_tokens[access] = {"client_id": client_id, "expires_at": now + ACCESS_TOKEN_TTL_SECONDS}
            self.refresh_tokens[refresh] = {"client_id": client_id, "expires_at": now + REFRESH_TOKEN_TTL_SECONDS}
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": ACCESS_TOKEN_TTL_SECONDS}

    def refresh(self, refresh: str, client_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            item = self.refresh_tokens.get(refresh)
            if not item or item["expires_at"] < time.time() or item["client_id"] != client_id:
                return None
            del self.refresh_tokens[refresh]
        return self.issue_tokens(client_id)

    def valid_access(self, token: str) -> bool:
        with self.lock:
            item = self.access_tokens.get(token)
            return bool(item and item["expires_at"] >= time.time())

    def revoke(self, token: str) -> None:
        with self.lock:
            self.access_tokens.pop(token, None)
            self.refresh_tokens.pop(token, None)


class Policy:
    def __init__(self, config: Config) -> None:
        self.repositories = config.allowed_repositories
        self.tools = config.allowed_tools
        self.write_enabled = config.write_enabled

    def allows_tool(self, name: str) -> bool:
        if name in WRITE_TOOL_NAMES:
            return self.write_enabled and (not self.tools or name in self.tools)
        if self.tools:
            return name in self.tools
        return bool(SAFE_TOOL_RE.fullmatch(name))

    def allows_arguments(self, arguments: Any) -> bool:
        if not self.repositories or not isinstance(arguments, dict):
            return True
        owner = arguments.get("owner") or arguments.get("namespace") or arguments.get("org")
        repo = arguments.get("repo") or arguments.get("repository") or arguments.get("repo_name")
        if isinstance(owner, str) and isinstance(repo, str):
            return f"{owner}/{repo}" in self.repositories
        # Repository-independent read-only calls (for example get_user_info)
        # remain usable; repo-scoped tools must provide both fields.
        return not any(key in arguments for key in ("owner", "namespace", "org", "repo", "repository", "repo_name"))

    def filter_tools(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            result = dict(result)
            result["tools"] = [tool for tool in result["tools"] if isinstance(tool, dict) and self.allows_tool(str(tool.get("name", "")))]
            output = dict(payload)
            output["result"] = result
            return output
        return payload


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.pairing = PairingManager(config.pairing_code)
        self.oauth = OAuthStore()
        self.policy = Policy(config)

    def base_url(self, handler: BaseHTTPRequestHandler) -> str:
        if self.config.public_url:
            return self.config.public_url
        host = handler.headers.get("Host", f"{self.config.host}:{self.config.port}")
        return f"http://{host}"

    def metadata(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        base = self.base_url(handler)
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }

    def protected_resource(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        base = self.base_url(handler)
        return {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": ["mcp"]}

    def write_tools(self) -> list[dict[str, Any]]:
        """Tools implemented by this Bridge via Gitee's V5 REST API.

        They are deliberately absent until BRIDGE_WRITE_ENABLED=1.  The Gitee
        remote MCP currently exposes read tools only, so forwarding it cannot
        create repositories or commits.
        """
        if not self.config.write_enabled:
            return []
        confirmation = {"type": "boolean", "description": "Must be true after the user has explicitly approved this write operation."}
        return [
            {
                "name": "create_repository",
                "description": "Create a Gitee repository for the authenticated user. This writes remotely; set confirm=true only after explicit user approval.",
                "inputSchema": {"type": "object", "required": ["name", "confirm"], "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "private": {"type": "boolean"}, "auto_init": {"type": "boolean", "description": "Initialize the repository with its first commit; defaults to true."}, "has_issues": {"type": "boolean"}, "has_wiki": {"type": "boolean"}, "confirm": confirmation}},
            },
            {
                "name": "create_branch",
                "description": "Create a branch from an existing Gitee branch. This writes remotely; set confirm=true only after explicit user approval.",
                "inputSchema": {"type": "object", "required": ["owner", "repo", "branch", "from_branch", "confirm"], "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "branch": {"type": "string"}, "from_branch": {"type": "string"}, "confirm": confirmation}},
            },
            {
                "name": "create_or_update_file",
                "description": "Create or update one UTF-8 text file and commit it. content is plain text: do not Base64-encode it. This writes remotely; set confirm=true only after explicit user approval.",
                "inputSchema": {"type": "object", "required": ["owner", "repo", "path", "content", "message", "confirm"], "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}, "message": {"type": "string"}, "branch": {"type": "string"}, "sha": {"type": "string", "description": "Optional current file SHA; otherwise the Bridge looks it up for updates."}, "confirm": confirmation}},
            },
            {
                "name": "commit_files",
                "description": "Commit multiple file changes in one Gitee commit. Every files[].content value is plain UTF-8 text, not Base64. This writes remotely; set confirm=true only after explicit user approval.",
                "inputSchema": {"type": "object", "required": ["owner", "repo", "branch", "message", "files", "confirm"], "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "branch": {"type": "string"}, "message": {"type": "string"}, "files": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "action": {"type": "string", "enum": ["create", "update", "delete"]}, "sha": {"type": "string"}}}}, "confirm": confirmation}},
            },
        ]

    def gitee_api(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> tuple[int, Any]:
        if not self.config.gitee_token:
            return 503, {"error": "GITEE_ACCESS_TOKEN is not configured"}
        data = _json_bytes(payload) if payload is not None else None
        request = urllib.request.Request(
            f"{self.config.gitee_api_url}{path}",
            data=data,
            headers={"Authorization": f"Bearer {self.config.gitee_token}", "Accept": "application/json", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return response.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body) if body else {"error": exc.reason}
            except json.JSONDecodeError:
                return exc.code, {"error": exc.reason}
        except (urllib.error.URLError, TimeoutError) as exc:
            return 502, {"error": "Gitee API unavailable", "detail": str(exc.reason if isinstance(exc, urllib.error.URLError) else exc)}

    @staticmethod
    def api_path(*segments: str) -> str:
        return "/" + "/".join(urllib.parse.quote(segment, safe="/") for segment in segments)

    @staticmethod
    def text_content(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    def custom_write(self, name: str, arguments: dict[str, Any]) -> tuple[int, Any]:
        if not self.config.write_enabled:
            return 403, {"error": "Bridge write tools are disabled; set BRIDGE_WRITE_ENABLED=1 and restart the Bridge."}
        if arguments.get("confirm") is not True:
            return 400, {"error": "confirm must be true for a remote write operation"}
        if name == "create_repository":
            payload = {key: arguments[key] for key in ("name", "description", "private", "has_issues", "has_wiki") if key in arguments}
            payload["auto_init"] = arguments.get("auto_init", True)
            return self.gitee_api("POST", "/user/repos", payload)
        owner, repo = str(arguments.get("owner", "")), str(arguments.get("repo", ""))
        if not owner or not repo:
            return 400, {"error": "owner and repo are required"}
        repo_path = self.api_path("repos", owner, repo)
        if name == "create_branch":
            return self.gitee_api("POST", f"{repo_path}/branches", {"branch_name": arguments.get("branch"), "refs": arguments.get("from_branch")})
        if name == "create_or_update_file":
            path = str(arguments.get("path", "")).strip("/")
            if not path:
                return 400, {"error": "path is required"}
            branch = arguments.get("branch")
            sha = arguments.get("sha")
            encoded_path = self.api_path("repos", owner, repo, "contents", path)
            if not sha:
                lookup_path = encoded_path + ("?" + urllib.parse.urlencode({"ref": branch}) if branch else "")
                lookup_status, lookup = self.gitee_api("GET", lookup_path)
                if 200 <= lookup_status < 300 and isinstance(lookup, dict):
                    sha = lookup.get("sha")
            payload = {"content": self.text_content(str(arguments.get("content", ""))), "message": arguments.get("message"), "branch": branch}
            payload = {key: value for key, value in payload.items() if value is not None}
            if sha:
                payload["sha"] = sha
                return self.gitee_api("PUT", encoded_path, payload)
            return self.gitee_api("POST", encoded_path, payload)
        if name == "commit_files":
            files = arguments.get("files")
            if not isinstance(files, list) or not files or len(files) > 100:
                return 400, {"error": "files must contain 1 to 100 entries"}
            actions = []
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].strip("/"):
                    return 400, {"error": "each file requires a non-empty path"}
                action = item.get("action", "create")
                if action not in {"create", "update", "delete"}:
                    return 400, {"error": "file action must be create, update, or delete"}
                change: dict[str, Any] = {"action": action, "path": item["path"].strip("/")}
                if action != "delete":
                    if not isinstance(item.get("content"), str):
                        return 400, {"error": "non-delete file actions require plain-text content"}
                    change["content"] = self.text_content(item["content"])
                if item.get("sha"):
                    change["sha"] = item["sha"]
                actions.append(change)
            return self.gitee_api("POST", f"{repo_path}/commits", {"branch": arguments.get("branch"), "message": arguments.get("message"), "actions": actions})
        return 404, {"error": f"Unknown custom tool: {name}"}

    def upstream(self, method: str, body: Optional[bytes], headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        if not self.config.gitee_token:
            return 503, {"Content-Type": "application/json"}, _json_bytes({"error": "GITEE_ACCESS_TOKEN is not configured"})
        request_headers = {
            "Authorization": f"Bearer {self.config.gitee_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        for key in ("MCP-Protocol-Version", "Mcp-Session-Id", "Last-Event-ID"):
            if headers.get(key):
                request_headers[key] = headers[key]
        request = urllib.request.Request(self.config.upstream_url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response_headers = {k: v for k, v in response.headers.items() if k.lower() in {"content-type", "mcp-session-id", "last-event-id", "cache-control"}}
                return response.status, response_headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {"Content-Type": exc.headers.get("Content-Type", "application/json")}, exc.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            return 502, {"Content-Type": "application/json"}, _json_bytes({"error": "Gitee MCP upstream unavailable", "detail": str(exc.reason if isinstance(exc, urllib.error.URLError) else exc)})


class Handler(BaseHTTPRequestHandler):
    server_version = "GiteeChatGPTBridge/0.1"

    @property
    def bridge(self) -> Bridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Do not log query strings, authorization headers, or request bodies.
        print(f"[bridge] {self.command} {self.path.split('?', 1)[0]} - {fmt % args}")

    def send_bytes(self, status: int, body: bytes, content_type: str = "application/json", headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: Any) -> None:
        self.send_bytes(status, _json_bytes(payload))

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    def require_access(self) -> bool:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer ") or not self.bridge.oauth.valid_access(value[7:].strip()):
            self.send_json(401, {"error": "invalid_token", "error_description": "Valid OAuth access token required"})
            return False
        return True

    def do_OPTIONS(self) -> None:
        self.send_bytes(204, b"", headers={"Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version, Mcp-Session-Id"})

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self.send_json(200, {"ok": True, "service": "gitee-chatgpt-bridge", "upstream": self.bridge.config.upstream_url, "gitee_token_configured": bool(self.bridge.config.gitee_token), "write_enabled": self.bridge.config.write_enabled, "pairing_required": not self.bridge.pairing.used})
            return
        if path == "/":
            body = "<h1>Gitee ChatGPT Bridge</h1><p>Use <code>/mcp</code> as the ChatGPT connector endpoint.</p><p>The one-time pairing code is printed only in the local Bridge terminal.</p>"
            self.send_bytes(200, body.encode(), "text/html; charset=utf-8")
            return
        if path == "/.well-known/oauth-protected-resource/mcp":
            self.send_json(200, self.bridge.protected_resource(self))
            return
        if path == "/.well-known/oauth-authorization-server":
            self.send_json(200, self.bridge.metadata(self))
            return
        if path == "/oauth/authorize":
            self.render_authorize()
            return
        if path == "/mcp":
            if not self.require_access():
                return
            status, headers, body = self.bridge.upstream("GET", None, dict(self.headers.items()))
            self.send_bytes(status, body, headers.pop("Content-Type", "application/json"), headers)
            return
        self.send_json(404, {"error": "not_found"})

    def render_authorize(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        client_id = query.get("client_id", [""])[0]
        redirect_uri = query.get("redirect_uri", [""])[0]
        challenge = query.get("code_challenge", [""])[0]
        state = query.get("state", [""])[0]
        client = self.bridge.oauth.get_client(client_id)
        if not client or redirect_uri not in client["redirect_uris"] or query.get("response_type", [""])[0] != "code" or query.get("code_challenge_method", [""])[0] != "S256":
            self.send_bytes(400, b"Invalid OAuth authorization request", "text/plain; charset=utf-8")
            return
        values = tuple(html.escape(value, quote=True) for value in (client_id, redirect_uri, challenge, state))
        form = """<!doctype html><meta charset='utf-8'><title>连接 Gitee</title>
        <style>body{font:16px system-ui;max-width:520px;margin:48px auto;padding:0 20px}input,button{font:inherit;padding:10px;width:100%%;box-sizing:border-box;margin:8px 0}button{background:#2563eb;color:white;border:0;border-radius:8px}</style>
        <h1>连接 Gitee MCP</h1><p>输入本机 Bridge 显示的 8 位配对码。配对码 5 分钟内有效且只能使用一次。</p>
        <form method='post' action='/oauth/authorize'><input name='pairing_code' autocomplete='one-time-code' placeholder='配对码' required>
        <input type='hidden' name='client_id' value='%s'><input type='hidden' name='redirect_uri' value='%s'><input type='hidden' name='code_challenge' value='%s'><input type='hidden' name='state' value='%s'><button>允许连接</button></form>""" % values
        self.send_bytes(200, form.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self.read_body()
        except ValueError as exc:
            self.send_json(413, {"error": str(exc)})
            return
        if path == "/oauth/register":
            self.handle_register(body)
        elif path == "/oauth/authorize":
            self.handle_authorize(body)
        elif path == "/oauth/token":
            self.handle_token(body)
        elif path == "/oauth/revoke":
            self.handle_revoke(body)
        elif path == "/mcp":
            self.handle_mcp(body)
        else:
            self.send_json(404, {"error": "not_found"})

    def form_body(self, body: bytes) -> Dict[str, str]:
        parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def handle_register(self, body: bytes) -> None:
        try:
            payload = json.loads(body or b"{}")
            client = self.bridge.oauth.register(str(payload.get("client_name", "ChatGPT")), payload.get("redirect_uris", []))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": "invalid_client_metadata", "error_description": str(exc)})
            return
        self.send_json(201, {**client, "token_endpoint_auth_method": "none", "client_secret_expires_at": 0})

    def handle_authorize(self, body: bytes) -> None:
        form = self.form_body(body)
        client = self.bridge.oauth.get_client(form.get("client_id", ""))
        redirect = form.get("redirect_uri", "")
        if not client or redirect not in client["redirect_uris"]:
            self.send_bytes(400, b"Invalid client or redirect URI", "text/plain; charset=utf-8")
            return
        if not self.bridge.pairing.consume(form.get("pairing_code", "")):
            self.send_bytes(403, b"Pairing code is invalid, expired, or already used", "text/plain; charset=utf-8")
            return
        code = self.bridge.oauth.issue_code(form["client_id"], redirect, form["code_challenge"], form.get("state", ""))
        query = {"code": code}
        if form.get("state"):
            query["state"] = form["state"]
        location = redirect + ("&" if "?" in redirect else "?") + urllib.parse.urlencode(query)
        self.send_bytes(302, b"", headers={"Location": location})

    def handle_token(self, body: bytes) -> None:
        form = self.form_body(body)
        grant = form.get("grant_type", "")
        if grant == "authorization_code":
            redeemed = self.bridge.oauth.redeem_code(form.get("code", ""), form.get("client_id", ""), form.get("redirect_uri", ""), form.get("code_verifier", ""))
            if not redeemed:
                self.send_json(400, {"error": "invalid_grant"})
                return
            self.send_json(200, self.bridge.oauth.issue_tokens(redeemed["client_id"]))
            return
        if grant == "refresh_token":
            tokens = self.bridge.oauth.refresh(form.get("refresh_token", ""), form.get("client_id", ""))
            if not tokens:
                self.send_json(400, {"error": "invalid_grant"})
                return
            self.send_json(200, tokens)
            return
        self.send_json(400, {"error": "unsupported_grant_type"})

    def handle_revoke(self, body: bytes) -> None:
        form = self.form_body(body)
        self.bridge.oauth.revoke(form.get("token", ""))
        self.send_bytes(200, b"")

    def handle_mcp(self, body: bytes) -> None:
        if not self.require_access():
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_json(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Invalid JSON"}, "id": None})
            return
        if isinstance(payload, dict) and payload.get("method") == "tools/call":
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            tool = str(params.get("name", ""))
            arguments = params.get("arguments", {})
            if tool in WRITE_TOOL_NAMES:
                if not isinstance(arguments, dict):
                    self.send_json(400, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32602, "message": "Tool arguments must be an object"}})
                    return
                status, result = self.bridge.custom_write(tool, arguments)
                response = {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": not 200 <= status < 300}}
                self.send_json(status, response)
                return
            if not self.bridge.policy.allows_tool(tool):
                self.send_json(403, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32001, "message": "Tool is not allowed by bridge policy"}})
                return
            if not self.bridge.policy.allows_arguments(arguments):
                self.send_json(403, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32002, "message": "Repository is not allowed by bridge policy"}})
                return
        status, headers, response_body = self.bridge.upstream("POST", body, dict(self.headers.items()))
        content_type = headers.pop("Content-Type", "application/json")
        if "json" in content_type:
            try:
                response_payload = json.loads(response_body)
                if isinstance(payload, dict) and payload.get("method") == "tools/list":
                    response_payload = self.bridge.policy.filter_tools(response_payload)
                    result = response_payload.get("result") if isinstance(response_payload, dict) else None
                    if isinstance(result, dict):
                        result = dict(result)
                        result["tools"] = list(result.get("tools", [])) + self.bridge.write_tools()
                        response_payload = dict(response_payload)
                        response_payload["result"] = result
                response_body = _json_bytes(response_payload)
            except json.JSONDecodeError:
                pass
        self.send_bytes(status, response_body, content_type, headers)


def main() -> None:
    config = Config.from_env()
    bridge = Bridge(config)
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.bridge = bridge  # type: ignore[attr-defined]
    print(f"Gitee ChatGPT Bridge listening on http://{config.host}:{config.port}")
    print(f"Pairing code: {config.pairing_code} (expires in {PAIRING_TTL_SECONDS // 60} minutes)")
    print(f"Upstream: {config.upstream_url}; token configured: {'yes' if config.gitee_token else 'no'}; write tools: {'enabled' if config.write_enabled else 'disabled'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
