#!/usr/bin/env python3
"""Local-first GitHub <-> ChatGPT Web handoff trigger.

The runtime observes the configured local Git clone by default. Network access is
explicit: only ``refresh_from_github`` / the Dashboard "从 GitHub 刷新" action
runs ``git fetch``. Historical task state is always read at the event commit SHA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.local.json"
DEFAULT_DB = ROOT / "state.sqlite3"
DASHBOARD_FILE = ROOT / "dashboard.html"
APPROVAL_DIR = ROOT / "approval-requests"
ORIGIN_RE = re.compile(r"^Coordination-Origin:\s*(agent|chatgpt)\s*$", re.M | re.I)
EVENT_RE = re.compile(r"^Coordination-Event-Id:\s*(\S+)\s*$", re.M | re.I)
CAUSE_RE = re.compile(r"^Coordination-Caused-By:\s*(\S+)\s*$", re.M | re.I)
PR_PATH_RE = re.compile(r"^coordination/PR-(\d+)/")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
BINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(args: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"missing {path}; copy config.example.json and configure it")
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("handoff_repo", "remote", "repository", "chatgpt", "agent"):
        if key not in config:
            raise RuntimeError(f"config missing {key}")
    config.setdefault("watch_branches", "all")
    config.setdefault("poll_interval_seconds", 15)
    config.setdefault("binding", {"require_active": False})
    return config


def pending_approval_requests() -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    try:
        paths = sorted(APPROVAL_DIR.glob("*.json"))
    except OSError:
        return requests
    for path in paths:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("id") and not item.get("decision"):
                item["file"] = path.name
                requests.append(item)
        except (OSError, ValueError, TypeError):
            continue
    return requests


def resolve_approval_request(request_id: str, decision: str) -> bool:
    if decision not in {"accept", "acceptForSession", "decline", "cancel"}:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]+", request_id):
        return False
    path = APPROVAL_DIR / f"{request_id}.json"
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("decision"):
            return False
        item["decision"] = decision
        path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        return True
    except (OSError, ValueError, TypeError):
        return False


class Store:
    """SQLite audit state.

    ``status`` / ``detail`` are retained only as legacy columns so an existing
    state.sqlite3 can be opened without rewriting history. New runtime decisions
    use approval_state and delivery_state independently.
    """

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
              create table if not exists settings (key text primary key, value text not null);
              create table if not exists cursors (key text primary key, value text not null);
              create table if not exists events (
                id integer primary key, event_key text unique not null, sha text not null,
                ref text not null default '', pr_number integer, origin text, caused_by text,
                subject text not null, observed_at text not null, dispatched_at text,
                status text not null default 'legacy', detail text not null default '',
                approval_state text, delivery_state text, delivery_detail text not null default ''
              );
              create table if not exists bindings (
                binding_id text primary key, route_id text, repository text not null, branch text not null,
                pr_number integer not null, web_conversation_id text not null,
                web_conversation_title text not null default '', local_agent_id text,
                local_conversation_id text, local_conversation_title text not null default '',
                status text not null, token_hash text not null, claim_token_hash text,
                created_at text not null, expires_at text not null, updated_at text not null,
                claimed_at text, confirmed_at text, revoked_at text
              );
              create index if not exists idx_bindings_target
                on bindings(repository, branch, pr_number, status);
            """)
            for statement in (
                "alter table events add column ref text not null default ''",
                "alter table events add column approval_state text",
                "alter table events add column delivery_state text",
                "alter table events add column delivery_detail text not null default ''",
            ):
                try:
                    self.db.execute(statement)
                except sqlite3.OperationalError:
                    pass
            defaults = {
                "enabled": True,
                "agent_to_chatgpt": True,
                "chatgpt_to_agent": True,
                "auto_submit": False,
                "approval_required": True,
                "github_pr_states": {},
                "github_pr_states_updated_at": None,
                "github_pr_state_error": "",
            }
            for key, value in defaults.items():
                self.db.execute("insert or ignore into settings values (?, ?)", (key, json.dumps(value)))
            self.db.commit()

    def setting(self, key: str) -> Any:
        with self.lock:
            row = self.db.execute("select value from settings where key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        with self.lock:
            self.db.execute(
                "insert into settings values (?, ?) on conflict(key) do update set value=excluded.value",
                (key, json.dumps(value)),
            )
            self.db.commit()

    def set_settings(self, values: dict[str, Any]) -> None:
        with self.lock:
            for key, value in values.items():
                self.db.execute(
                    "insert into settings values (?, ?) on conflict(key) do update set value=excluded.value",
                    (key, json.dumps(value)),
                )
            self.db.commit()

    def cursor(self, ref: str) -> str | None:
        with self.lock:
            row = self.db.execute("select value from cursors where key=?", (f"git_head:{ref}",)).fetchone()
        return row["value"] if row else None

    def set_cursor(self, ref: str, sha: str) -> None:
        with self.lock:
            self.db.execute(
                "insert into cursors values (?, ?) on conflict(key) do update set value=excluded.value",
                (f"git_head:{ref}", sha),
            )
            self.db.commit()

    def add_event(self, event: dict[str, Any]) -> bool:
        values = dict(event)
        values.setdefault("status", "detected")
        values.setdefault("approval_state", "detected")
        values.setdefault("delivery_state", "not_started")
        values.setdefault("delivery_detail", "")
        with self.lock:
            try:
                self.db.execute(
                    """insert into events(
                         event_key,sha,ref,pr_number,origin,caused_by,subject,observed_at,
                         status,approval_state,delivery_state,delivery_detail)
                       values(:event_key,:sha,:ref,:pr_number,:origin,:caused_by,:subject,:observed_at,
                         :status,:approval_state,:delivery_state,:delivery_detail)""",
                    values,
                )
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def event(self, event_key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("select * from events where event_key=?", (event_key,)).fetchone()
        return dict(row) if row else None

    def set_event_states(
        self,
        event_key: str,
        *,
        approval_state: str | None = None,
        delivery_state: str | None = None,
        delivery_detail: str | None = None,
        legacy_status: str | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if approval_state is not None:
            fields.append("approval_state=?"); params.append(approval_state)
        if delivery_state is not None:
            fields.append("delivery_state=?"); params.append(delivery_state)
        if delivery_detail is not None:
            fields.append("delivery_detail=?"); params.append(delivery_detail[:1000])
            fields.append("detail=?"); params.append(delivery_detail[:1000])
        if legacy_status is not None:
            fields.append("status=?"); params.append(legacy_status)
        if delivery_state in {"filled", "submitted", "agent_started", "needs_human", "skipped"}:
            fields.append("dispatched_at=?"); params.append(now())
        if not fields:
            return
        params.append(event_key)
        with self.lock:
            self.db.execute(f"update events set {', '.join(fields)} where event_key=?", params)
            self.db.commit()

    def pending_events(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "select * from events where approval_state='awaiting' order by id"
            ).fetchall()
        return [dict(row) for row in rows]

    def fill_only_events(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "select * from events where delivery_state='filled' order by id"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _public_event(row: sqlite3.Row | dict[str, Any], pr_states: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        if not item.get("approval_state"):
            item["approval_state"] = "legacy_unknown"
        if not item.get("delivery_state"):
            item["delivery_state"] = "legacy_unknown"
            item["delivery_detail"] = item.get("detail", "")
        pr = item.get("pr_number")
        cached = pr_states.get(str(pr), {}) if pr is not None else {}
        item["github_pr_state"] = cached.get("state", "unknown")
        item["github_pr_state_source"] = cached.get("source", "not_synced")
        item["github_pr_state_updated_at"] = cached.get("updated_at")
        return item

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            rows = self.db.execute("select * from events order by id desc limit 300").fetchall()
            settings = {r["key"]: json.loads(r["value"]) for r in self.db.execute("select * from settings")}
        pr_states = settings.get("github_pr_states") or {}
        events = [self._public_event(row, pr_states) for row in rows]
        return {"settings": settings, "events": events, "updated_at": now()}

    @staticmethod
    def _binding_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item.pop("token_hash", None)
        item.pop("claim_token_hash", None)
        return item

    def _expire_bindings_locked(self, timestamp: str) -> None:
        self.db.execute(
            "update bindings set status='expired', updated_at=? where status in ('pending','claimed') and expires_at<=?",
            (timestamp, timestamp),
        )

    def list_bindings(self, repository: str | None = None) -> list[dict[str, Any]]:
        timestamp = now()
        with self.lock:
            self._expire_bindings_locked(timestamp)
            query = "select * from bindings"
            params: tuple[Any, ...] = ()
            if repository:
                query += " where repository=?"; params = (repository,)
            rows = self.db.execute(query + " order by created_at desc", params).fetchall()
            self.db.commit()
        return [self._binding_public(row) for row in rows]

    def binding(self, binding_id: str) -> dict[str, Any] | None:
        timestamp = now()
        with self.lock:
            self._expire_bindings_locked(timestamp)
            row = self.db.execute("select * from bindings where binding_id=?", (binding_id,)).fetchone()
            self.db.commit()
        return self._binding_public(row) if row else None

    def create_binding(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = now()
        with self.lock:
            self._expire_bindings_locked(timestamp)
            existing = self.db.execute(
                "select binding_id from bindings where repository=? and branch=? and pr_number=? and status='active'",
                (data["repository"], data["branch"], data["pr_number"]),
            ).fetchone()
            if existing:
                self.db.commit()
                return {"ok": False, "status": "conflict", "error": "该仓库/分支/PR 已有 active binding", "binding_id": existing["binding_id"]}
            self.db.execute(
                """insert into bindings(
                  binding_id,repository,branch,pr_number,web_conversation_id,web_conversation_title,
                  status,token_hash,created_at,expires_at,updated_at)
                  values(?,?,?,?,?,?,?,?,?,?,?)""",
                (data["binding_id"], data["repository"], data["branch"], data["pr_number"],
                 data["web_conversation_id"], data.get("web_conversation_title", ""), "pending",
                 data["token_hash"], timestamp, data["expires_at"], timestamp),
            )
            self.db.commit()
            row = self.db.execute("select * from bindings where binding_id=?", (data["binding_id"],)).fetchone()
        return {"ok": True, "status": "pending", "binding": self._binding_public(row), "token": data["token"]}

    def claim_binding(self, binding_id: str, token_hash: str, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = now()
        with self.lock:
            self._expire_bindings_locked(timestamp)
            row = self.db.execute("select * from bindings where binding_id=?", (binding_id,)).fetchone()
            if not row:
                self.db.commit(); return {"ok": False, "status": "not_found", "error": "binding 不存在"}
            if row["status"] != "pending":
                self.db.commit(); return {"ok": False, "status": row["status"], "error": f"binding 当前状态为 {row['status']}，不可认领"}
            if not secrets.compare_digest(row["token_hash"], token_hash):
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "配对 token 无效"}
            if (row["repository"], row["branch"], row["pr_number"]) != (data["repository"], data["branch"], data["pr_number"]):
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "认领目标 repository/branch/PR 不匹配"}
            active = self.db.execute(
                "select binding_id from bindings where repository=? and branch=? and pr_number=? and status='active'",
                (row["repository"], row["branch"], row["pr_number"]),
            ).fetchone()
            if active:
                self.db.execute("update bindings set status='conflict',updated_at=? where binding_id=?", (timestamp, binding_id))
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "该仓库/分支/PR 已有 active binding"}
            claim_token = secrets.token_urlsafe(32)
            self.db.execute(
                """update bindings set status='claimed',route_id=?,local_agent_id=?,local_conversation_id=?,
                   local_conversation_title=?,claim_token_hash=?,claimed_at=?,updated_at=? where binding_id=?""",
                (data["route_id"], data["local_agent_id"], data["local_conversation_id"],
                 data.get("local_conversation_title", ""), hashlib.sha256(claim_token.encode()).hexdigest(),
                 timestamp, timestamp, binding_id),
            )
            self.db.commit()
            updated = self.db.execute("select * from bindings where binding_id=?", (binding_id,)).fetchone()
        return {"ok": True, "status": "claimed", "binding": self._binding_public(updated), "confirm_token": claim_token}

    def confirm_binding(self, binding_id: str, claim_token_hash: str, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = now()
        with self.lock:
            self._expire_bindings_locked(timestamp)
            row = self.db.execute("select * from bindings where binding_id=?", (binding_id,)).fetchone()
            if not row:
                self.db.commit(); return {"ok": False, "status": "not_found", "error": "binding 不存在"}
            if row["status"] != "claimed":
                self.db.commit(); return {"ok": False, "status": row["status"], "error": f"binding 当前状态为 {row['status']}，不可确认"}
            if not row["claim_token_hash"] or not secrets.compare_digest(row["claim_token_hash"], claim_token_hash):
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "confirm token 无效"}
            if (row["repository"], row["branch"], row["pr_number"]) != (data["repository"], data["branch"], data["pr_number"]):
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "确认目标 repository/branch/PR 不匹配"}
            if (row["route_id"], row["local_agent_id"], row["local_conversation_id"]) != (data["route_id"], data["local_agent_id"], data["local_conversation_id"]):
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "确认身份与 claim 不匹配"}
            active = self.db.execute(
                "select binding_id from bindings where repository=? and branch=? and pr_number=? and status='active' and binding_id<>?",
                (row["repository"], row["branch"], row["pr_number"], binding_id),
            ).fetchone()
            if active:
                self.db.execute("update bindings set status='conflict',updated_at=? where binding_id=?", (timestamp, binding_id))
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "确认时发现其它 active binding"}
            self.db.execute(
                "update bindings set status='active',claim_token_hash=NULL,confirmed_at=?,updated_at=? where binding_id=?",
                (timestamp, timestamp, binding_id),
            )
            self.db.commit()
            updated = self.db.execute("select * from bindings where binding_id=?", (binding_id,)).fetchone()
        return {"ok": True, "status": "active", "binding": self._binding_public(updated)}

    def revoke_binding(self, binding_id: str, token_hash: str) -> dict[str, Any]:
        timestamp = now()
        with self.lock:
            self._expire_bindings_locked(timestamp)
            row = self.db.execute("select * from bindings where binding_id=?", (binding_id,)).fetchone()
            if not row:
                self.db.commit(); return {"ok": False, "status": "not_found", "error": "binding 不存在"}
            valid = secrets.compare_digest(row["token_hash"], token_hash) or bool(
                row["claim_token_hash"] and secrets.compare_digest(row["claim_token_hash"], token_hash)
            )
            if not valid:
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "撤销 token 无效"}
            if row["status"] in {"revoked", "expired"}:
                self.db.commit(); return {"ok": False, "status": row["status"], "error": f"binding 当前状态为 {row['status']}"}
            self.db.execute(
                "update bindings set status='revoked',revoked_at=?,updated_at=? where binding_id=?",
                (timestamp, timestamp, binding_id),
            )
            self.db.commit()
            updated = self.db.execute("select * from bindings where binding_id=?", (binding_id,)).fetchone()
        return {"ok": True, "status": "revoked", "binding": self._binding_public(updated)}


@dataclass
class Commit:
    sha: str
    body: str
    subject: str

    @property
    def origin(self) -> str | None:
        match = ORIGIN_RE.search(self.body)
        return match.group(1).lower() if match else None

    @property
    def event_id(self) -> str:
        match = EVENT_RE.search(self.body)
        return match.group(1) if match else f"commit:{self.sha}"

    @property
    def caused_by(self) -> str | None:
        match = CAUSE_RE.search(self.body)
        return match.group(1) if match else None


class GitSource:
    """Read local Git refs/history. No method here performs network I/O."""

    def __init__(self, config: dict[str, Any]):
        raw = Path(config["handoff_repo"])
        self.repo = (raw if raw.is_absolute() else ROOT / raw).resolve()
        self.remote = config["remote"]
        self.watch_branches = config.get("watch_branches", "all")

    def refs(self) -> list[str]:
        names = run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads", f"refs/remotes/{self.remote}"],
            self.repo,
        ).splitlines()
        refs: list[str] = []
        for name in names:
            if name in {self.remote, f"{self.remote}/HEAD"}:
                continue
            branch = name.removeprefix(f"{self.remote}/")
            if self.watch_branches != "all" and branch not in set(self.watch_branches):
                continue
            refs.append(name)
        return sorted(set(refs))

    def poll(self, ref: str, cursor: str | None) -> tuple[str, list[Commit]]:
        head = run(["git", "rev-parse", ref], self.repo)
        if cursor is None:
            return head, []
        if cursor == head:
            return head, []
        try:
            raw = run(
                ["git", "log", "--reverse", "--format=%H%x1f%s%x1f%B%x1e", f"{cursor}..{head}"],
                self.repo,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"本地 Git cursor {cursor[:12]} 无法推进到 {ref}={head[:12]}：{exc}") from exc
        commits: list[Commit] = []
        for record in raw.split("\x1e"):
            if not record.strip():
                continue
            parts = record.strip().split("\x1f", 2)
            if len(parts) != 3:
                continue
            sha, subject, body = parts
            commits.append(Commit(sha=sha, subject=subject, body=body))
        return head, commits

    def pr_number(self, sha: str) -> int | None:
        names = run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha], self.repo)
        for name in names.splitlines():
            match = PR_PATH_RE.match(name)
            if match:
                return int(match.group(1))
        return None


class OpenBrowserUse:
    """Fixed Open Browser Use CLI adapter; no LLM chooses browser actions."""

    def __init__(self, config: dict[str, Any]):
        self.chat = config["chatgpt"]
        seed = "|".join(self.chat.get(key, "") for key in ("browser", "profile", "conversation_url"))
        self.session_id = f"obu-trigger-{hashlib.sha256(seed.encode()).hexdigest()[:12]}"
        self._health_lock = threading.Lock()

    def _common(self) -> list[str]:
        return ["--session-id", self.session_id, "--browser", self.chat.get("browser", "chrome"), "--profile", self.chat.get("profile", "Default")]

    @staticmethod
    def _clear_stale_registry() -> bool:
        registry = Path("/tmp/open-browser-use/active.json")
        try:
            data = json.loads(registry.read_text())
            socket_path = Path(str(data.get("socketPath", "")))
            if socket_path and not socket_path.exists():
                registry.unlink(); return True
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        return False

    def _ping(self, common: list[str]) -> None:
        try:
            run(["open-browser-use", "ping", *common], timeout=15)
        except Exception as first_error:
            if not self._clear_stale_registry():
                raise
            try:
                run(["open-browser-use", "ping", *common], timeout=15)
            except Exception:
                raise first_error

    def check_connection(self) -> dict[str, Any]:
        with self._health_lock:
            checked_at = now()
            try:
                profiles = json.loads(run(["open-browser-use", "profiles", "--connected", "--json"], timeout=10))
                browser = self.chat.get("browser", "chrome").lower(); profile = self.chat.get("profile", "Default").lower()
                matching = [item for item in profiles if item.get("browser", "").lower() == browser and (item.get("directory", "").lower() == profile or item.get("displayName", "").lower() == profile)]
                if not matching:
                    raise RuntimeError(f"未找到已连接的 Chrome 配置：{browser}/{self.chat.get('profile', 'Default')}")
                self._ping(self._common())
                return {"state": "connected", "label": "已连接", "checked_at": checked_at, "target": matching[0].get("target", f"{browser}:{profile}"), "detail": "OBU ping 成功"}
            except Exception as exc:
                return {"state": "disconnected", "label": "无法连接", "checked_at": checked_at, "target": f"{self.chat.get('browser', 'chrome')}:{self.chat.get('profile', 'Default')}", "detail": str(exc)[:500]}

    @staticmethod
    def _result(raw: str, operation: str) -> Any:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{operation} returned invalid JSON: {raw}") from exc
        if payload.get("error"):
            error = payload["error"]
            raise RuntimeError(f"{operation} failed: {error.get('message', error) if isinstance(error, dict) else error}")
        return payload.get("result")

    def _rpc(self, command: list[str], operation: str, timeout: int = 15) -> Any:
        return self._result(run(command, timeout=timeout), operation)

    def _cdp(self, common: list[str], tab_id: int, method: str, params: dict[str, Any]) -> Any:
        raw = run(["open-browser-use", "cdp", *common, "--tab-id", str(tab_id), "--method", method, "--params", json.dumps(params)], timeout=20)
        return self._result(raw, f"CDP {method}")

    def _evaluate(self, common: list[str], tab_id: int, expression: str) -> Any:
        result = self._cdp(common, tab_id, "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if isinstance(result, dict) and result.get("exceptionDetails"):
            raise RuntimeError(f"ChatGPT composer check failed: {result['exceptionDetails'].get('text', 'JavaScript exception')}")
        evaluated = result.get("result", {}) if isinstance(result, dict) else {}
        return evaluated.get("value")

    def attach_unique_chatgpt_tab(self) -> dict[str, Any]:
        """Claim exactly one OBU-visible ChatGPT conversation tab.

        A continuation must never guess between conversations or create a
        second tab.  ``getUserTabs`` is intentionally the sole inventory: it
        only returns tabs Open Browser Use can control for this session.
        """
        common = self._common()
        self._ping(common)
        result = self._rpc(
            ["open-browser-use", "call", *common, "--method", "getUserTabs", "--params", "{}"],
            "getUserTabs",
        )
        tabs = result.get("tabs", []) if isinstance(result, dict) else result
        if not isinstance(tabs, list):
            raise RuntimeError("getUserTabs returned an invalid tab list")
        candidates = []
        for tab in tabs:
            url = str(tab.get("url", ""))
            match = re.match(r"https://(?:chatgpt\.com|chat\.openai\.com)/c/([^/?#]+)", url)
            if match:
                candidates.append((tab, match.group(1)))
        if not candidates:
            raise RuntimeError("未找到受 Open Browser Use 控制的 ChatGPT 对话标签；请只连接一个 /c/ 会话后重试")
        if len(candidates) != 1:
            raise RuntimeError(f"发现 {len(candidates)} 个受 Open Browser Use 控制的 ChatGPT 对话标签；请只保留一个后重试")
        tab, conversation_id = candidates[0]
        tab_id = tab.get("id")
        if not isinstance(tab_id, int):
            raise RuntimeError("ChatGPT tab has no numeric tab id")
        try:
            self._rpc(["open-browser-use", "claim-tab", *common, "--tab-id", str(tab_id)], "claim ChatGPT tab")
        except RuntimeError as exc:
            if f"already part of browser session {self.session_id}" not in str(exc):
                raise
        try:
            title = str(self._evaluate(common, tab_id, "document.title") or "").strip()
        except Exception:
            title = ""
        title = re.sub(r"\s*(?:[-|]\s*)?ChatGPT\s*$", "", title, flags=re.I).strip()
        if not title:
            title = str(tab.get("title", "")).strip()
            title = re.sub(r"\s*(?:[-|]\s*)?ChatGPT\s*$", "", title, flags=re.I).strip()
        url = str(tab.get("url", ""))
        run(["open-browser-use", "finalize-tabs", *common, "--keep", json.dumps([{"tabId": tab_id, "status": "handoff"}])], timeout=15)
        return {"tab_id": tab_id, "conversation_id": conversation_id, "conversation_title": title or "未命名 ChatGPT 对话", "conversation_url": url}

    def dispatch(self, message: str, submit: bool) -> str:
        attached = self.attach_unique_chatgpt_tab()
        url = attached["conversation_url"]
        configured = self.chat.get("conversation_url", "")
        if configured and "REPLACE_" not in configured and configured != url:
            raise RuntimeError("唯一受控 ChatGPT 标签与已绑定的会话不一致；请重新执行“打开 GPT”后再继续")
        self.chat["conversation_url"] = url
        common = self._common(); tab_id = attached["tab_id"]
        editor = '#prompt-textarea[contenteditable="true"]'; handoff_ready = False
        try:
            before = self._evaluate(common, tab_id, """(() => {const e=document.querySelector(%s);if(!e)throw new Error('ChatGPT composer not found');const f=document.querySelector('textarea[placeholder="问问 ChatGPT"], textarea.wcDTda_fallbackTextarea');const text=(e.innerText||e.textContent||f?.value||'').trim();e.focus();return {draftLength:text.length,draftText:text};})()""" % json.dumps(editor))
            if not isinstance(before, dict): raise RuntimeError("could not read ChatGPT composer state")
            normalize = lambda value: re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", value.replace("\r", ""))).strip()
            existing = normalize(str(before.get("draftText", ""))); event_match = re.search(r"(?:^|\n)Event-ID:\s*(\S+)", message); same_event = bool(event_match and event_match.group(1) in existing)
            if existing == normalize(message) or same_event:
                handoff_ready = not submit
                if submit:
                    self._evaluate(common, tab_id, """(() => {const b=document.querySelector('[data-testid="send-button"]');if(!b||b.disabled)throw new Error('send button unavailable');b.click();return 'submitted';})()"""); return "submitted"
                return "filled; verified; waiting for user submit"
            if before.get("draftLength", 0): raise RuntimeError("ChatGPT composer already contains a draft; refusing to overwrite it")
            inserted = self._evaluate(common, tab_id, """(() => {const e=document.querySelector(%s);e.focus();return {inserted:document.execCommand('insertText',false,%s)};})()""" % (json.dumps(editor), json.dumps(message)))
            if not isinstance(inserted, dict) or not inserted.get("inserted"): raise RuntimeError("browser refused to insert the ChatGPT handoff draft")
            verified = self._evaluate(common, tab_id, """(async () => {const norm=v=>v.replace(/\r/g,'').replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim();const expected=norm(%s),deadline=Date.now()+2500;while(Date.now()<deadline){const e=document.querySelector(%s),f=document.querySelector('textarea[placeholder="问问 ChatGPT"], textarea.wcDTda_fallbackTextarea');const vals=[e?.innerText,e?.textContent,f?.value].filter(v=>typeof v==='string').map(norm);if(vals.includes(expected))return {matches:true};await new Promise(r=>setTimeout(r,100));}return {matches:false};})()""" % (json.dumps(message.strip()), json.dumps(editor)))
            if not isinstance(verified, dict) or not verified.get("matches"): raise RuntimeError("ChatGPT composer did not retain the injected handoff; no message was sent")
            handoff_ready = not submit
            if submit:
                self._evaluate(common, tab_id, """(() => {const b=document.querySelector('[data-testid="send-button"]');if(!b||b.disabled)throw new Error('send button unavailable');b.click();return 'submitted';})()"""); return "submitted"
            return "filled; verified; waiting for user submit"
        finally:
            status = "handoff" if handoff_ready else "deliverable"
            run(["open-browser-use", "finalize-tabs", *common, "--keep", json.dumps([{"tabId": tab_id, "status": status}])], timeout=15)


def parse_task_markdown(content: str) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []; current: dict[str, Any] | None = None; in_code = False
    states = {"x": "done", "~": "in-progress", "?": "waiting", "!": "blocked", "-": "superseded", " ": "todo"}
    for line in content.splitlines():
        if line.strip().startswith("```"): in_code = not in_code; continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading and not in_code:
            current = {"title": heading.group(2), "level": len(heading.group(1)), "items": []}; sections.append(current); continue
        task = re.match(r"^\s*-\s+\[([ x~?!-])\]\s+(.+?)\s*$", line, re.I)
        if task and not in_code:
            if current is None: current = {"title": "任务清单", "level": 2, "items": []}; sections.append(current)
            current["items"].append({"state": states.get(task.group(1).lower(), "todo"), "label": task.group(2)})
    sections = [section for section in sections if section["items"]]; items = [item for section in sections for item in section["items"]]
    counts = {state: sum(item["state"] == state for item in items) for state in states.values()}; done = counts["done"]
    return {"summary": f"{done}/{len(items)} 个任务已完成" if items else "未解析到任务复选框", "state_counts": counts, "all_complete": bool(items) and done == len(items), "sections": sections}


class Service:
    def __init__(self, config: dict[str, Any], store: Store):
        self.config, self.store = config, store; self.git, self.browser = GitSource(config), OpenBrowserUse(config)
        self.last_local_error = ""; self.last_refresh_error = ""; self.last_refresh_at: str | None = None
        self.cache_dir = self.git.repo / ".cache"
        self.branch_cache_path = self.cache_dir / "dashboard-branch-cache.json"
        self.browser_status = {"state": "unknown", "label": "未检测", "checked_at": None, "target": f"{self.browser.chat.get('browser','chrome')}:{self.browser.chat.get('profile','Default')}", "detail": "尚未执行连接检测"}

    def auto_mode(self) -> bool: return bool(not self.store.setting("approval_required") and self.store.setting("auto_submit"))

    def repository_status(self) -> dict[str, Any]:
        watched = "全部本地可见分支/远端跟踪分支" if self.git.watch_branches == "all" else ", ".join(self.git.watch_branches)
        try:
            head = run(["git", "rev-parse", "HEAD"], self.git.repo); branch = run(["git", "branch", "--show-current"], self.git.repo) or "detached"
        except Exception as exc:
            head, branch = "", ""; self.last_local_error = str(exc)
        return {"name": self.config.get("repository", ""), "local_path": str(self.git.repo), "remote": self.git.remote, "watch_branches": watched, "local_head": head, "local_branch": branch, "source_mode": "local_git_default", "can_switch_live": False, "last_refresh_at": self.last_refresh_at, "last_refresh_error": self.last_refresh_error}

    def check_browser(self) -> dict[str, Any]: self.browser_status = self.browser.check_connection(); return self.browser_status

    def open_gpt(self) -> dict[str, Any]:
        """Attach the one user-selected OBU ChatGPT tab and expose bind metadata."""
        try:
            attached = self.browser.attach_unique_chatgpt_tab()
            # Runtime-only: do not silently alter config.local.json.
            self.config["chatgpt"]["conversation_url"] = attached["conversation_url"]
            self.browser.chat["conversation_url"] = attached["conversation_url"]
            self.browser_status = {"state": "connected", "label": "已绑定唯一 GPT 会话", "checked_at": now(),
                                   "target": f"{self.browser.chat.get('browser','chrome')}:{self.browser.chat.get('profile','Default')}",
                                   "detail": f"{attached['conversation_title']} · {attached['conversation_id']}", "conversation": attached}
            return {"ok": True, **attached}
        except Exception as exc:
            self.browser_status = {"state": "disconnected", "label": "GPT 会话未绑定", "checked_at": now(),
                                   "target": f"{self.browser.chat.get('browser','chrome')}:{self.browser.chat.get('profile','Default')}", "detail": str(exc)[:500]}
            return {"ok": False, "error": str(exc)[:500]}

    def continue_pr(self, pr_number: int) -> dict[str, Any]:
        """Prepare, but never send, the latest local handoff context for a PR."""
        if not isinstance(pr_number, int) or pr_number < 1:
            return {"ok": False, "error": "invalid PR number"}
        cache = self.branch_cache()
        candidates = [branch for branch in cache.get("branches", []) if branch.get("primary_pr") == pr_number]
        if not candidates:
            return {"ok": False, "error": f"本地缓存中没有 PR #{pr_number} 的交接轨道"}
        branch = max(candidates, key=lambda item: len(item.get("nodes", [])))
        state = str(branch.get("pr_state", "unknown")).lower()
        if state in {"closed", "merged"}:
            return {"ok": False, "closed": True, "error": f"PR #{pr_number} 已{'合并' if state == 'merged' else '关闭'}，不能继续交接"}
        nodes = branch.get("nodes", [])
        latest = nodes[-1] if nodes else {"sha": branch.get("head", ""), "subject": "当前分支 HEAD", "tasks": []}
        task = next((item for item in reversed(latest.get("tasks", [])) if item.get("pr_number") == pr_number), None)
        if task is None:
            task = next((item for item in branch.get("tasks", []) if item.get("pr_number") == pr_number), None)
        branch_name = str(branch.get("ref", "")).removeprefix(f"{self.git.remote}/")
        binding = next((item for item in self.store.list_bindings(self.config.get("repository", ""))
                        if item.get("status") == "active" and item.get("branch") == branch_name and item.get("pr_number") == pr_number), None)
        return {"ok": True, "pr_number": pr_number, "ref": branch.get("ref"), "head": branch.get("head"),
                "pr_state": state, "latest": latest, "task": task, "binding": binding,
                "message": "已准备最新本地交接上下文；请先人工审阅，再在唯一 GPT 会话中继续。"}

    def cache_branch_tasks(self) -> dict[str, Any]:
        """Persist every visible branch's complete local history and task snapshots.

        This reads Git objects only.  Network synchronization remains an explicit
        user action in ``refresh_from_github``.
        """
        branches: list[dict[str, Any]] = []
        pr_states = self.store.setting("github_pr_states") or {}
        # Prefer the remote-tracking ref when a local branch points at the same
        # named GitHub branch; otherwise the Dashboard would show duplicates.
        refs = self.git.refs()
        remote_refs = {ref.removeprefix(f"{self.git.remote}/") for ref in refs if ref.startswith(f"{self.git.remote}/")}
        refs = [ref for ref in refs if ref.startswith(f"{self.git.remote}/") or ref not in remote_refs]
        for ref in refs:
            try:
                head = run(["git", "rev-parse", ref], self.git.repo)
                changed_paths = run(["git", "-c", "core.quotepath=false", "log", "--format=", "--name-only", ref, "--", "coordination"], self.git.repo).splitlines()
            except RuntimeError:
                continue
            # Only show commits introduced by this branch.  Otherwise every
            # stacked branch repeats all of its ancestors in the canvas.
            ancestors: list[tuple[int, str]] = []
            for candidate in refs:
                if candidate == ref:
                    continue
                try:
                    if run(["git", "rev-parse", candidate], self.git.repo) == head:
                        continue
                    run(["git", "merge-base", "--is-ancestor", candidate, ref], self.git.repo)
                    distance = int(run(["git", "rev-list", "--count", f"{candidate}..{ref}"], self.git.repo))
                    ancestors.append((distance, candidate))
                except (RuntimeError, ValueError):
                    continue
            base_ref = min(ancestors)[1] if ancestors else None
            try:
                history_range = f"{base_ref}..{ref}" if base_ref else ref
                raw_log = run(["git", "log", "--reverse", "--format=%H%x1f%s%x1f%B%x1e", history_range], self.git.repo)
            except RuntimeError:
                raw_log = ""
            paths = sorted({path for path in changed_paths if re.fullmatch(r"coordination/PR-([1-9][0-9]*)/任务\.md", path)})
            commits: list[dict[str, Any]] = []
            for record in raw_log.split("\x1e"):
                parts = record.strip().split("\x1f", 2)
                if len(parts) != 3:
                    continue
                sha, subject, body = parts
                origin_match, event_match, cause_match = ORIGIN_RE.search(body), EVENT_RE.search(body), CAUSE_RE.search(body)
                snapshots: list[dict[str, Any]] = []
                for path in paths:
                    try:
                        content = run(["git", "show", f"{sha}:{path}"], self.git.repo)[:30000]
                    except RuntimeError:
                        continue
                    pr_match = re.fullmatch(r"coordination/PR-([1-9][0-9]*)/任务\.md", path)
                    if pr_match:
                        snapshots.append({"pr_number": int(pr_match.group(1)), "path": path, "content": content, **parse_task_markdown(content)})
                commits.append({"sha": sha, "subject": subject, "origin": origin_match.group(1).lower() if origin_match else "other", "event_key": event_match.group(1) if event_match else f"commit:{sha}", "caused_by": cause_match.group(1) if cause_match else None, "tasks": snapshots})
            tasks: list[dict[str, Any]] = []
            for path in paths:
                try:
                    content = run(["git", "show", f"{ref}:{path}"], self.git.repo)
                except RuntimeError:
                    continue
                match = re.fullmatch(r"coordination/PR-([1-9][0-9]*)/任务\.md", path)
                if not match:
                    continue
                content = content[:30000]
                tasks.append({"pr_number": int(match.group(1)), "path": path, "content": content, **parse_task_markdown(content)})
            # A branch's current tree can contain inherited PR directories.
            # Label its lane only from task snapshots introduced by this branch,
            # with an explicit ``prN`` branch name as a fallback.
            introduced_prs = [task["pr_number"] for commit in commits for task in commit["tasks"]]
            name_match = re.search(r"(?:^|[-_/])pr[-_]?([1-9][0-9]*)(?:$|[-_/])", ref, re.IGNORECASE)
            short_ref = ref.removeprefix(f"{self.git.remote}/")
            if name_match:
                primary_pr = int(name_match.group(1))
            elif short_ref in {"main", "master"}:
                primary_pr = None
            elif introduced_prs:
                primary_pr = max(introduced_prs, default=None)
            else:
                primary_pr = None
            cached_state = pr_states.get(str(primary_pr), {}) if primary_pr is not None else {}
            branches.append({"ref": ref, "head": head, "base_ref": base_ref, "primary_pr": primary_pr,
                             "pr_state": cached_state.get("state", "unknown"), "pr_state_source": cached_state.get("source", "not_synced"),
                             "tasks": tasks, "nodes": commits})
        payload = {"cached_at": now(), "branches": branches}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.branch_cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.branch_cache_path)
        return payload

    def branch_cache(self) -> dict[str, Any]:
        try:
            data = json.loads(self.branch_cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("branches"), list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"cached_at": None, "branches": []}

    def _text_field(self, payload: dict[str, Any], key: str, minimum: int = 1, maximum: int = 200) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum) or any(ord(c) < 32 for c in value): raise ValueError(f"invalid {key}")
        return value.strip()

    def create_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            repository = self._text_field(payload, "repository"); branch = self._text_field(payload, "branch", 1, 255); web_id = self._text_field(payload, "web_conversation_id"); title = self._text_field(payload, "web_conversation_title", 0, 200) if payload.get("web_conversation_title") else ""; pr = payload.get("pr_number"); expires_seconds = payload.get("expires_seconds", 900)
            if repository != self.config.get("repository"): raise ValueError("repository 必须匹配当前配置仓库")
            if not isinstance(pr, int) or isinstance(pr, bool) or pr < 1: raise ValueError("invalid pr_number")
            if not isinstance(expires_seconds, int) or not 60 <= expires_seconds <= 3600: raise ValueError("expires_seconds 必须在 60 到 3600 之间")
        except ValueError as exc: return {"ok": False, "status": "invalid", "error": str(exc)}
        token = secrets.token_urlsafe(32); expires = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + expires_seconds, timezone.utc).isoformat(timespec="seconds")
        return self.store.create_binding({"binding_id": f"bind-{secrets.token_urlsafe(12)}", "repository": repository, "branch": branch, "pr_number": pr, "web_conversation_id": web_id, "web_conversation_title": title, "token": token, "token_hash": hashlib.sha256(token.encode()).hexdigest(), "expires_at": expires})

    def claim_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            binding_id = self._text_field(payload, "binding_id", 8, 128); token = self._text_field(payload, "token", 20, 200); data = {k: self._text_field(payload, k) for k in ("route_id", "local_agent_id", "local_conversation_id", "repository", "branch")}; data["pr_number"] = payload.get("pr_number")
            if not isinstance(data["pr_number"], int) or data["pr_number"] < 1: raise ValueError("invalid pr_number")
            data["local_conversation_title"] = self._text_field(payload, "local_conversation_title", 0, 200) if payload.get("local_conversation_title") else ""
        except ValueError as exc: return {"ok": False, "status": "invalid", "error": str(exc)}
        return self.store.claim_binding(binding_id, hashlib.sha256(token.encode()).hexdigest(), data)

    def confirm_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            binding_id = self._text_field(payload, "binding_id", 8, 128); token = self._text_field(payload, "confirm_token", 20, 200); data = {k: self._text_field(payload, k) for k in ("route_id", "local_agent_id", "local_conversation_id", "repository", "branch")}; data["pr_number"] = payload.get("pr_number")
            if not isinstance(data["pr_number"], int) or data["pr_number"] < 1: raise ValueError("invalid pr_number")
        except ValueError as exc: return {"ok": False, "status": "invalid", "error": str(exc)}
        return self.store.confirm_binding(binding_id, hashlib.sha256(token.encode()).hexdigest(), data)

    def revoke_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        try: binding_id = self._text_field(payload, "binding_id", 8, 128); token = self._text_field(payload, "token", 20, 200)
        except ValueError as exc: return {"ok": False, "status": "invalid", "error": str(exc)}
        return self.store.revoke_binding(binding_id, hashlib.sha256(token.encode()).hexdigest())

    def refresh_binding_name(self, binding_id: str) -> dict[str, Any]:
        if not BINDING_ID_RE.fullmatch(binding_id): return {"ok": False, "error": "invalid binding_id"}
        if not self.store.binding(binding_id): return {"ok": False, "error": "binding 不存在"}
        return {"ok": False, "error": "云端元数据不可访问；请由 ChatGPT 端显式回写对话名称"}

    def active_binding_for_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        branch = str(event.get("ref") or "").removeprefix(f"{self.git.remote}/")
        matches = [b for b in self.store.list_bindings(self.config.get("repository", "")) if b.get("status") == "active" and b.get("branch") == branch and b.get("pr_number") == event.get("pr_number")]
        return matches[0] if len(matches) == 1 else None

    def task_snapshot(self, pr_number: int | None, sha: str) -> dict[str, Any]:
        if pr_number is None: return {"ok": False, "available": False, "error": "该事件未关联真实 PR，历史任务快照不可用"}
        if not isinstance(pr_number, int) or pr_number < 1: return {"ok": False, "available": False, "error": "invalid PR number"}
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha): return {"ok": False, "available": False, "error": "invalid commit SHA"}
        path = f"coordination/PR-{pr_number}/任务.md"
        try: run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], self.git.repo)
        except RuntimeError as exc: return {"ok": False, "available": False, "pr_number": pr_number, "commit_sha": sha, "path": path, "error": f"本地 Git 不包含该事件提交：{exc}"}
        try: content = run(["git", "show", f"{sha}:{path}"], self.git.repo)
        except RuntimeError: return {"ok": False, "available": False, "pr_number": pr_number, "commit_sha": sha, "path": path, "error": "历史任务快照不可用：该 commit 中不存在对应 coordination/PR-N/任务.md"}
        truncated = len(content) > 30000; content = content[:30000]
        return {"ok": True, "available": True, "pr_number": pr_number, "commit_sha": sha, "path": path, "content": content, "truncated": truncated, **parse_task_markdown(content)}

    def current_task(self, pr_number: int) -> dict[str, Any]:
        if not isinstance(pr_number, int) or pr_number < 1: return {"ok": False, "error": "invalid PR number"}
        path = self.git.repo / "coordination" / f"PR-{pr_number}" / "任务.md"
        try: content = path.read_text(encoding="utf-8")
        except FileNotFoundError: return {"ok": False, "error": "当前 HEAD 不存在该 PR 的任务.md（可能已合并并清理）"}
        content = content[:30000]; return {"ok": True, "path": f"coordination/PR-{pr_number}/任务.md", "content": content, **parse_task_markdown(content)}

    def wake_prompt(self, commit: Commit, pr: int | None, event_id: str, ref: str, binding: dict[str, Any] | None) -> str:
        follow = ("本事件尚未关联 PR。仅在确认分支只包含一个可关闭目标后创建真实 PR；获得真实编号前不得创建 coordination/PR-N/。" if pr is None else "请重新读取该 PR 当前 HEAD、任务.md 与 agent汇报.md 后处理。")
        binding_line = (f"Binding-ID: {binding['binding_id']}\nRoute-ID: {binding['route_id']}\nWeb-Conversation-ID: {binding['web_conversation_id']}\n" if binding else "Binding: no active binding\n")
        return ("GitHub 协作事件已到达。\n\n" f"Repository: {self.config['repository']}\nBranch: {ref or 'unknown'}\nPR: #{pr if pr else 'unassigned'}\n" f"Origin: agent\nHead: {commit.sha}\nEvent-ID: {event_id}\n{binding_line}\n{follow}\n" "本消息仅用于唤醒；不要依据旧聊天猜测项目事实。")

    def dispatch_agent(self, commit: Commit, pr: int | None, binding: dict[str, Any] | None) -> str:
        command = self.config["agent"].get("command", [])
        if not command: raise RuntimeError("agent.command is empty; configure a local command before enabling this route")
        route = (f" Binding-ID={binding['binding_id']} Route-ID={binding['route_id']} Local-Agent-ID={binding['local_agent_id']} Local-Conversation-ID={binding['local_conversation_id']}." if binding else " No active binding metadata was supplied; do not broadcast this event.")
        prompt = (f"GitHub coordination event {commit.event_id}: ChatGPT updated PR #{pr or 'unknown'}. " "Read the configured local handoff repository, current README/task/chatgpt解惑.md and continue only the current task." + route)
        subprocess.Popen([*command, prompt], cwd=self.git.repo, start_new_session=True); return "local agent process started"

    def handle(self, commit: Commit, ref: str) -> None:
        if commit.origin not in {"agent", "chatgpt"}: return
        pr = self.git.pr_number(commit.sha); event = {"event_key": commit.event_id, "sha": commit.sha, "ref": ref, "pr_number": pr, "origin": commit.origin, "caused_by": commit.caused_by, "subject": commit.subject, "observed_at": now(), "status": "detected", "approval_state": "detected", "delivery_state": "not_started", "delivery_detail": ""}
        if not self.store.add_event(event): return
        if not self.store.setting("enabled"):
            self.store.set_event_states(commit.event_id, approval_state="not_applicable", delivery_state="skipped", delivery_detail="总开关暂停", legacy_status="skipped: paused"); return
        route = "agent_to_chatgpt" if commit.origin == "agent" else "chatgpt_to_agent"
        if not self.store.setting(route):
            self.store.set_event_states(commit.event_id, approval_state="not_applicable", delivery_state="skipped", delivery_detail=f"{route} disabled", legacy_status=f"skipped: {route} disabled"); return
        if self.store.setting("approval_required"):
            self.store.set_event_states(commit.event_id, approval_state="awaiting", delivery_state="not_started", delivery_detail="等待用户在本地 Dashboard 审批", legacy_status="awaiting approval"); return
        self.store.set_event_states(commit.event_id, approval_state="auto_approved"); self.dispatch_event(commit.event_id)

    def approve_event(self, event_key: str) -> dict[str, Any]:
        event = self.store.event(event_key)
        if not event: return {"ok": False, "error": "event not found"}
        if event.get("approval_state") not in {"awaiting", "approved", "auto_approved", None} and event.get("delivery_state") != "needs_human": return {"ok": False, "error": "event cannot be approved or retried"}
        if event.get("approval_state") != "auto_approved": self.store.set_event_states(event_key, approval_state="approved")
        self.dispatch_event(event_key); return {"ok": True}

    def dispatch_event(self, event_key: str, allow_fill_only_resubmit: bool = False) -> None:
        event = self.store.event(event_key)
        if not event: raise RuntimeError("event not found")
        if event.get("delivery_state") in {"submitted", "agent_started"}: return
        if event.get("delivery_state") == "filled" and not allow_fill_only_resubmit: return
        try:
            binding = self.active_binding_for_event(event)
            if self.config.get("binding", {}).get("require_active", False) and not binding: raise RuntimeError("no active binding matches repository/branch/PR")
            if event["origin"] == "agent":
                if binding:
                    url = str(self.config.get("chatgpt", {}).get("conversation_url", "")); m = re.search(r"/c/([^/?#]+)", url)
                    if not m or m.group(1) != binding["web_conversation_id"]: raise RuntimeError("configured ChatGPT conversation does not match active binding")
                prompt = self.wake_prompt(Commit(event["sha"], "", event["subject"]), event["pr_number"], event["event_key"], event["ref"], binding); detail = self.browser.dispatch(prompt, self.store.setting("auto_submit")); state = "submitted" if detail == "submitted" else "filled"
            else:
                detail = self.dispatch_agent(Commit(event["sha"], "", event["subject"]), event["pr_number"], binding); state = "agent_started"
            self.store.set_event_states(event_key, delivery_state=state, delivery_detail=detail, legacy_status="dispatched")
        except Exception as exc:
            self.store.set_event_states(event_key, delivery_state="needs_human", delivery_detail=str(exc), legacy_status="needs human")

    def set_auto_mode(self, enabled: bool) -> dict[str, Any]:
        self.store.set_settings({"approval_required": not enabled, "auto_submit": enabled}); dispatched = self.drain_auto_mode() if enabled else []; return {"ok": True, "auto_mode": enabled, "drained_event_keys": dispatched}

    def drain_auto_mode(self) -> list[str]:
        if not self.auto_mode(): return []
        keys: list[str] = []
        for event in self.store.pending_events():
            self.store.set_event_states(event["event_key"], approval_state="auto_approved"); self.dispatch_event(event["event_key"]); keys.append(event["event_key"])
        for event in self.store.fill_only_events(): self.dispatch_event(event["event_key"], allow_fill_only_resubmit=True); keys.append(event["event_key"])
        return keys

    def scan_local(self) -> dict[str, Any]:
        try:
            observed = []
            for ref in self.git.refs():
                head, commits = self.git.poll(ref, self.store.cursor(ref))
                for commit in commits: self.handle(commit, ref)
                self.store.set_cursor(ref, head); observed.append({"ref": ref, "commits": len(commits), "head": head})
            self.cache_branch_tasks()
            self.last_local_error = ""; return {"ok": True, "source": "local_git", "refs": observed, "commits": sum(item["commits"] for item in observed)}
        except Exception as exc:
            self.last_local_error = str(exc); return {"ok": False, "source": "local_git", "error": self.last_local_error}

    def _refresh_pr_states(self) -> str:
        if not shutil.which("gh"): return "gh CLI 不可用；GitHub PR 状态保持未同步/旧缓存，不做推断"
        try:
            raw = run(["gh", "pr", "list", "--repo", self.config["repository"], "--state", "all", "--limit", "100", "--json", "number,state,mergedAt,headRefName"], self.git.repo, timeout=30); rows = json.loads(raw); stamp = now(); states: dict[str, Any] = {}
            for row in rows:
                state = "merged" if row.get("mergedAt") else str(row.get("state", "unknown")).lower(); states[str(row["number"])] = {"state": state, "source": "explicit_github_refresh", "updated_at": stamp, "head_ref": row.get("headRefName", "")}
            self.store.set_settings({"github_pr_states": states, "github_pr_states_updated_at": stamp, "github_pr_state_error": ""}); return ""
        except Exception as exc:
            message = str(exc); self.store.set_setting("github_pr_state_error", message); return message

    def refresh_from_github(self) -> dict[str, Any]:
        try: run(["git", "fetch", self.git.remote, "--prune", "--quiet"], self.git.repo, timeout=60)
        except Exception as exc:
            self.last_refresh_error = str(exc); self.last_refresh_at = now(); return {"ok": False, "source": "github_refresh", "error": self.last_refresh_error, "local_state_preserved": True}
        pr_warning = self._refresh_pr_states(); self.last_refresh_error = ""; self.last_refresh_at = now(); scanned = self.scan_local(); cache = self.cache_branch_tasks(); return {"ok": scanned.get("ok", False), "source": "github_refresh", "fetched": True, "pr_state_warning": pr_warning, "scan": scanned, "cached_branches": len(cache["branches"]), "refreshed_at": self.last_refresh_at}

    def poll_once(self) -> dict[str, Any]: return self.scan_local()

    def status(self) -> dict[str, Any]:
        cache = self.branch_cache()
        data = self.store.snapshot(); data.update({"repository": self.repository_status(), "browser": self.browser_status, "auto_mode": self.auto_mode(), "last_local_error": self.last_local_error, "last_refresh_error": self.last_refresh_error, "app_approvals": pending_approval_requests(), "branch_cache": {"cached_at": cache.get("cached_at"), "branch_count": len(cache.get("branches", []))}}); return data


def handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, data: Any, content_type: str = "application/json; charset=utf-8") -> None:
            body = (json.dumps(data, ensure_ascii=False).encode("utf-8") if content_type.startswith("application/json") else str(data).encode("utf-8")); self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
        def read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0")); data = json.loads(self.rfile.read(length) or b"{}"); return data if isinstance(data, dict) else None
            except (ValueError, json.JSONDecodeError): return None
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/pair"}:
                try: return self.reply(200, DASHBOARD_FILE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                except OSError as exc: return self.reply(500, {"error": f"dashboard.html unavailable: {exc}"})
            if parsed.path == "/api/status": return self.reply(200, service.status())
            if parsed.path == "/api/approvals": return self.reply(200, {"requests": pending_approval_requests()})
            if parsed.path == "/api/bindings": return self.reply(200, {"ok": True, "bindings": service.store.list_bindings(service.config.get("repository", ""))})
            if parsed.path == "/api/cache/branches": return self.reply(200, {"ok": True, **service.branch_cache()})
            if parsed.path == "/api/task/snapshot":
                q = parse_qs(parsed.query); raw_pr = q.get("pr", [""])[0]; sha = q.get("sha", [""])[0]; pr = int(raw_pr) if re.fullmatch(r"[1-9][0-9]*", raw_pr) else None; return self.reply(200, service.task_snapshot(pr, sha))
            if parsed.path == "/api/task/current":
                raw_pr = parse_qs(parsed.query).get("pr", [""])[0]
                if not re.fullmatch(r"[1-9][0-9]*", raw_pr): return self.reply(400, {"ok": False, "error": "invalid PR number"})
                return self.reply(200, service.current_task(int(raw_pr)))
            return self.reply(404, {"error": "not found"})
        def do_POST(self) -> None:
            if self.path in {"/api/scan-local", "/api/poll"}: return self.reply(200, service.scan_local())
            if self.path == "/api/refresh-github": return self.reply(200, service.refresh_from_github())
            if self.path == "/api/browser/check": return self.reply(200, service.check_browser())
            if self.path == "/api/browser/open-gpt":
                result = service.open_gpt(); return self.reply(200 if result.get("ok") else 409, result)
            if self.path.startswith("/api/pr/") and self.path.endswith("/continue"):
                raw_pr = self.path[len("/api/pr/"):-len("/continue")].rstrip("/")
                if not re.fullmatch(r"[1-9][0-9]*", raw_pr): return self.reply(400, {"ok": False, "error": "invalid PR number"})
                result = service.continue_pr(int(raw_pr)); return self.reply(200 if result.get("ok") else 409, result)
            if self.path == "/api/mode":
                data = self.read_json()
                if not data or not isinstance(data.get("auto"), bool): return self.reply(400, {"error": "invalid auto mode"})
                return self.reply(200, service.set_auto_mode(data["auto"]))
            if self.path.startswith("/api/approvals/"):
                data = self.read_json() or {}; request_id = unquote(self.path.removeprefix("/api/approvals/"))
                if not resolve_approval_request(request_id, data.get("decision", "")): return self.reply(409, {"error": "approval request is missing or already resolved"})
                return self.reply(200, {"ok": True})
            if self.path.startswith("/api/events/") and self.path.endswith("/approve"):
                event_key = unquote(self.path[len("/api/events/"):-len("/approve")]).rstrip("/"); result = service.approve_event(event_key); return self.reply(200 if result.get("ok") else 409, result)
            if self.path == "/api/settings":
                data = self.read_json()
                if not data or data.get("key") not in {"enabled", "agent_to_chatgpt", "chatgpt_to_agent"} or not isinstance(data.get("value"), bool): return self.reply(400, {"error": "invalid setting"})
                service.store.set_setting(data["key"], data["value"]); return self.reply(200, {"ok": True})
            if self.path.startswith("/api/bindings/"):
                data = self.read_json()
                if data is None: return self.reply(400, {"ok": False, "error": "JSON object required"})
                suffix = self.path.removeprefix("/api/bindings/")
                if suffix == "invite": result = service.create_binding(data)
                elif suffix == "claim": result = service.claim_binding(data)
                elif suffix == "confirm": result = service.confirm_binding(data)
                elif suffix == "revoke": result = service.revoke_binding(data)
                elif suffix.startswith("refresh-name/"): result = service.refresh_binding_name(unquote(suffix.removeprefix("refresh-name/")))
                else: return self.reply(404, {"ok": False, "error": "unknown binding action"})
                return self.reply(200 if result.get("ok") else 409, result)
            return self.reply(404, {"error": "not found"})
        def log_message(self, *_: Any) -> None: pass
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG); parser.add_argument("--db", type=Path, default=DEFAULT_DB); parser.add_argument("--port", type=int, default=8765); parser.add_argument("--once", action="store_true", help="scan local Git once; never fetch"); parser.add_argument("--refresh-once", action="store_true", help="explicitly fetch GitHub once, then scan"); args = parser.parse_args(); config = load_config(args.config); store = Store(args.db); service = Service(config, store)
    if args.refresh_once: print(json.dumps(service.refresh_from_github(), ensure_ascii=False)); return
    if args.once: print(json.dumps(service.scan_local(), ensure_ascii=False)); return
    def loop() -> None:
        while True:
            service.scan_local(); service.drain_auto_mode(); time.sleep(max(3, int(config.get("poll_interval_seconds", 15))))
    threading.Thread(target=loop, daemon=True).start(); server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(service)); print(f"Dashboard: http://127.0.0.1:{args.port}"); server.serve_forever()


if __name__ == "__main__": main()
