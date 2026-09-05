#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local, deterministic GitHub <-> ChatGPT Web handoff trigger.

The browser adapter is deliberately a fixed Open Browser Use CLI workflow, not
an LLM agent.  It never reads cookies, passwords, or unrelated tabs.  Sending
is opt-in and disabled by default; the dashboard controls both directions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shlex
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
APPROVAL_DIR = ROOT / "approval-requests"
ORIGIN_RE = re.compile(r"^Coordination-Origin:\s*(agent|chatgpt)\s*$", re.M | re.I)
EVENT_RE = re.compile(r"^Coordination-Event-Id:\s*(\S+)\s*$", re.M | re.I)
CAUSE_RE = re.compile(r"^Coordination-Caused-By:\s*(\S+)\s*$", re.M | re.I)
PR_PATH_RE = re.compile(r"^coordination/PR-(\d+)/")
BINDING_STATES = {"pending", "claimed", "active", "expired", "revoked", "conflict"}
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
        example = ROOT / "config.example.json"
        raise RuntimeError(f"missing {path}; copy {example.name} and configure it")
    config = json.loads(path.read_text())
    for key in ("handoff_repo", "remote", "repository", "chatgpt", "agent"):
        if key not in config:
            raise RuntimeError(f"config missing {key}")
    if "watch_branches" not in config:
        config["watch_branches"] = "all"
    return config


def pending_approval_requests() -> list[dict[str, Any]]:
    """Read app-server approval requests published by local Agent processes."""
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
    """Resolve one request; the app-server bridge consumes the decision."""
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
                ref text not null default '', pr_number integer, origin text, caused_by text, subject text not null,
                observed_at text not null, dispatched_at text, status text not null,
                detail text not null default ''
              );
              create table if not exists bindings (
                binding_id text primary key, route_id text, repository text not null, branch text not null,
                pr_number integer not null, web_conversation_id text not null, web_conversation_title text not null default '',
                local_agent_id text, local_conversation_id text, local_conversation_title text not null default '',
                status text not null, token_hash text not null, claim_token_hash text,
                created_at text not null, expires_at text not null, updated_at text not null,
                claimed_at text, confirmed_at text, revoked_at text
              );
              create index if not exists idx_bindings_target on bindings(repository, branch, pr_number, status);
            """)
            try:
                self.db.execute("alter table events add column ref text not null default ''")
            except sqlite3.OperationalError:
                pass
            defaults = {"enabled": True, "agent_to_chatgpt": True,
                        "chatgpt_to_agent": True, "auto_submit": False,
                        "approval_required": True}
            for key, value in defaults.items():
                self.db.execute("insert or ignore into settings values (?, ?)", (key, json.dumps(value)))
            self.db.commit()

    def setting(self, key: str) -> Any:
        with self.lock:
            row = self.db.execute("select value from settings where key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        with self.lock:
            self.db.execute("insert into settings values (?, ?) on conflict(key) do update set value=excluded.value",
                            (key, json.dumps(value)))
            self.db.commit()

    def set_settings(self, values: dict[str, Any]) -> None:
        """Update related settings in one SQLite transaction."""
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
            self.db.execute("insert into cursors values (?, ?) on conflict(key) do update set value=excluded.value", (f"git_head:{ref}", sha))
            self.db.commit()

    def add_event(self, event: dict[str, Any]) -> bool:
        with self.lock:
            try:
                self.db.execute("""insert into events(event_key,sha,ref,pr_number,origin,caused_by,subject,observed_at,status)
                  values(:event_key,:sha,:ref,:pr_number,:origin,:caused_by,:subject,:observed_at,:status)""", event)
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def finish(self, event_key: str, status: str, detail: str = "") -> None:
        with self.lock:
            self.db.execute("update events set status=?, detail=?, dispatched_at=? where event_key=?",
                            (status, detail[:1000], now(), event_key))
            self.db.commit()

    def event(self, event_key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("select * from events where event_key=?", (event_key,)).fetchone()
        return dict(row) if row else None

    def pending_events(self) -> list[dict[str, Any]]:
        """Return events waiting for the explicit approval gate.

        ``needs human`` is deliberately excluded: it records a failed or
        incomplete delivery and must never be retried merely by switching to
        unattended mode.
        """
        with self.lock:
            rows = self.db.execute(
                "select * from events where status='awaiting approval' order by id"
            ).fetchall()
        return [dict(row) for row in rows]

    def fill_only_events(self) -> list[dict[str, Any]]:
        """Return verified drafts that are safe to submit once in auto mode."""
        with self.lock:
            rows = self.db.execute(
                "select * from events where status='dispatched' and detail=? order by id",
                ("filled; verified; waiting for user submit",),
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            rows = self.db.execute("select * from events order by id desc limit 200").fetchall()
            settings = {r["key"]: json.loads(r["value"]) for r in self.db.execute("select * from settings")}
        events = [dict(row) for row in rows]
        prs: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            prs.setdefault(str(event["pr_number"] or "unassigned"), []).append(event)
        return {"settings": settings, "events": events, "prs": prs, "updated_at": now()}

    @staticmethod
    def _binding_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for secret in ("token_hash", "claim_token_hash"):
            item.pop(secret, None)
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
                query += " where repository=?"
                params = (repository,)
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
                "insert into bindings(binding_id,repository,branch,pr_number,web_conversation_id,web_conversation_title,status,token_hash,created_at,expires_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?)",
                (data["binding_id"], data["repository"], data["branch"], data["pr_number"], data["web_conversation_id"],
                 data.get("web_conversation_title", ""), "pending", data["token_hash"], timestamp, data["expires_at"], timestamp),
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
            if row["repository"] != data["repository"] or row["branch"] != data["branch"] or row["pr_number"] != data["pr_number"]:
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
                "update bindings set status='claimed',route_id=?,local_agent_id=?,local_conversation_id=?,local_conversation_title=?,claim_token_hash=?,claimed_at=?,updated_at=? where binding_id=?",
                (data["route_id"], data["local_agent_id"], data["local_conversation_id"], data.get("local_conversation_title", ""),
                 hashlib.sha256(claim_token.encode()).hexdigest(), timestamp, timestamp, binding_id),
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
            if row["repository"] != data["repository"] or row["branch"] != data["branch"] or row["pr_number"] != data["pr_number"]:
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "确认目标 repository/branch/PR 不匹配"}
            if row["route_id"] != data["route_id"] or row["local_agent_id"] != data["local_agent_id"] or row["local_conversation_id"] != data["local_conversation_id"]:
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "确认身份与 claim 不匹配"}
            active = self.db.execute(
                "select binding_id from bindings where repository=? and branch=? and pr_number=? and status='active' and binding_id<>?",
                (row["repository"], row["branch"], row["pr_number"], binding_id),
            ).fetchone()
            if active:
                self.db.execute("update bindings set status='conflict',updated_at=? where binding_id=?", (timestamp, binding_id))
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "确认时发现其它 active binding"}
            self.db.execute("update bindings set status='active',claim_token_hash=NULL,confirmed_at=?,updated_at=? where binding_id=?", (timestamp, timestamp, binding_id))
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
            valid = secrets.compare_digest(row["token_hash"], token_hash) or bool(row["claim_token_hash"] and secrets.compare_digest(row["claim_token_hash"], token_hash))
            if not valid:
                self.db.commit(); return {"ok": False, "status": "conflict", "error": "撤销 token 无效"}
            if row["status"] in {"revoked", "expired"}:
                self.db.commit(); return {"ok": False, "status": row["status"], "error": f"binding 当前状态为 {row['status']}"}
            self.db.execute("update bindings set status='revoked',revoked_at=?,updated_at=? where binding_id=?", (timestamp, timestamp, binding_id))
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
    def __init__(self, config: dict[str, Any]):
        self.repo = (ROOT / config["handoff_repo"]).resolve()
        self.remote = config["remote"]
        self.watch_branches = config.get("watch_branches", "all")

    def refs(self) -> list[str]:
        names = run(["git", "for-each-ref", "--format=%(refname:short)", f"refs/remotes/{self.remote}"], self.repo).splitlines()
        refs = [name for name in names if name not in {self.remote, f"{self.remote}/HEAD"}]
        if self.watch_branches == "all":
            return refs
        wanted = set(self.watch_branches)
        return [name for name in refs if name.removeprefix(f"{self.remote}/") in wanted]

    def poll(self, ref: str, cursor: str | None) -> tuple[str, list[Commit]]:
        head = run(["git", "rev-parse", ref], self.repo)
        if cursor is None:
            return head, []  # First start establishes a safe baseline.
        if cursor == head:
            return head, []
        raw = run(["git", "log", "--reverse", "--format=%H%x1f%s%x1f%B%x1e", f"{cursor}..{head}"], self.repo)
        commits = []
        for record in raw.split("\x1e"):
            if not record.strip():
                continue
            sha, subject, body = record.split("\x1f", 2)
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
    """Fixed CLI adapter. No LLM is involved in this dispatch path."""
    def __init__(self, config: dict[str, Any]):
        self.chat = config["chatgpt"]
        # Keep one broker session for the lifetime of the local trigger. Ending
        # the OBU turn after every event can leave active.json pointing at a
        # broker that has already exited, making the next event look offline.
        session_seed = "|".join((self.chat.get(key, "") for key in ("browser", "profile", "conversation_url")))
        session_hash = hashlib.sha256(session_seed.encode()).hexdigest()[:12]
        self.session_id = f"obu-trigger-{session_hash}"
        self._health_lock = threading.Lock()

    def _common(self) -> list[str]:
        return ["--session-id", self.session_id, "--browser", self.chat.get("browser", "chrome"),
                "--profile", self.chat.get("profile", "Default")]

    @staticmethod
    def _clear_stale_registry() -> bool:
        """Remove only an active registry whose socket path is gone."""
        registry = Path("/tmp/open-browser-use/active.json")
        try:
            data = json.loads(registry.read_text())
            socket_path = Path(str(data.get("socketPath", "")))
            if socket_path and not socket_path.exists():
                registry.unlink()
                return True
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return False
        return False

    def _ping(self, common: list[str]) -> None:
        try:
            run(["open-browser-use", "ping", *common], timeout=15)
        except Exception as first_error:
            # v0.1.36+ can repair a missing registry by scanning live sockets,
            # but a registry pointing at a deleted socket must be removed first.
            if not self._clear_stale_registry():
                raise
            try:
                run(["open-browser-use", "ping", *common], timeout=15)
            except Exception:
                raise first_error

    def check_connection(self) -> dict[str, Any]:
        """Return a UI-safe health snapshot without inspecting browser data."""
        with self._health_lock:
            checked_at = now()
            try:
                profiles_raw = run(["open-browser-use", "profiles", "--connected", "--json"], timeout=10)
                profiles = json.loads(profiles_raw)
                browser = self.chat.get("browser", "chrome").lower()
                profile = self.chat.get("profile", "Default").lower()
                matching = [item for item in profiles if item.get("browser", "").lower() == browser and
                            (item.get("directory", "").lower() == profile or item.get("displayName", "").lower() == profile)]
                if not matching:
                    raise RuntimeError(f"未找到已连接的 Chrome 配置：{browser}/{self.chat.get('profile', 'Default')}")
                self._ping(self._common())
                return {"state": "connected", "label": "已连接", "checked_at": checked_at,
                        "target": matching[0].get("target", f"{browser}:{profile}"), "detail": "OBU ping 成功"}
            except Exception as exc:
                return {"state": "disconnected", "label": "无法连接", "checked_at": checked_at,
                        "target": f"{self.chat.get('browser', 'chrome')}:{self.chat.get('profile', 'Default')}",
                        "detail": str(exc)[:500]}

    @staticmethod
    def _result(raw: str, operation: str) -> Any:
        """Extract a successful Open Browser Use JSON-RPC result."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{operation} returned invalid JSON: {raw}") from exc
        if payload.get("error"):
            raise RuntimeError(f"{operation} failed: {payload['error'].get('message', payload['error'])}")
        return payload.get("result")

    def _cdp(self, common: list[str], tab_id: int, method: str, params: dict[str, Any]) -> Any:
        raw = run(["open-browser-use", "cdp", *common, "--tab-id", str(tab_id), "--method", method,
                   "--params", json.dumps(params)], timeout=20)
        return self._result(raw, f"CDP {method}")

    def _rpc(self, command: list[str], operation: str, timeout: int = 15) -> Any:
        return self._result(run(command, timeout=timeout), operation)

    def _evaluate(self, common: list[str], tab_id: int, expression: str) -> Any:
        result = self._cdp(common, tab_id, "Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        evaluated = result.get("result", {}) if isinstance(result, dict) else {}
        if "exceptionDetails" in result:
            raise RuntimeError(f"ChatGPT composer check failed: {result['exceptionDetails'].get('text', 'JavaScript exception')}")
        return evaluated.get("value")

    def dispatch(self, message: str, submit: bool) -> str:
        url = self.chat.get("conversation_url", "")
        if "REPLACE_" in url or not url.startswith("https://chatgpt.com/"):
            raise RuntimeError("configure chatgpt.conversation_url before enabling agent -> ChatGPT")
        common = self._common()
        # This tests that the explicitly configured browser/profile is connected.
        self._ping(common)
        tabs_result = self._rpc(
            ["open-browser-use", "call", *common, "--method", "getUserTabs", "--params", "{}"],
            "getUserTabs",
        )
        tabs = tabs_result.get("tabs", []) if isinstance(tabs_result, dict) else tabs_result
        if not isinstance(tabs, list):
            raise RuntimeError("getUserTabs returned an invalid tab list")
        matching = [tab for tab in tabs if tab.get("url") == url]
        if matching:
            tab_id = matching[-1]["id"]
            try:
                self._rpc(["open-browser-use", "claim-tab", *common, "--tab-id", str(tab_id)], "claim ChatGPT tab")
            except RuntimeError as exc:
                # A daemon restart reuses this deterministic session id. OBU
                # reports the tab as already owned by that same session; this
                # is safe to continue with and avoids an orphaned handoff.
                if f"already part of browser session {self.session_id}" not in str(exc):
                    raise
        else:
            opened = self._rpc(["open-browser-use", "open-tab", *common, "--url", url], "open ChatGPT tab")
            candidate = opened.get("tabId") if isinstance(opened, dict) else None
            if candidate is None and isinstance(opened, dict):
                candidate = opened.get("tab", {}).get("id")
            if not isinstance(candidate, int):
                raise RuntimeError(f"could not read opened tab id: {opened}")
            tab_id = candidate
        # ChatGPT uses a ProseMirror editor. Directly assigning textContent makes
        # a visually plausible DOM change but does not update the app's draft.
        # Focus the exact editor and use CDP's native text input instead.
        editor = '#prompt-textarea[contenteditable="true"]'
        handoff_ready = False
        try:
            before = self._evaluate(common, tab_id, """(() => {
              const e=document.querySelector(%s); if (!e) throw new Error('ChatGPT composer not found');
              const fallback=document.querySelector('textarea[placeholder="问问 ChatGPT"], textarea.wcDTda_fallbackTextarea');
              const text=(e.innerText||e.textContent||fallback?.value||'').trim(); e.focus();
              return {draftLength:text.length, draftText:text}; })()""" % json.dumps(editor))
            if not isinstance(before, dict):
                raise RuntimeError("could not read ChatGPT composer state")
            # ProseMirror's innerText includes extra blank lines between <p>
            # nodes; normalize only layout whitespace before comparing content.
            normalize = lambda value: re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", value.replace("\r", ""))).strip()
            existing_draft = normalize(str(before.get("draftText", "")))
            event_match = re.search(r"(?:^|\n)Event-ID:\s*(\S+)", message)
            same_event = bool(event_match and event_match.group(1) in existing_draft)
            if existing_draft == normalize(message) or same_event:
                handoff_ready = not submit
                if submit:
                    send = """(() => { const b=document.querySelector('[data-testid=\"send-button\"]');
                      if (!b || b.disabled) throw new Error('send button unavailable'); b.click(); return 'submitted'; })()"""
                    self._evaluate(common, tab_id, send)
                    return "submitted"
                return "filled; verified; waiting for user submit"
            if before.get("draftLength", 0):
                raise RuntimeError("ChatGPT composer already contains a draft; refusing to overwrite it")
            inserted = self._evaluate(common, tab_id, """(() => {
              const e=document.querySelector(%s); e.focus();
              return {inserted:document.execCommand('insertText', false, %s)}; })()""" % (json.dumps(editor), json.dumps(message)))
            if not isinstance(inserted, dict) or not inserted.get("inserted"):
                raise RuntimeError("browser refused to insert the ChatGPT handoff draft")
            verified = self._evaluate(common, tab_id, """(async () => {
              const normalize=value => value.replace(/\r/g,'').replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
              const expected=normalize(%s); const deadline=Date.now()+2500;
              while (Date.now() < deadline) {
                const e=document.querySelector(%s);
                const fallback=document.querySelector('textarea[placeholder="问问 ChatGPT"], textarea.wcDTda_fallbackTextarea');
                const actuals=[e?.innerText,e?.textContent,fallback?.value]
                  .filter(value => typeof value === 'string').map(normalize);
                const actual=actuals.find(value => value === expected) || actuals.find(Boolean) || '';
                if (actual === expected) return {length:actual.length, matches:true};
                await new Promise(resolve => setTimeout(resolve, 100));
              }
              const e=document.querySelector(%s);
              const fallback=document.querySelector('textarea[placeholder="问问 ChatGPT"], textarea.wcDTda_fallbackTextarea');
              const actuals=[e?.innerText,e?.textContent,fallback?.value]
                .filter(value => typeof value === 'string').map(normalize);
              return {length:Math.max(0,...actuals.map(value => value.length)), matches:false};
            })()""" % (json.dumps(message.strip()), json.dumps(editor), json.dumps(editor)))
            if not isinstance(verified, dict) or not verified.get("matches"):
                raise RuntimeError("ChatGPT composer did not retain the injected handoff; no message was sent")
            handoff_ready = not submit
            if submit:
                send = """(() => { const b=document.querySelector('[data-testid=\"send-button\"]');
                  if (!b || b.disabled) throw new Error('send button unavailable'); b.click(); return 'submitted'; })()"""
                self._evaluate(common, tab_id, send)
                return "submitted"
            return "filled; verified; waiting for user submit"
        finally:
            # A verified fill awaits the user's Send click, so retain handoff
            # ownership.  Any failure before that point must be reclaimable.
            status = "handoff" if handoff_ready else "deliverable"
            run(["open-browser-use", "finalize-tabs", *common,
                 "--keep", json.dumps([{"tabId": tab_id, "status": status}])], timeout=15)
            # Keep the broker session alive for subsequent events. The trigger
            # is a long-running local service, not a one-shot browser turn.


def parse_task_markdown(content: str) -> dict[str, Any]:
    """Parse only explicit Markdown task checkboxes; status legend text is ignored."""
    sections: list[dict[str, Any]] = []
    other_lines: list[str] = []
    current: dict[str, Any] | None = None
    in_code = False
    state_names = {"x": "done", "~": "in-progress", "?": "waiting", "!": "blocked", "-": "superseded", " ": "todo"}
    for line in content.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            other_lines.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading and not in_code:
            current = {"title": heading.group(2), "level": len(heading.group(1)), "items": []}
            sections.append(current)
            other_lines.append(line)
            continue
        task = re.match(r"^\s*-\s+\[([ x~?!-])\]\s+(.+?)\s*$", line, re.I)
        if task and not in_code:
            if current is None:
                current = {"title": "任务清单", "level": 2, "items": []}
                sections.append(current)
            current["items"].append({"state": state_names.get(task.group(1).lower(), "todo"), "label": task.group(2)})
        elif line.strip() and not in_code:
            other_lines.append(line)
    sections = [section for section in sections if section["items"]]
    items = [item for section in sections for item in section["items"]]
    state_counts = {state: sum(item["state"] == state for item in items)
                    for state in ("done", "todo", "in-progress", "waiting", "blocked", "superseded")}
    completed = state_counts["done"]
    all_complete = bool(items) and completed == len(items)
    return {"summary": (f"真实任务文件当前全部完成：{completed} 个子任务均已完成"
                         if all_complete else f"{completed} 个子任务已完成，{len(items) - completed} 个子任务仍待处理"),
            "state_counts": state_counts, "all_complete": all_complete,
            "sections": sections, "other_content": "\n".join(other_lines).strip()}


class Service:
    def __init__(self, config: dict[str, Any], store: Store):
        self.config, self.store = config, store
        self.git, self.browser = GitSource(config), OpenBrowserUse(config)
        self.last_error = ""
        self.browser_status: dict[str, Any] = {"state": "unknown", "label": "未检测", "checked_at": None,
                                               "target": f"{self.browser.chat.get('browser', 'chrome')}:{self.browser.chat.get('profile', 'Default')}",
                                               "detail": "尚未执行连接检测"}

    def check_browser(self) -> dict[str, Any]:
        self.browser_status = self.browser.check_connection()
        return self.browser_status

    def auto_mode(self) -> bool:
        return bool(not self.store.setting("approval_required") and self.store.setting("auto_submit"))

    def repository_status(self) -> dict[str, Any]:
        """Expose the configured handoff identity without enabling unsafe hot-switching."""
        watch_branches = self.git.watch_branches
        watched = "全部远端分支" if watch_branches == "all" else (
            ", ".join(watch_branches) if isinstance(watch_branches, list) else str(watch_branches)
        )
        return {
            "name": str(self.config.get("repository", "未命名交接仓库")),
            "local_path": str(self.git.repo),
            "remote": self.git.remote,
            "watch_branches": watched,
            "mode": "single-configured-repository",
            "can_switch_live": False,
        }

    @staticmethod
    def _text_field(payload: dict[str, Any], key: str, minimum: int = 1, maximum: int = 200) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum) or any(ord(char) < 32 for char in value):
            raise ValueError(f"invalid {key}")
        return value.strip()

    def create_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            repository = self._text_field(payload, "repository", 1, 200)
            branch = self._text_field(payload, "branch", 1, 255)
            web_conversation_id = self._text_field(payload, "web_conversation_id", 1, 200)
            title = self._text_field(payload, "web_conversation_title", 0, 200) if payload.get("web_conversation_title") else ""
            pr_number = payload.get("pr_number")
            expires_seconds = payload.get("expires_seconds", 900)
            if repository != self.config.get("repository"):
                raise ValueError("repository 必须匹配当前配置仓库")
            if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
                raise ValueError("invalid pr_number")
            if not isinstance(expires_seconds, int) or isinstance(expires_seconds, bool) or not 60 <= expires_seconds <= 3600:
                raise ValueError("expires_seconds 必须在 60 到 3600 之间")
        except ValueError as exc:
            return {"ok": False, "status": "invalid", "error": str(exc)}
        token = secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc)
        expires_at = (created.timestamp() + expires_seconds)
        expires = datetime.fromtimestamp(expires_at, timezone.utc).isoformat(timespec="seconds")
        return self.store.create_binding({"binding_id": f"bind-{secrets.token_urlsafe(12)}", "repository": repository,
                                          "branch": branch, "pr_number": pr_number, "web_conversation_id": web_conversation_id,
                                          "web_conversation_title": title, "token": token,
                                          "token_hash": hashlib.sha256(token.encode()).hexdigest(), "expires_at": expires})

    def claim_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            binding_id = self._text_field(payload, "binding_id", 8, 128)
            token = self._text_field(payload, "token", 20, 200)
            data = {key: self._text_field(payload, key) for key in ("route_id", "local_agent_id", "local_conversation_id", "repository", "branch")}
            data["pr_number"] = payload.get("pr_number")
            if not isinstance(data["pr_number"], int) or isinstance(data["pr_number"], bool) or data["pr_number"] < 1:
                raise ValueError("invalid pr_number")
            data["local_conversation_title"] = self._text_field(payload, "local_conversation_title", 0, 200) if payload.get("local_conversation_title") else ""
        except ValueError as exc:
            return {"ok": False, "status": "invalid", "error": str(exc)}
        return self.store.claim_binding(binding_id, hashlib.sha256(token.encode()).hexdigest(), data)

    def confirm_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            binding_id = self._text_field(payload, "binding_id", 8, 128)
            token = self._text_field(payload, "confirm_token", 20, 200)
            data = {key: self._text_field(payload, key) for key in ("route_id", "local_agent_id", "local_conversation_id", "repository", "branch")}
            data["pr_number"] = payload.get("pr_number")
            if not isinstance(data["pr_number"], int) or isinstance(data["pr_number"], bool) or data["pr_number"] < 1:
                raise ValueError("invalid pr_number")
        except ValueError as exc:
            return {"ok": False, "status": "invalid", "error": str(exc)}
        return self.store.confirm_binding(binding_id, hashlib.sha256(token.encode()).hexdigest(), data)

    def revoke_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            binding_id = self._text_field(payload, "binding_id", 8, 128)
            token = self._text_field(payload, "token", 20, 200)
        except ValueError as exc:
            return {"ok": False, "status": "invalid", "error": str(exc)}
        return self.store.revoke_binding(binding_id, hashlib.sha256(token.encode()).hexdigest())

    def refresh_binding_name(self, binding_id: str) -> dict[str, Any]:
        # Cloud metadata is intentionally not inferred from browser titles or cookies.
        if not BINDING_ID_RE.fullmatch(binding_id):
            return {"ok": False, "error": "invalid binding_id"}
        if not self.store.binding(binding_id):
            return {"ok": False, "error": "binding 不存在"}
        return {"ok": False, "error": "云端元数据不可访问；请由 ChatGPT 端显式回写对话名称"}

    def active_binding_for_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        branch = str(event.get("ref") or "").removeprefix(f"{self.git.remote}/")
        pr_number = event.get("pr_number")
        matches = [item for item in self.store.list_bindings(str(self.config.get("repository", "")))
                   if item.get("status") == "active" and item.get("branch") == branch and item.get("pr_number") == pr_number]
        return matches[0] if len(matches) == 1 else None

    def task_document(self, pr_number: int | None = None) -> dict[str, Any]:
        """Read only the in-repository handoff task file for a PR."""
        repo = self.git.repo.resolve()
        if pr_number is not None and (not isinstance(pr_number, int) or pr_number < 1):
            return {"ok": False, "error": "invalid PR number"}
        if pr_number is None:
            candidates: list[Path] = []
            try:
                for candidate in (repo / "coordination").glob("PR-*/任务.md"):
                    relative = candidate.resolve().relative_to(repo)
                    if re.fullmatch(r"coordination/PR-[1-9][0-9]*/任务\.md", relative.as_posix()):
                        candidates.append(candidate.resolve())
            except (OSError, ValueError):
                candidates = []
            if len(candidates) != 1:
                return {"ok": False, "unassigned": True,
                        "error": "未关联 PR，当前仓库无法唯一确定任务.md"}
            path, unassigned = candidates[0], True
        else:
            path, unassigned = (repo / "coordination" / f"PR-{pr_number}" / "任务.md").resolve(), False
        try:
            relative = path.relative_to(repo)
        except ValueError:
            return {"ok": False, "error": "任务文件路径不在交接仓库内"}
        if not re.fullmatch(r"coordination/PR-[1-9][0-9]*/任务\.md", relative.as_posix()):
            return {"ok": False, "error": "任务文件路径不符合允许范围"}
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"ok": False, "unassigned": unassigned, "path": relative.as_posix(), "error": "任务.md 不存在"}
        except (OSError, UnicodeError):
            return {"ok": False, "unassigned": unassigned, "path": relative.as_posix(), "error": "任务.md 无法读取"}
        truncated = len(content) > 20000
        content = content[:20000]
        parsed = parse_task_markdown(content)
        return {"ok": True, "unassigned": unassigned, "pr_number": pr_number,
                "path": relative.as_posix(), "content": content, "truncated": truncated,
                **parsed}

    def task_history(self, pr_number: int | None = None) -> dict[str, Any]:
        """Return local Git snapshots for the allowed PR task file only."""
        repo = self.git.repo.resolve()
        if pr_number is None:
            current_lookup = self.task_document()
            match = re.fullmatch(r"coordination/PR-([1-9][0-9]*)/任务\.md", str(current_lookup.get("path", "")))
            if not match:
                return {"ok": False, "unassigned": True, "error": "未关联 PR 无法唯一确定任务.md"}
            pr_number = int(match.group(1))
        if not isinstance(pr_number, int) or pr_number < 1:
            return {"ok": False, "error": "invalid PR number"}
        relative = Path("coordination") / f"PR-{pr_number}" / "任务.md"
        if not re.fullmatch(r"coordination/PR-[1-9][0-9]*/任务\.md", relative.as_posix()):
            return {"ok": False, "error": "任务文件路径不符合允许范围"}
        try:
            path = (repo / relative).resolve()
            path.relative_to(repo)
        except (OSError, ValueError):
            return {"ok": False, "error": "任务文件路径不在交接仓库内"}
        try:
            log = run(["git", "log", "--follow", "--format=%H%x09%cI%x09%s", "--", relative.as_posix()], repo)
        except RuntimeError as exc:
            return {"ok": False, "path": relative.as_posix(), "error": f"本地 Git 历史不可用：{exc}"}
        snapshots: list[dict[str, Any]] = []
        for line in log.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                continue
            sha, committed_at, subject = parts
            try:
                content = run(["git", "show", f"{sha}:{relative.as_posix()}"], repo)
            except RuntimeError:
                continue
            content = content[:20000]
            parsed = parse_task_markdown(content)
            snapshots.append({"commit_sha": sha, "short_sha": sha[:8], "committed_at": committed_at,
                               "subject": subject, "content": content, **parsed})
        snapshots.reverse()
        current = self.task_document(pr_number)
        return {"ok": True, "path": relative.as_posix(), "pr_number": pr_number, "current": current,
                "snapshots": snapshots, "history_count": len(snapshots)}

    def set_auto_mode(self, enabled: bool) -> dict[str, Any]:
        """Set the explicit unattended mode and drain existing approvals.

        The two low-level settings remain available for diagnostics, but the
        user-facing mode is intentionally coupled: unattended mode means no
        per-event approval and an actual ChatGPT Web submit. Turning it off
        restores the safe fill-only approval workflow.
        """
        self.store.set_settings({"approval_required": not enabled, "auto_submit": enabled})
        dispatched = self.drain_auto_mode() if enabled else []
        return {"ok": True, "auto_mode": enabled, "drained_event_keys": dispatched}

    def drain_auto_mode(self) -> list[str]:
        """Route approvals and submit verified drafts while unattended mode is on."""
        if not self.auto_mode():
            return []
        event_keys = [event["event_key"] for event in self.store.pending_events()]
        for event_key in event_keys:
            self.dispatch_event(event_key)
        fill_only_keys = [event["event_key"] for event in self.store.fill_only_events()]
        for event_key in fill_only_keys:
            self.dispatch_event(event_key, allow_fill_only_resubmit=True)
        event_keys.extend(fill_only_keys)
        return event_keys

    def wake_prompt(self, commit: Commit, pr: int | None, event_id: str | None = None, ref: str = "", binding: dict[str, Any] | None = None) -> str:
        follow_up = ("本事件尚未关联 PR。请检查上述分支是否只包含一个可关闭目标；若是且你拥有写权限，"
                     "创建真实 PR，并只在获得真实编号后从 TEMPLATE 实例化 coordination/PR-<N>/。"
                     "不得虚构 PR 编号。" if pr is None else
                     "请按 Coordinator 协议重新读取该 PR 的当前 HEAD、任务.md 和 agent汇报.md。")
        binding_line = (f"Binding-ID: {binding['binding_id']}\nRoute-ID: {binding['route_id']}\nWeb-Conversation-ID: {binding['web_conversation_id']}\n"
                        if binding else "Binding: no active binding (legacy unpaired route)\n")
        return ("GitHub 协作事件已到达。\n\n"
                f"Repository: {self.config['repository']}\nBranch: {ref or 'unknown'}\nPR: #{pr if pr else 'unassigned'}\n"
                f"Origin: agent\nHead: {commit.sha}\nEvent-ID: {event_id or commit.event_id}\n{binding_line}\n"
                f"{follow_up}\n本消息仅用于唤醒；不要依据本聊天中的旧状态猜测项目事实。")

    def wake_prompt_for_event(self, event: dict[str, Any], binding: dict[str, Any] | None = None) -> str:
        return self.wake_prompt(Commit(event["sha"], "", event["subject"]), event["pr_number"], event["event_key"], event["ref"], binding)

    def dispatch_agent(self, commit: Commit, pr: int | None, binding: dict[str, Any] | None = None) -> str:
        command = self.config["agent"].get("command", [])
        if not command:
            raise RuntimeError("agent.command is empty; configure a local command before enabling this route")
        route = (f" Binding-ID={binding['binding_id']} Route-ID={binding['route_id']} Local-Agent-ID={binding['local_agent_id']} Local-Conversation-ID={binding['local_conversation_id']}."
                 if binding else " No active binding metadata was supplied; do not broadcast this event.")
        prompt = (f"GitHub coordination event {commit.event_id}: ChatGPT updated PR #{pr or 'unknown'}. "
                  "Fetch the configured handoff repository, read its current README, task and chatgpt解惑.md, then continue only the current task." + route)
        subprocess.Popen([*command, prompt], cwd=self.git.repo, start_new_session=True)
        return "local agent process started"

    def handle(self, commit: Commit, ref: str) -> None:
        if commit.origin not in {"agent", "chatgpt"}:
            return
        pr = self.git.pr_number(commit.sha)
        event = {"event_key": commit.event_id, "sha": commit.sha, "ref": ref, "pr_number": pr,
                 "origin": commit.origin, "caused_by": commit.caused_by, "subject": commit.subject,
                 "observed_at": now(), "status": "detected"}
        if not self.store.add_event(event):
            return
        if not self.store.setting("enabled"):
            self.store.finish(commit.event_id, "skipped: paused")
            return
        route = "agent_to_chatgpt" if commit.origin == "agent" else "chatgpt_to_agent"
        if not self.store.setting(route):
            self.store.finish(commit.event_id, f"skipped: {route} disabled")
            return
        if self.store.setting("approval_required"):
            self.store.finish(commit.event_id, "awaiting approval", "Open the local dashboard and approve this one event.")
            return
        self.dispatch_event(commit.event_id)

    def dispatch_event(self, event_key: str, allow_fill_only_resubmit: bool = False) -> None:
        event = self.store.event(event_key)
        if not event:
            raise RuntimeError("event not found")
        if event["status"] == "dispatched" and not allow_fill_only_resubmit:
            return
        if event["status"] == "dispatched" and event["detail"] != "filled; verified; waiting for user submit":
            return
        try:
            binding = self.active_binding_for_event(event)
            if self.config.get("binding", {}).get("require_active", False) and not binding:
                raise RuntimeError("no active binding matches repository/branch/PR")
            if event["origin"] == "agent":
                if binding:
                    url = str(self.config.get("chatgpt", {}).get("conversation_url", ""))
                    conversation_id = re.search(r"/c/([^/?#]+)", url)
                    if not conversation_id or conversation_id.group(1) != binding["web_conversation_id"]:
                        raise RuntimeError("configured ChatGPT conversation does not match active binding")
                detail = self.browser.dispatch(self.wake_prompt_for_event(event, binding), self.store.setting("auto_submit"))
            else:
                commit = Commit(event["sha"], "", event["subject"])
                detail = self.dispatch_agent(commit, event["pr_number"], binding)
            self.store.finish(event_key, "dispatched", detail)
        except Exception as exc:
            self.store.finish(event_key, "needs human", str(exc))

    def poll_once(self) -> dict[str, Any]:
        try:
            run(["git", "fetch", self.git.remote, "--prune", "--quiet"], self.git.repo)
            observed = []
            for ref in self.git.refs():
                head, commits = self.git.poll(ref, self.store.cursor(ref))
                for commit in commits:
                    self.handle(commit, ref)
                self.store.set_cursor(ref, head)
                observed.append({"ref": ref, "commits": len(commits), "head": head})
            self.last_error = ""
            return {"refs": observed, "commits": sum(item["commits"] for item in observed)}
        except Exception as exc:
            self.last_error = str(exc)
            return {"error": self.last_error}


HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>GitHub 协作触发器</title>
<style>
:root{color-scheme:dark;--bg:#10131a;--surface:#191f2b;--surface-2:#222a38;--line:#344055;--text:#f5f7fb;--muted:#aab4c6;--good:#6ce0a2;--warn:#ffc76d;--danger:#ff9a9a;--accent:#83b6ff}*{box-sizing:border-box}body{font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:1180px;margin:0 auto;padding:32px 20px 48px;background:var(--bg);color:var(--text)}h1,h2,p{margin-top:0}h1{font-size:clamp(24px,4vw,34px);margin-bottom:5px}.eyebrow{color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px}.lede{color:var(--muted);max-width:720px;margin-bottom:24px}.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px;margin:16px 0}.panel-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px}.panel-heading h2{font-size:17px;margin:0}.hint{color:var(--muted);font-size:13px}.notice{display:none;border:1px solid var(--danger);border-radius:8px;background:#352126;color:#ffd4d4;padding:10px 12px;margin:14px 0}.browser{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.health{font-weight:700}.connected{color:var(--good)}.disconnected{color:var(--danger)}.unknown{color:var(--warn)}.detail{color:var(--muted);min-width:220px;flex:1}.mode{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px;border:1px solid var(--line);border-radius:9px;background:var(--surface-2);margin-bottom:12px}.mode-copy{color:var(--muted);font-size:13px;max-width:620px}.toggle-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.toggle{width:100%;text-align:left}.toggle small{display:block;color:var(--muted);font-weight:400;margin-top:3px}button,select{font:inherit;border:1px solid #53627a;border-radius:7px;background:#242d3c;color:var(--text);padding:8px 10px;cursor:pointer}button:hover{border-color:var(--accent);background:#2b3850}button:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.primary{border-color:#4f9cf9;background:#245794}.primary:hover{background:#2d6eba}.danger{border-color:#a45158;color:#ffc5c8}.on{color:var(--good);border-color:#4c9e73}.off{color:var(--warn);border-color:#9a7742}.summary{display:flex;gap:8px;flex-wrap:wrap}.pill{display:inline-flex;gap:5px;align-items:center;border:1px solid var(--line);border-radius:999px;padding:4px 9px;color:var(--muted);font-size:12px}.pill.good{border-color:#40815f;color:var(--good)}.pill.warn{border-color:#9a7742;color:var(--warn)}.pill.danger{border-color:#a45158;color:var(--danger)}.filters{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.filters select{padding:6px 8px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:820px}th,td{padding:12px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-size:12px;font-weight:600;white-space:nowrap}.event-meta{color:var(--muted);font-size:12px;margin-top:5px;word-break:break-word}.status{font-weight:700;white-space:nowrap}.status-ok{color:var(--good)}.status-warn{color:var(--warn)}.status-danger{color:var(--danger)}.event-action{margin-top:9px}.empty{text-align:center;color:var(--muted);padding:26px}.approvals{border-color:#9a7742;background:#2b261c}.approval{border-top:1px solid #5e5238;padding:12px 0}.approval:first-child{border-top:0}.approval code{display:block;white-space:pre-wrap;color:#ffe0a6;margin:8px 0;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}@media(max-width:760px){body{padding:20px 14px}.toggle-grid{grid-template-columns:1fr}.mode{align-items:flex-start;flex-direction:column}.detail{min-width:0}.panel{padding:14px}}
</style>
<main><header><div class="eyebrow">本机确定性服务</div><h1>GitHub ↔ ChatGPT Web 协作触发器</h1><p class="lede">默认逐条审批和填入草稿。只有你明确开启自动审批模式，才会批准事件并发送 ChatGPT 消息。</p></header>
<div id="notice" class="notice" role="alert"></div>
<section class="panel" aria-labelledby="browser-heading"><div class="panel-heading"><h2 id="browser-heading">浏览器连接</h2><span class="hint">只检测已配置的 profile，不读取登录信息。</span></div><div id="browser" class="browser" aria-live="polite"></div></section>
<section class="panel" aria-labelledby="controls-heading"><div class="panel-heading"><h2 id="controls-heading">路由控制</h2><span id="last-update" class="hint"></span></div><div id="controls"></div></section>
<section class="panel" aria-labelledby="events-heading"><div class="panel-heading"><div><h2 id="events-heading">交接时间线</h2><span id="event-summary" class="hint"></span></div><div class="filters"><label for="event-filter">显示</label><select id="event-filter"><option value="all">全部事件</option><option value="action">待处理与需要人工处理</option><option value="pending">仅等待审批</option><option value="human">仅需要人工处理</option><option value="done">仅已执行</option></select><button id="poll-now">立即检查 GitHub</button></div></div><div class="table-wrap"><table><thead><tr><th>发现时间</th><th>PR</th><th>来源</th><th>提交与事件</th><th>状态</th><th>详情与操作</th></tr></thead><tbody id="events"></tbody></table></div></section></main>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label={enabled:'总开关',agent_to_chatgpt:'Agent → ChatGPT',chatgpt_to_agent:'ChatGPT → Agent'};
const description={enabled:'暂停时只记录已跳过的事件。',agent_to_chatgpt:'控制本地 Agent 唤醒 ChatGPT Web。',chatgpt_to_agent:'控制 ChatGPT 更新启动已配置的本地 Agent。'};
const status={detected:'已发现','awaiting approval':'等待审批',dispatched:'已执行','needs human':'需要人工处理','skipped: paused':'已跳过：总开关暂停','skipped: agent_to_chatgpt disabled':'已跳过：Agent → ChatGPT 已关闭','skipped: chatgpt_to_agent disabled':'已跳过：ChatGPT → Agent 已关闭'};
const origin={agent:'本地 Agent',chatgpt:'远程 ChatGPT'};
let latest=null, eventFilter='all', lastStatusFingerprint='';
const preserveScrollSelector='#canvas-detail,#canvas-viewport,#task-document-raw';
const captureScrollPosition=()=>{const root=document.scrollingElement||document.documentElement,pageMax=Math.max(0,root.scrollHeight-window.innerHeight);return {pageX:window.scrollX,pageY:window.scrollY,pageMax,atBottom:window.scrollY>=pageMax-8,targets:[...document.querySelectorAll(preserveScrollSelector)].map((element,index)=>{const max=Math.max(0,element.scrollHeight-element.clientHeight);return {key:element.id||element.className||String(index),left:element.scrollLeft,top:element.scrollTop,max,atBottom:element.scrollTop>=max-8}})}};
let lastUserScrollAt=0, restoringScroll=false;
window.addEventListener('scroll',()=>{if(!restoringScroll)lastUserScrollAt=performance.now()},{passive:true});
const restoreScrollPosition=state=>{if(!state||performance.now()-lastUserScrollAt<700)return;requestAnimationFrame(()=>{if(performance.now()-lastUserScrollAt<700)return;restoringScroll=true;const root=document.scrollingElement||document.documentElement,max=Math.max(0,root.scrollHeight-window.innerHeight),pageY=state.atBottom?max:Math.min(state.pageY,max);window.scrollTo(state.pageX,pageY);state.targets.forEach(saved=>{const element=document.getElementById(saved.key)||document.querySelector('.'+String(saved.key).split(' ').join('.'));if(element){const currentMax=Math.max(0,element.scrollHeight-element.clientHeight);element.scrollLeft=saved.left;element.scrollTop=saved.atBottom?currentMax:Math.min(saved.top,currentMax)}});requestAnimationFrame(()=>{restoringScroll=false})})};
const notice=message=>{const box=document.querySelector('#notice');box.textContent=message||'';box.style.display=message?'block':'none'};
async function api(path,options={}){const response=await fetch(path,options);let data={};try{data=await response.json()}catch(_){}if(!response.ok)throw new Error(data.error||'请求失败');return data}
async function set(k,v){try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k,value:v})});notice('')}catch(error){notice('无法更新“'+label[k]+'”：'+error.message)}await load()}
async function setMode(v){try{const data=await api('/api/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auto:v})});notice(v?'自动审批模式已开启；已处理 '+data.drained_event_keys.length+' 个符合条件的事件。':'已恢复逐条审批和填入草稿模式。')}catch(error){notice('切换自动审批模式失败：'+error.message)}await load()}
async function poll(){try{const data=await api('/api/poll',{method:'POST'});if(data.error)notice('GitHub 检查失败：'+data.error);else notice('已检查 GitHub；发现 '+data.commits+' 个新提交。')}catch(error){notice('GitHub 检查失败：'+error.message)}await load()}
async function approve(key){try{await api('/api/approve/'+encodeURIComponent(key),{method:'POST'});notice('事件已提交给触发器处理。')}catch(error){notice('无法处理此事件：'+error.message)}await load()}
async function checkBrowser(){const box=document.querySelector('#browser');box.textContent='浏览器连接：检测中…';try{renderBrowser(await api('/api/browser/check',{method:'POST'}))}catch(error){notice('浏览器连接检测失败：'+error.message)}}
function renderBrowser(browser){document.querySelector('#browser').innerHTML=`<span class="health ${esc(browser.state)}">● ${esc(browser.label)}</span><span class="pill">${esc(browser.target||'未配置')}</span><span class="detail">${esc(browser.detail||'')} ${browser.checked_at?'（'+esc(browser.checked_at)+'）':''}</span><button id="check-browser">检测浏览器连接</button>`;document.querySelector('#check-browser').addEventListener('click',checkBrowser)}
function tone(event){if(event.status==='dispatched')return 'ok';if(event.status==='needs human')return 'danger';return 'warn'}
function visibleEvents(events){return events.filter(event=>eventFilter==='all'||eventFilter==='action'&&['awaiting approval','needs human'].includes(event.status)||eventFilter==='pending'&&event.status==='awaiting approval'||eventFilter==='human'&&event.status==='needs human'||eventFilter==='done'&&event.status==='dispatched')}
function renderEvents(data){const events=visibleEvents(data.events), pending=data.events.filter(e=>e.status==='awaiting approval').length, human=data.events.filter(e=>e.status==='needs human').length;document.querySelector('#event-summary').innerHTML=`<span class="summary"><span class="pill">共 ${data.events.length} 个</span><span class="pill ${pending?'warn':'good'}">${pending} 个等待审批</span><span class="pill ${human?'danger':'good'}">${human} 个需要人工处理</span></span>`;const body=document.querySelector('#events');if(!events.length){body.innerHTML='<tr><td class="empty" colspan="6">当前筛选条件下没有事件。</td></tr>';return}body.innerHTML=events.map(event=>{const retry=['awaiting approval','needs human'].includes(event.status),eventId=esc(event.event_key),causedBy=event.caused_by?'<div class="event-meta">由 '+esc(event.caused_by)+' 引起</div>':'';return `<tr><td>${esc(event.observed_at)}</td><td>${event.pr_number==null?'<span class="pill">未关联</span>':'<span class="pill">#'+esc(event.pr_number)+'</span>'}</td><td>${esc(origin[event.origin]||event.origin)}</td><td><strong>${esc(event.sha.slice(0,8))}</strong><div>${esc(event.subject)}</div><div class="event-meta">${eventId}</div><div class="event-meta">${esc(event.ref||'')}</div>${causedBy}</td><td class="status status-${tone(event)}">${esc(status[event.status]||event.status)}</td><td>${esc(event.detail||'—')}${retry?`<br><button class="event-action ${event.status==='needs human'?'danger':'primary'}" data-event-key="${eventId}">${event.status==='needs human'?'修复后重试':'批准此事件'}</button>`:''}</td></tr>`}).join('');body.querySelectorAll('[data-event-key]').forEach(button=>button.addEventListener('click',()=>approve(button.dataset.eventKey)))}
function renderControls(data){const settings=data.settings,controls=document.querySelector('#controls');controls.innerHTML=`<div class="mode"><div><button id="auto-mode" class="${data.auto_mode?'on':'off'}">${data.auto_mode?'✓ 自动审批模式：已开启':'○ 自动审批模式：已关闭'}</button><div class="mode-copy">${data.auto_mode?'新事件和现有等待审批事件会自动处理；已验证的草稿可提交一次。':'安全模式：每条事件需要单独批准，ChatGPT 消息默认只填入草稿。'}</div></div><span class="pill ${data.auto_mode?'warn':'good'}">${data.auto_mode?'会自动发送':'逐条审批'}</span></div><div class="toggle-grid">${['enabled','agent_to_chatgpt','chatgpt_to_agent'].map(key=>`<button class="toggle ${settings[key]?'on':'off'}" data-setting="${key}"><strong>${settings[key]?'✓ 已开启':'○ 已关闭'}：${label[key]}</strong><small>${description[key]}</small></button>`).join('')}</div>`;controls.querySelector('#auto-mode').addEventListener('click',()=>setMode(!data.auto_mode));controls.querySelectorAll('[data-setting]').forEach(button=>button.addEventListener('click',()=>set(button.dataset.setting,!settings[button.dataset.setting])))}
const statusFingerprint=data=>JSON.stringify({events:data.events,settings:data.settings,browser:{state:data.browser?.state,label:data.browser?.label,target:data.browser?.target,detail:data.browser?.detail},repository:data.repository,auto_mode:data.auto_mode,last_error:data.last_error});
async function load(){try{const data=await api('/api/status'),fingerprint=statusFingerprint(data),changed=fingerprint!==lastStatusFingerprint,scrollState=changed?captureScrollPosition():null;latest=data;if(changed){renderBrowser(data.browser);renderControls(data);renderEvents(data);lastStatusFingerprint=fingerprint;restoreScrollPosition(scrollState)}document.querySelector('#last-update').textContent='页面刷新于 '+new Date().toLocaleTimeString();if(data.last_error)notice('后台轮询错误：'+data.last_error)}catch(error){notice('无法读取 Dashboard 状态：'+error.message)}}
document.querySelector('#poll-now').addEventListener('click',poll);document.querySelector('#event-filter').addEventListener('change',event=>{eventFilter=event.target.value;if(latest)renderEvents(latest)});load();setInterval(load,5000);
</script>"""
HTML += """<script>
const approvalText = p => `${p.reason || 'Codex 请求执行操作'}\\n\\n${p.command || '文件变更'}${p.cwd ? '\\n工作目录：' + p.cwd : ''}`;
async function resolveApproval(id, decision) {
  await fetch('/api/approvals/' + encodeURIComponent(id), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({decision})});
  loadApprovals();
}
async function loadApprovals() {
  const box = document.querySelector('#approvals') || (() => { const x=document.createElement('div'); x.id='approvals'; x.className='approvals'; document.querySelector('#controls').after(x); return x; })();
  const data = await (await fetch('/api/approvals')).json();
  box.style.display = data.requests.length ? 'block' : 'none';
  box.innerHTML = data.requests.length ? '<strong>Codex 待审批操作（不是自动审批模式）</strong>' + data.requests.map(r => `<div class=approval><code>${esc(approvalText(r.params||{}))}</code><button onclick="resolveApproval('${esc(r.id)}','accept')">允许这一次</button><button onclick="resolveApproval('${esc(r.id)}','acceptForSession')">始终允许（本会话）</button><button onclick="resolveApproval('${esc(r.id)}','decline')">拒绝</button></div>`).join('') : '';
}
loadApprovals(); setInterval(loadApprovals, 1000);
</script>"""

# The first HTML block keeps the dependency-free dashboard shell.  This layer
# supplies the richer, accessible dashboard surface without changing routing APIs.
HTML += """<style>
:root{color-scheme:light;--bg:#f3f6fb;--surface:rgba(255,255,255,.88);--surface-2:#f7f9fd;--line:#dce3ee;--text:#172033;--muted:#68758b;--good:#168451;--warn:#a96600;--danger:#c23838;--accent:#276ef1}body{background:radial-gradient(circle at 10% -10%,#dceaff 0,transparent 34%),radial-gradient(circle at 100% 0,#e7dcff 0,transparent 31%),var(--bg)!important;color:var(--text)!important;letter-spacing:.005em}.eyebrow{color:var(--accent)!important}.lede,.hint,.detail,.event-meta,.mode-copy{color:var(--muted)!important}.panel,#repository-card,#pr-timeline{background:var(--surface)!important;border:1px solid rgba(219,227,239,.9)!important;box-shadow:0 14px 34px rgba(41,57,86,.08);backdrop-filter:blur(14px)}.panel{border-radius:20px!important;padding:20px!important}.panel-heading h2{color:var(--text)}button,select{border-color:#d0d9e7!important;background:#fff!important;color:var(--text)!important;box-shadow:0 1px 2px rgba(31,47,73,.04);transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease,background .18s ease}button:hover{border-color:#a9bfe8!important;background:#f7faff!important;box-shadow:0 5px 14px rgba(39,110,241,.12);transform:translateY(-1px)}button.primary{background:var(--accent)!important;border-color:var(--accent)!important;color:#fff!important}button.primary:hover{background:#185bd5!important}.on{color:var(--good)!important;border-color:#9ed8bb!important;background:#f3fbf7!important}.off{color:var(--warn)!important;border-color:#f0cf92!important;background:#fffaf0!important}.mode{background:#f5f8fe!important;border-color:#dce5f4!important;border-radius:14px!important}.pill{background:#fff!important;border-color:#dae2ee!important;color:#5d6c83!important}.pill.good{color:var(--good)!important;border-color:#a5d8bd!important;background:#f4fbf7!important}.pill.warn{color:var(--warn)!important;border-color:#f0d097!important;background:#fff9ef!important}.pill.danger{color:var(--danger)!important;border-color:#edb6b6!important;background:#fff5f5!important}.notice{background:#fff5f5!important;border-color:#efb0b0!important;color:#9e2525!important;box-shadow:0 8px 22px rgba(144,45,45,.08)}.connected,.status-ok{color:var(--good)!important}.disconnected,.status-danger{color:var(--danger)!important}.unknown,.status-warn{color:var(--warn)!important}th{color:#728097!important;border-color:#e2e8f1!important}td{border-color:#e5ebf3!important}.approvals{background:#fffaf0!important;border-color:#f0d29a!important;box-shadow:none!important}.approval{border-color:#f0dcae!important}.approval code{color:#73510d!important;background:#fffdf7;padding:8px;border-radius:8px}
#repository-card{border-radius:20px;padding:19px 20px;margin:16px 0;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;align-items:center}.repo-kicker{color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}.repo-name{font-size:20px;font-weight:750;letter-spacing:-.02em;margin:3px 0 8px;word-break:break-word}.repo-meta{display:flex;gap:7px;flex-wrap:wrap}.repo-guidance{grid-column:1/-1;border-top:1px solid #e3e9f2;padding-top:13px;color:var(--muted);font-size:13px}.repo-guidance summary{cursor:pointer;color:var(--accent);font-weight:650}.repo-guidance p{margin:8px 0 0}.repo-guidance ol{margin:8px 0 0;padding-left:20px}.repo-mode{justify-self:end}
#pr-timeline{border-radius:20px;padding:20px;margin:16px 0}.timeline-heading{display:flex;gap:16px;align-items:baseline;justify-content:space-between;flex-wrap:wrap;margin-bottom:14px}.timeline-heading h2{font-size:18px;margin:0}.timeline-subtitle{color:var(--muted);font-size:13px}.timeline-track{display:grid;gap:16px}.timeline-card{position:relative;border:1px solid #dce5f1;border-radius:16px;background:linear-gradient(135deg,#fff,#fbfcff);box-shadow:0 6px 17px rgba(39,57,91,.06)}.timeline-card:not(:last-child)::after{content:"";position:absolute;left:30px;bottom:-18px;width:2px;height:19px;background:repeating-linear-gradient(to bottom,#8eafe9 0 5px,transparent 5px 10px);background-size:2px 20px;animation:timeline-flow 1.3s linear infinite}.timeline-summary{width:100%;display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:13px;align-items:center;text-align:left;border:0!important;background:transparent!important;box-shadow:none!important;padding:16px}.timeline-summary:hover{transform:none!important;box-shadow:none!important;background:#f8fbff!important}.timeline-number{display:grid;place-items:center;width:34px;height:34px;border-radius:11px;background:#edf4ff;color:var(--accent);font-weight:750}.timeline-title{font-weight:730;color:var(--text);font-size:16px}.timeline-brief{margin-top:3px;color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.timeline-chevron{color:var(--muted);font-size:18px;transition:transform .18s ease}.timeline-card.expanded .timeline-chevron{transform:rotate(180deg)}.timeline-detail{border-top:1px solid #e5ebf3;padding:0 16px 16px}.timeline-detail[hidden]{display:none}.timeline-event{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:13px 0;border-bottom:1px solid #edf1f6}.timeline-event:last-child{border-bottom:0}.timeline-event-title{font-weight:650}.timeline-event-copy{color:var(--muted);font-size:12px;margin-top:4px;word-break:break-word}.timeline-event-actions{text-align:right}.timeline-empty{padding:24px;text-align:center;color:var(--muted);border:1px dashed #c8d4e5;border-radius:14px}@keyframes timeline-flow{to{background-position:0 20px}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition:none!important}}@media(max-width:760px){#repository-card{grid-template-columns:1fr}.repo-mode{justify-self:start}.timeline-event{grid-template-columns:1fr}.timeline-event-actions{text-align:left}.timeline-card:not(:last-child)::after{left:25px}.timeline-summary{grid-template-columns:auto minmax(0,1fr) auto}.panel,#pr-timeline{padding:16px!important}}
</style><script>
function prepareRichDashboard(){if(document.querySelector('#repository-card'))return;const main=document.querySelector('main'),header=main.querySelector('header'),noticeBox=document.querySelector('#notice'),legacyPanel=document.querySelector('#events-heading').closest('section');const repository=document.createElement('section');repository.id='repository-card';repository.setAttribute('aria-labelledby','repository-heading');repository.innerHTML='<div><div class="repo-kicker">当前交接仓库</div><div id="repository-name" class="repo-name">正在读取配置…</div><div id="repository-meta" class="repo-meta"></div></div><span id="repository-mode" class="pill repo-mode">单仓库配置</span><details class="repo-guidance"><summary>更换或配置交接仓库</summary><p>此触发器按启动配置运行一份交接仓库。为避免把 SQLite 的 cursor、事件去重记录和已授权的本地命令路由到错误仓库，Dashboard 不提供运行中热切换。</p><ol><li>编辑启动时传入的 <code>--config</code> 文件（默认 <code>trigger/config.local.json</code>）中的 <code>handoff_repo</code>、<code>repository</code> 和需要变更的本地命令。</li><li>确认目标仓库、remote、浏览器会话和 wrapper 授权都属于同一个项目。</li><li>重启 Trigger；首次轮询只建立新仓库 baseline，不会重放历史提交。</li></ol><p>当前版本没有仓库配置文件发现或多仓库状态隔离能力；因此页面会诚实显示当前配置，而不会把选择项伪装成已切换。</p></details>';header.after(repository);const timeline=document.createElement('section');timeline.id='pr-timeline';timeline.setAttribute('aria-labelledby','timeline-heading');timeline.innerHTML='<div class="timeline-heading"><div><h2 id="timeline-heading">PR 交接时间线</h2><div id="timeline-subtitle" class="timeline-subtitle">按 PR 汇总事件；点击卡片查看详情和可用操作。</div></div><span id="timeline-summary" class="pill">正在读取事件…</span></div><div id="timeline-cards" class="timeline-track"></div>';legacyPanel.before(timeline);legacyPanel.querySelector('#events-heading').textContent='全部事件明细'}
function renderRepository(repository){const item=repository||{},name=document.querySelector('#repository-name'),meta=document.querySelector('#repository-meta'),mode=document.querySelector('#repository-mode');if(!name)return;name.textContent=item.name||'未命名交接仓库';meta.innerHTML=`<span class="pill">remote: ${esc(item.remote||'未配置')}</span><span class="pill">${esc(item.watch_branches||'未配置')}</span><span class="pill">${esc(item.local_path||'本地路径未知')}</span>`;mode.textContent=item.can_switch_live?'可切换':'单仓库配置'}
function timelineGroups(events){const groups=new Map();for(const event of events){const key=event.pr_number==null?'unassigned':String(event.pr_number);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(event)}return [...groups.entries()]}
function renderTimeline(data){const cards=document.querySelector('#timeline-cards'),subtitle=document.querySelector('#timeline-subtitle'),summary=document.querySelector('#timeline-summary');if(!cards)return;const groups=timelineGroups(visibleEvents(data.events));const waiting=data.events.filter(event=>event.status==='awaiting approval').length,human=data.events.filter(event=>event.status==='needs human').length;summary.textContent=`${groups.length} 个 PR · ${waiting+human} 项需要关注`;subtitle.textContent=groups.length?'按最近活动排序；点击任意卡片展开事件、提交与审批操作。':'当前筛选条件下没有可展示的 PR 交接事件。';if(!groups.length){cards.innerHTML='<div class="timeline-empty">当前筛选条件下没有交接事件。</div>';return}cards.innerHTML=groups.map(([key,events],index)=>{const latestEvent=events[0],pending=events.some(event=>event.status==='awaiting approval'),needsHuman=events.some(event=>event.status==='needs human'),stage=needsHuman?'需要人工处理':pending?'等待审批':status[latestEvent.status]||latestEvent.status,stageClass=needsHuman?'danger':pending?'warn':'good',title=key==='unassigned'?'未关联 PR':'PR #'+key,detailId='timeline-detail-'+index;const details=events.map(event=>{const retry=['awaiting approval','needs human'].includes(event.status),eventId=esc(event.event_key),cause=event.caused_by?' · 由 '+esc(event.caused_by)+' 引起':'';return `<div class="timeline-event"><div><div class="timeline-event-title">${esc(event.subject)}</div><div class="timeline-event-copy">${esc(event.observed_at)} · ${esc(origin[event.origin]||event.origin)} · ${esc(event.sha.slice(0,8))}</div><div class="timeline-event-copy">Event ID: ${eventId}${cause}</div><div class="timeline-event-copy">${esc(event.ref||'未记录分支')} · ${esc(event.detail||'暂无详情')}</div></div><div class="timeline-event-actions"><span class="pill ${event.status==='needs human'?'danger':event.status==='awaiting approval'?'warn':'good'}">${esc(status[event.status]||event.status)}</span>${retry?`<br><button class="event-action ${event.status==='needs human'?'danger':'primary'}" data-event-key="${eventId}">${event.status==='needs human'?'修复后重试':'批准此事件'}</button>`:''}</div></div>`}).join('');return `<article class="timeline-card"><button type="button" class="timeline-summary" aria-expanded="false" aria-controls="${detailId}" data-timeline-card="${index}"><span class="timeline-number">${index+1}</span><span><span class="timeline-title">${title}</span><span class="timeline-brief">${esc(latestEvent.subject)} · ${events.length} 个事件</span></span><span><span class="pill ${stageClass}">${stage}</span><span class="timeline-chevron" aria-hidden="true">⌄</span></span></button><div id="${detailId}" class="timeline-detail" hidden>${details}</div></article>`}).join('');cards.querySelectorAll('[data-timeline-card]').forEach(button=>button.addEventListener('click',()=>{const card=button.closest('.timeline-card'),detail=document.getElementById(button.getAttribute('aria-controls')),expanded=button.getAttribute('aria-expanded')==='true';button.setAttribute('aria-expanded',String(!expanded));detail.hidden=expanded;card.classList.toggle('expanded',!expanded)}));cards.querySelectorAll('[data-event-key]').forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();approve(button.dataset.eventKey)}))}
prepareRichDashboard();const baseRenderEvents=renderEvents;const baseRenderTimeline=renderTimeline;const timelineCardKey=card=>card.querySelector('.timeline-title')?.textContent||'';renderTimeline=data=>{const cards=document.querySelector('#timeline-cards'),expanded=new Set([...cards?.querySelectorAll('.timeline-card')||[]].filter(card=>card.classList.contains('expanded')).map(timelineCardKey));baseRenderTimeline(data);cards?.querySelectorAll('.timeline-card').forEach(card=>{if(!expanded.has(timelineCardKey(card)))return;const button=card.querySelector('.timeline-summary'),detail=card.querySelector('.timeline-detail');button.setAttribute('aria-expanded','true');detail.hidden=false;card.classList.add('expanded')})};renderEvents=data=>{baseRenderEvents(data);renderRepository(data.repository);renderTimeline(data)};if(latest)renderEvents(latest);
</script></html>"""

# Canvas layer: the existing API/data model stays unchanged; this replaces the
# presentation of the PR timeline with a pannable, zoomable event canvas.
HTML += """<style>
#control-deck{position:sticky;top:12px;z-index:20;display:grid;gap:10px;padding:14px 16px;margin:16px 0;background:rgba(255,255,255,.86);border:1px solid #dbe4f0;border-radius:22px;box-shadow:0 16px 36px rgba(41,57,86,.11);backdrop-filter:blur(18px)}.control-deck-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.control-deck-heading h2{margin:0;font-size:18px}.control-deck-heading p{margin:4px 0 0;color:#68758b;font-size:13px}.control-deck-heading .pill{white-space:nowrap}.control-deck-section{border-top:1px solid #e6ebf3;padding-top:10px}.control-deck-section.panel{margin:0!important;border:0!important;border-radius:0!important;padding:0!important;background:transparent!important;box-shadow:none!important;backdrop-filter:none!important}.control-deck #repository-card{margin:0;padding:4px 0 10px;box-shadow:none;border:0!important;background:transparent!important;border-radius:0}.control-deck #repository-card .repo-guidance{background:#f7f9fd;border-radius:12px;padding:10px 12px}.control-deck #browser{padding:0 0 3px}.control-deck #controls{padding:0}.control-deck .panel-heading{margin-bottom:7px}.control-deck .panel-heading h2{font-size:14px;color:#68758b}.control-deck .panel-heading #last-update{font-size:11px}.control-deck .toggle-grid{grid-template-columns:repeat(3,minmax(130px,1fr))}.control-deck .mode{margin-bottom:8px}.control-deck .approvals{margin:10px 0 0!important}.canvas-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px}.canvas-toolbar h2{margin:0;font-size:19px}.canvas-toolbar p{margin:3px 0 0;color:#68758b;font-size:13px}.canvas-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.canvas-actions label{color:#68758b;font-size:13px}.canvas-actions select{min-width:150px}.canvas-actions button{min-width:38px}.canvas-zoom{display:inline-flex;align-items:center;gap:5px;padding:3px;border:1px solid #dce5f1;border-radius:10px;background:#fff}.canvas-zoom button{padding:5px 9px;border:0!important;box-shadow:none!important}.canvas-zoom output{min-width:45px;text-align:center;color:#68758b;font-size:12px}.canvas-layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px;align-items:stretch}.canvas-viewport{position:relative;min-height:540px;overflow:hidden;border:1px solid #dbe4f0;border-radius:17px;background:radial-gradient(circle,#d9e4f3 1px,transparent 1px),#f8fafd;background-size:22px 22px;cursor:grab;touch-action:none;outline:none}.canvas-viewport:focus-visible{box-shadow:0 0 0 3px rgba(39,110,241,.2)}.canvas-viewport.is-dragging{cursor:grabbing}.canvas-world{position:absolute;left:0;top:0;transform-origin:0 0;will-change:transform}.canvas-edges{position:absolute;left:0;top:0;overflow:visible;pointer-events:none}.canvas-edge{fill:none;stroke:#8eafe9;stroke-width:2;stroke-dasharray:7 8;animation:canvas-flow 1.2s linear infinite}.canvas-node{position:absolute;width:225px;min-height:118px;border:1px solid #d7e2f1;border-radius:15px;background:rgba(255,255,255,.96);box-shadow:0 8px 18px rgba(39,57,91,.1);transition:box-shadow .18s ease,border-color .18s ease}.canvas-node:hover{border-color:#99b9ed;box-shadow:0 11px 24px rgba(39,110,241,.15)}.canvas-node.is-selected{border-color:#4c88ed;box-shadow:0 0 0 3px rgba(76,136,237,.18),0 11px 24px rgba(39,110,241,.15)}.canvas-node-button{display:block;width:100%;min-height:116px;padding:13px 14px;text-align:left;border:0!important;border-radius:15px;background:transparent!important;box-shadow:none!important;color:#172033!important}.canvas-node-button:hover{transform:none!important;background:#f8fbff!important}.canvas-node-top{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:9px}.canvas-node-pr{color:#276ef1;font-size:12px;font-weight:750}.canvas-node-time{color:#8a97aa;font-size:11px}.canvas-node-title{display:block;color:#172033;font-size:14px;font-weight:700;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.canvas-node-meta{display:block;margin-top:9px;color:#68758b;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.canvas-node-status{display:inline-flex;margin-top:7px;padding:3px 7px;border-radius:999px;font-size:11px;font-weight:700}.canvas-node-status.ok{color:#168451;background:#f0fbf5}.canvas-node-status.warn{color:#a96600;background:#fff8e9}.canvas-node-status.danger{color:#c23838;background:#fff2f2}.canvas-detail{position:relative;min-height:540px;border:1px solid #dbe4f0;border-radius:17px;background:rgba(255,255,255,.88);padding:17px;overflow:auto}.canvas-detail.empty{display:grid;place-items:center;text-align:center;color:#68758b}.canvas-detail h3{margin:0 0 7px;color:#172033;font-size:17px;line-height:1.35}.canvas-detail .detail-label{color:#8a97aa;font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-top:13px}.canvas-detail .detail-value{color:#31415a;font-size:13px;word-break:break-word;margin-top:2px}.canvas-detail .detail-actions{margin-top:17px}.canvas-detail .detail-close{position:absolute;right:11px;top:9px;border:0!important;box-shadow:none!important;background:transparent!important;color:#8a97aa!important;padding:4px 7px}.canvas-empty{display:grid;place-items:center;min-height:500px;padding:40px;text-align:center;color:#68758b}.canvas-empty strong{display:block;color:#31415a;font-size:16px;margin-bottom:6px}@keyframes canvas-flow{to{stroke-dashoffset:-30}}@media(prefers-reduced-motion:reduce){.canvas-edge{animation:none}}@media(max-width:900px){#control-deck{position:relative;top:0}.canvas-layout{grid-template-columns:1fr}.canvas-detail{min-height:260px;max-height:none}.canvas-viewport{min-height:480px}}@media(max-width:680px){.control-deck .toggle-grid{grid-template-columns:1fr}.control-deck-heading{display:block}.control-deck-heading .pill{display:inline-flex;margin-top:8px}.canvas-viewport{min-height:420px}.canvas-toolbar{align-items:flex-start}.canvas-actions{width:100%}.canvas-actions select{flex:1}}
</style><script>
(function(){
  const controlDeck=document.createElement('section');controlDeck.id='control-deck';controlDeck.setAttribute('aria-labelledby','control-deck-heading');controlDeck.innerHTML='<div class="control-deck-heading"><div><div class="eyebrow">安全控制台</div><h2 id="control-deck-heading">链路控制</h2><p>GitHub 是事件中转；每个方向的开关与审批授权保持独立。</p></div><span class="pill good">本机服务 · 127.0.0.1</span></div>';
  const noticeBox=document.querySelector('#notice'),repositoryCard=document.querySelector('#repository-card'),browserPanel=document.querySelector('#browser').closest('section'),controlsPanel=document.querySelector('#controls').closest('section');[noticeBox,repositoryCard,browserPanel,controlsPanel].forEach((node,index)=>{if(!node)return;if(index>0)node.classList.add('control-deck-section');controlDeck.append(node)});document.querySelector('main header').after(controlDeck);
  const oldTimeline=document.querySelector('#pr-timeline'),canvasRoot=oldTimeline;oldTimeline.innerHTML='<div class="canvas-toolbar"><div><h2 id="canvas-heading">PR 事件画布</h2><p id="canvas-subtitle">拖拽平移、滚轮缩放；点击事件节点查看完整上下文。</p></div><div class="canvas-actions"><label for="canvas-pr-filter">项目 / PR</label><select id="canvas-pr-filter" aria-label="选择要显示的 PR"></select><span class="canvas-zoom"><button type="button" id="canvas-zoom-out" aria-label="缩小">−</button><output id="canvas-zoom-level">100%</output><button type="button" id="canvas-zoom-in" aria-label="放大">+</button></span><button type="button" id="canvas-fit">适配视图</button><button type="button" id="canvas-reset">重置视图</button></div></div><div class="canvas-layout"><div class="canvas-viewport" id="canvas-viewport" tabindex="0" role="application" aria-label="PR 事件画布，可拖拽平移和滚轮缩放"><div id="canvas-world" class="canvas-world"></div></div><aside id="canvas-detail" class="canvas-detail empty" aria-live="polite"><div>选择一个事件节点<br>查看完整变动、上下文和可用操作</div></aside></div>';oldTimeline.classList.add('canvas-panel');
  const legacyEvents=document.querySelector('#events-heading').closest('section');legacyEvents.style.display='none';
  const viewport=document.querySelector('#canvas-viewport'),world=document.querySelector('#canvas-world'),detail=document.querySelector('#canvas-detail'),filter=document.querySelector('#canvas-pr-filter'),zoomLevel=document.querySelector('#canvas-zoom-level');let canvasFilter='all',selectedEventKey=null,view={scale:1,x:0,y:0,fit:false,dragging:false,moved:false,startX:0,startY:0,originX:0,originY:0};
  const titleForKey=key=>key.startsWith('pr:')?'PR #'+key.slice(3):key.startsWith('unassigned:chain:')?'未关联 PR · 因果链':'未关联 PR · 独立事件';
  const canvasTracks=events=>{const ordered=[...events].sort((a,b)=>String(a.observed_at).localeCompare(String(b.observed_at)));const prGroups=new Map(),unassigned=[];ordered.forEach(event=>{if(event.pr_number==null)unassigned.push(event);else{const key='pr:'+event.pr_number;if(!prGroups.has(key))prGroups.set(key,[]);prGroups.get(key).push(event)}});const tracks=[...prGroups.entries()].map(([key,items])=>({key,title:titleForKey(key),events:items,edges:items.slice(1).map((event,index)=>[items[index].event_key,event.event_key])}));const byKey=new Map(unassigned.map(event=>[event.event_key,event])),seen=new Set;for(const event of unassigned){if(seen.has(event.event_key))continue;const component=new Set([event.event_key]),stack=[event.event_key];while(stack.length){const key=stack.pop(),current=byKey.get(key);for(const candidate of unassigned){if((candidate.caused_by===key||current?.caused_by===candidate.event_key)&&!component.has(candidate.event_key)){component.add(candidate.event_key);stack.push(candidate.event_key)}}}component.forEach(key=>seen.add(key));const items=unassigned.filter(item=>component.has(item.event_key));const chained=items.some(item=>item.caused_by&&component.has(item.caused_by));const root=items.find(item=>!item.caused_by)||items[0];const key=chained?'unassigned:chain:'+root.event_key:'unassigned:event:'+event.event_key;tracks.push({key,title:chained?'未关联 PR · 因果链 · '+root.subject:'未关联 PR · 独立事件 · '+event.subject,events:items,edges:chained?items.filter(item=>item.caused_by&&component.has(item.caused_by)).map(item=>[item.caused_by,item.event_key]):[]})}return tracks.sort((a,b)=>String(a.events[0]?.observed_at).localeCompare(String(b.events[0]?.observed_at)))};
  const updateTransform=()=>{world.style.transform=`translate(${view.x}px,${view.y}px) scale(${view.scale})`;zoomLevel.textContent=Math.round(view.scale*100)+'%'};
  const fitCanvas=()=>{const width=parseFloat(world.style.width)||1,height=parseFloat(world.style.height)||1,w=viewport.clientWidth,h=viewport.clientHeight;view.scale=Math.min(1,Math.max(.35,Math.min((w-40)/width,(h-40)/height)));view.x=Math.max(20,(w-width*view.scale)/2);view.y=Math.max(20,(h-height*view.scale)/2);view.fit=true;updateTransform()};
  const zoomCanvasAt=(factor,clientX,clientY)=>{const old=view.scale,next=Math.min(2.2,Math.max(.35,old*factor)),rect=viewport.getBoundingClientRect(),px=clientX-rect.left,py=clientY-rect.top;view.x=px-(px-view.x)*(next/old);view.y=py-(py-view.y)*(next/old);view.scale=next;updateTransform()};
  const zoomCanvas=factor=>zoomCanvasAt(factor,viewport.getBoundingClientRect().left+viewport.clientWidth/2,viewport.getBoundingClientRect().top+viewport.clientHeight/2);
  const renderDetail=event=>{if(!event){detail.className='canvas-detail empty';detail.innerHTML='<div>选择一个事件节点<br>查看完整变动、上下文和可用操作</div>';return}const retry=['awaiting approval','needs human'].includes(event.status);detail.className='canvas-detail';detail.innerHTML=`<button type="button" class="detail-close" aria-label="关闭详情">×</button><div class="detail-label">${event.pr_number==null?'未关联 PR':'PR #'+esc(event.pr_number)}</div><h3>${esc(event.subject)}</h3><span class="pill ${event.status==='needs human'?'danger':event.status==='awaiting approval'?'warn':'good'}">${esc(status[event.status]||event.status)}</span><div class="detail-label">时间 / 来源</div><div class="detail-value">${esc(event.observed_at)} · ${esc(origin[event.origin]||event.origin)}</div><div class="detail-label">提交 / 分支</div><div class="detail-value">${esc(event.sha)}<br>${esc(event.ref||'未记录分支')}</div><div class="detail-label">Event ID</div><div class="detail-value">${esc(event.event_key)}</div>${event.caused_by?`<div class="detail-label">因果链</div><div class="detail-value">${esc(event.caused_by)}</div>`:''}<div class="detail-label">执行详情</div><div class="detail-value">${esc(event.detail||'暂无详情')}</div>${retry?`<div class="detail-actions"><button type="button" class="${event.status==='needs human'?'danger':'primary'}" data-canvas-event-action="${esc(event.event_key)}">${event.status==='needs human'?'修复后重试':'批准此事件'}</button></div>`:''}`;detail.querySelector('.detail-close').addEventListener('click',()=>{selectedEventKey=null;renderDetail(null);document.querySelectorAll('.canvas-node').forEach(node=>node.classList.remove('is-selected'))});const action=detail.querySelector('[data-canvas-event-action]');if(action)action.addEventListener('click',()=>approve(action.dataset.canvasEventAction))};
  const renderCanvas=data=>{const allEvents=data.events||[],allTracks=canvasTracks(allEvents),tracks=canvasFilter==='all'?allTracks:allTracks.filter(track=>track.key===canvasFilter),options=['<option value="all">全部轨道（'+allTracks.length+'）</option>'].concat(allTracks.map(track=>`<option value="${esc(track.key)}">${esc(track.title)}（${track.events.length} 个事件）</option>`));if(filter.innerHTML!==options.join(''))filter.innerHTML=options.join('');if(!allTracks.some(track=>track.key===canvasFilter)&&canvasFilter!=='all')canvasFilter='all';filter.value=canvasFilter;const width=Math.max(700,...tracks.map(track=>track.events.length*265+80)),height=Math.max(430,tracks.length*190+55);world.style.width=width+'px';world.style.height=height+'px';const positions=new Map;let nodes='';tracks.forEach((track,row)=>{const y=30+row*190;track.events.forEach((event,index)=>{const x=35+index*265;positions.set(event.event_key,{x,y});const toneClass=event.status==='needs human'?'danger':event.status==='awaiting approval'?'warn':'ok';nodes+=`<article class="canvas-node ${selectedEventKey===event.event_key?'is-selected':''}" style="left:${x}px;top:${y}px" data-canvas-node="${esc(event.event_key)}"><button type="button" class="canvas-node-button" aria-label="${esc(track.title+'：'+event.subject)}"><span class="canvas-node-top"><span class="canvas-node-pr">${esc(track.title)}</span><span class="canvas-node-time">${esc(event.observed_at.slice(11,16))}</span></span><span class="canvas-node-title">${esc(event.subject)}</span><span class="canvas-node-meta">${esc(origin[event.origin]||event.origin)} · ${esc(event.sha.slice(0,8))}</span><span class="canvas-node-status ${toneClass}">${esc(status[event.status]||event.status)}</span></button></article>`})});let lines='<svg class="canvas-edges" width="'+width+'" height="'+height+'" aria-hidden="true"><defs><marker id="canvas-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#8eafe9"></path></marker></defs>';tracks.forEach(track=>track.edges.forEach(([from,to])=>{const start=positions.get(from),end=positions.get(to);if(start&&end)lines+=`<line class="canvas-edge" marker-end="url(#canvas-arrow)" x1="${start.x+225}" y1="${start.y+59}" x2="${end.x+5}" y2="${end.y+59}"></line>`}));lines+='</svg>';world.innerHTML=tracks.length?lines+nodes:'<div class="canvas-empty"><div><strong>当前筛选条件下没有事件</strong>如需查看其它 PR，请更改上方筛选。</div></div>';world.querySelectorAll('[data-canvas-node]').forEach(node=>node.querySelector('button').addEventListener('click',()=>{selectedEventKey=node.dataset.canvasNode;const event=allEvents.find(item=>item.event_key===selectedEventKey);document.querySelectorAll('.canvas-node').forEach(item=>item.classList.toggle('is-selected',item===node));renderDetail(event)}));if(selectedEventKey){const selected=allEvents.find(event=>event.event_key===selectedEventKey);if(selected)renderDetail(selected);else{selectedEventKey=null;renderDetail(null)}}if(!view.fit)requestAnimationFrame(fitCanvas);else updateTransform()};
  viewport.addEventListener('pointerdown',event=>{if(event.target.closest('button'))return;view.dragging=true;view.moved=false;view.startX=event.clientX;view.startY=event.clientY;view.originX=view.x;view.originY=view.y;viewport.classList.add('is-dragging');viewport.setPointerCapture?.(event.pointerId)});viewport.addEventListener('pointermove',event=>{if(!view.dragging)return;const dx=event.clientX-view.startX,dy=event.clientY-view.startY;if(Math.abs(dx)+Math.abs(dy)>3)view.moved=true;view.x=view.originX+dx;view.y=view.originY+dy;updateTransform()});const stopDrag=event=>{if(!view.dragging)return;view.dragging=false;viewport.classList.remove('is-dragging');viewport.releasePointerCapture?.(event.pointerId)};viewport.addEventListener('pointerup',stopDrag);viewport.addEventListener('pointercancel',stopDrag);viewport.addEventListener('wheel',event=>{event.preventDefault();zoomCanvasAt(Math.exp(-Math.max(-120,Math.min(120,event.deltaY))*.0015),event.clientX,event.clientY)},{passive:false});document.querySelector('#canvas-zoom-in').addEventListener('click',()=>zoomCanvas(1.2));document.querySelector('#canvas-zoom-out').addEventListener('click',()=>zoomCanvas(.83));document.querySelector('#canvas-fit').addEventListener('click',fitCanvas);document.querySelector('#canvas-reset').addEventListener('click',()=>{view={scale:1,x:0,y:0,fit:false,dragging:false,moved:false,startX:0,startY:0,originX:0,originY:0};updateTransform();requestAnimationFrame(fitCanvas)});filter.addEventListener('change',event=>{canvasFilter=event.target.value;view.fit=false;if(latest)renderCanvas(latest)});
renderEvents=data=>{baseRenderEvents(data);renderRepository(data.repository);renderCanvas(data)};if(latest)renderEvents(latest);
})();
</script></html>"""

# Keep the control deck in normal document flow so it cannot cover the canvas.
HTML += """<style>
#control-deck{position:relative!important;top:auto!important;z-index:1!important}
.canvas-panel{scroll-margin-top:16px}
.task-document{margin-top:16px;padding-top:13px;border-top:1px solid #e5ebf3}
.task-document summary{cursor:pointer;color:#276ef1;font-weight:700}
.task-document .task-path,.task-document .task-summary{color:#68758b;font-size:12px;margin-top:5px}.task-document .task-complete-note{color:#168451;font-weight:700}
.task-progress{display:flex;align-items:center;gap:9px;margin-top:10px;color:#68758b;font-size:12px}.task-progress-bar{height:7px;flex:1;max-width:180px;border-radius:99px;background:#e9eef6;overflow:hidden}.task-progress-bar i{display:block;height:100%;border-radius:99px;background:#34b36b}.task-section{margin-top:15px}.task-section h4{margin:0 0 7px;color:#31415a;font-size:12px;font-weight:750}.task-list{display:grid;gap:7px;list-style:none;margin:0;padding:0}.task-item{display:grid;grid-template-columns:24px minmax(0,1fr) auto;gap:8px;align-items:start;padding:9px 10px;border:1px solid #e3e9f2;border-radius:12px;background:#fbfcff}.task-check{display:grid;place-items:center;width:22px;height:22px;border-radius:8px;background:#eef3fb;color:#8391a5;font-size:13px;font-weight:800}.task-item.done .task-check{background:#e6f7ed;color:#168451}.task-item.in-progress .task-check{background:#fff4dc;color:#a96600}.task-item.waiting .task-check{background:#fff4dc;color:#a96600}.task-item.blocked .task-check{background:#fff0f0;color:#c23838}.task-item.superseded .task-check{background:#f0f1f4;color:#68758b}.task-item-label{color:#31415a;font-size:13px;line-height:1.4}.task-item-state{color:#8a97aa;font-size:11px;white-space:nowrap;padding-top:3px}.task-other{margin-top:14px;padding:9px 10px;border:1px dashed #cfd9e8;border-radius:12px;color:#68758b;font-size:12px}.task-other summary{color:#68758b;font-weight:650}.task-other pre{margin:8px 0 0;max-height:160px}
.task-document pre{max-height:280px;overflow:auto;white-space:pre-wrap;word-break:break-word;background:#f7f9fd;border:1px solid #e2e8f1;border-radius:10px;padding:10px;color:#31415a;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.task-history-block,.task-current-block{margin-top:14px;padding:12px;border:1px solid #e3e9f2;border-radius:12px;background:#fff}.task-history-block{background:#f8fbff;border-color:#d5e3f7}.task-history-meta{color:#68758b;font-size:11px;line-height:1.45;margin-top:5px}.task-history-missing{color:#a96600;font-size:12px;line-height:1.5;margin-top:7px}
.binding-panel{border-top:1px solid #e6ebf3;padding-top:10px}.binding-panel h3{margin:0;color:#68758b;font-size:14px}.binding-note{color:#68758b;font-size:12px;margin:4px 0 9px}.binding-list{display:grid;gap:7px}.binding-row{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;padding:9px 10px;background:#f7f9fd;border:1px solid #e3e9f2;border-radius:11px;font-size:12px}.binding-row strong{color:#31415a}.binding-row small{display:block;color:#68758b;line-height:1.4}.binding-row button{padding:4px 7px;font-size:11px;white-space:nowrap}.binding-form{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:9px}.binding-form input{min-width:0;padding:7px 8px;border:1px solid #d0d9e7;border-radius:8px;font:inherit}.binding-form button{grid-column:span 1}.binding-token{margin-top:8px;padding:8px;background:#fffaf0;border:1px solid #f0d29a;border-radius:8px;word-break:break-all;color:#73510d;font:11px ui-monospace,SFMono-Regular,Menlo,monospace}@media(max-width:680px){.binding-form{grid-template-columns:1fr 1fr}.binding-form button{grid-column:span 2}}
.canvas-layout{align-items:start!important;grid-template-columns:minmax(0,1fr) 340px!important}.canvas-detail{position:sticky!important;top:16px;height:min(72vh,720px)!important;min-height:420px!important;max-height:calc(100vh - 32px)!important;overflow:auto!important;overscroll-behavior:contain!important;align-self:start!important;z-index:2}.canvas-detail.empty{height:min(72vh,720px)!important}@media(max-width:900px){.canvas-layout{grid-template-columns:1fr!important}.canvas-detail{position:relative!important;top:auto!important;height:420px!important;min-height:260px!important;max-height:420px!important;z-index:1!important}}
@media(max-width:900px){#control-deck{position:relative!important}.canvas-layout{grid-template-columns:1fr}.canvas-viewport{min-height:420px}}
</style><script>
(function(){
  const detail=document.querySelector('#canvas-detail');
  const taskOpenByEvent=new Map();
  const taskOpenState=eventKey=>{const saved=taskOpenByEvent.get(eventKey);if(saved&&typeof saved==='object')return saved;return {raw:saved===true,other:false}};
  const taskUrl=event=>'/api/task?pr='+encodeURIComponent(event.pr_number==null?'unassigned':event.pr_number);
  const historyUrl=event=>'/api/task/history?pr='+encodeURIComponent(event.pr_number==null?'unassigned':event.pr_number);
  const matchTaskHistory=(history,event)=>{if(!history?.ok)return null;const snapshots=history.snapshots||[],exact=snapshots.find(snapshot=>snapshot.commit_sha===event.sha);if(exact)return {...exact,match:'commit_sha'};const at=Date.parse(event.observed_at),prior=snapshots.filter(snapshot=>Date.parse(snapshot.committed_at)<=at);return prior.length?{...prior[prior.length-1],match:'event_time'}:null};
  const taskState={done:['✓','已完成'],todo:['','待处理'],'in-progress':['…','进行中'],waiting:['?','等待处理'],blocked:['!','需人工处理'],superseded:['–','已替代']};
  const renderTaskSections=(data,eventKey,scope='current',includeOther=true)=>{const open=taskOpenState(eventKey),sections=(data.sections||[]).map(section=>'<div class="task-section"><h4>'+esc(section.title)+'</h4><ul class="task-list">'+section.items.map(item=>{const state=taskState[item.state]||taskState.todo;return '<li class="task-item '+esc(item.state)+'"><span class="task-check" aria-hidden="true">'+state[0]+'</span><span class="task-item-label">'+esc(item.label)+'</span><span class="task-item-state">'+state[1]+'</span></li>'}).join('')+'</ul></div>').join('');const other=includeOther&&data.other_content?'<details id="task-document-other" class="task-other"'+(open.other?' open':'')+'><summary>其他内容（标题、说明和列表）</summary><pre>'+esc(data.other_content)+'</pre></details>':'';return sections||other?sections+other:'<div class="task-other">未解析到复选任务；可展开下方原始任务.md 核对。</div>'};
  const replacePanel=(panel,html)=>{const detailTop=detail.scrollTop,raw=document.querySelector('#task-document-raw'),rawTop=raw?.scrollTop||0;panel.innerHTML=html;detail.scrollTop=detailTop;const nextRaw=panel.querySelector('#task-document-raw');if(nextRaw)nextRaw.scrollTop=rawTop};
  const loadTask=()=>{
    const selected=document.querySelector('.canvas-node.is-selected'),key=selected?.dataset.canvasNode;
    const event=key&&typeof latest!=='undefined'&&latest?.events?.find(item=>item.event_key===key);
    if(!event||detail.querySelector('.task-document'))return;
    const panel=document.createElement('section');panel.className='task-document';replacePanel(panel,'<div class="detail-label">任务.md / 交接进度</div><div class="task-summary">只读读取中…</div>');detail.append(panel);
    fetch(taskUrl(event)).then(response=>response.json()).then(data=>{
      if(document.querySelector('.canvas-node.is-selected')?.dataset.canvasNode!==event.event_key)return;
      if(!data.ok){panel.innerHTML='<div class="detail-label">任务.md / 交接进度</div><div class="task-summary">'+esc(data.error||'任务.md 不可用')+'</div>';return}
      return fetch(historyUrl(event)).then(response=>response.json()).catch(()=>({ok:false})).then(history=>{
      if(document.querySelector('.canvas-node.is-selected')?.dataset.canvasNode!==event.event_key)return;
      const prefix=data.unassigned?'未关联 PR 的当前 handoff 任务':'PR #'+esc(data.pr_number)+' 的交接任务';
      const open=taskOpenState(event.event_key);
      const completed=data.sections?.flatMap(section=>section.items).filter(item=>item.state==='done').length||0,total=data.sections?.flatMap(section=>section.items).length||0,percent=total?Math.round(completed/total*100):0;
      const historical=matchTaskHistory(history,event);
      const historyBlock=historical?'<section class="task-history-block"><div class="detail-label">该事件时点的任务进度</div><div class="task-history-meta">匹配方式：'+(historical.match==='commit_sha'?'事件提交 SHA 精确匹配':'按事件时间匹配最近不晚于该事件的提交')+' · '+esc(historical.short_sha||historical.commit_sha?.slice(0,8)||'')+' · '+esc(historical.committed_at||'')+' · '+esc(historical.subject||'')+'</div><div class="task-summary">'+esc(historical.summary)+'</div>'+renderTaskSections(historical,event.event_key,'history',false)+'</section>':'<section class="task-history-block"><div class="detail-label">该事件时点的任务进度</div><div class="task-history-missing">无历史快照：无法将该事件与不晚于该事件的任务.md 提交可靠关联。</div></section>';
      const currentBlock='<section class="task-current-block"><div class="detail-label">当前最新任务进度</div><div class="task-path">'+prefix+' · '+esc(data.path)+'</div><div class="task-summary '+(data.all_complete?'task-complete-note':'')+'">'+esc(data.summary)+(data.truncated?'（内容已截断）':'')+'</div><div class="task-progress" aria-label="任务完成进度"><span class="task-progress-bar"><i style="width:'+percent+'%"></i></span><span>'+percent+'% 已完成</span></div>'+renderTaskSections(data,event.event_key,'current',true)+'</section>';
      replacePanel(panel,'<div class="detail-label">任务.md / 交接进度</div>'+historyBlock+currentBlock+'<details id="task-document-details"'+(open.raw?' open':'')+'><summary>'+ (open.raw?'收起原始任务.md':'查看原始任务.md（展开完整任务.md）') +'</summary><pre id="task-document-raw">'+esc(data.content)+'</pre></details>');
      const disclosure=panel.querySelector('#task-document-details'),otherDisclosure=panel.querySelector('#task-document-other'),summary=disclosure.querySelector('summary'),otherSummary=otherDisclosure?.querySelector('summary');
      const sync=()=>{summary.textContent=disclosure.open?'收起原始任务.md':'查看原始任务.md（展开完整任务.md）'};
      disclosure.addEventListener('toggle',()=>{const state=taskOpenState(event.event_key);state.raw=disclosure.open;taskOpenByEvent.set(event.event_key,state);sync()});sync();
      if(otherDisclosure){const syncOther=()=>{if(otherSummary)otherSummary.textContent=otherDisclosure.open?'收起其他内容（标题、说明和列表）':'其他内容（标题、说明和列表）'};otherDisclosure.addEventListener('toggle',()=>{const state=taskOpenState(event.event_key);state.other=otherDisclosure.open;taskOpenByEvent.set(event.event_key,state);syncOther()});syncOther()}
      });
    }).catch(()=>{replacePanel(panel,'<div class="detail-label">任务.md / 交接进度</div><div class="task-summary">任务.md 读取失败，请稍后重试。</div>')});
  };
  new MutationObserver(loadTask).observe(detail,{childList:true});loadTask();
})();
</script><script>
(function(){
  const deck=document.querySelector('#control-deck');if(!deck)return;
  const panel=document.createElement('section');panel.className='binding-panel';panel.innerHTML='<h3>安全配对绑定</h3><p class="binding-note">绑定只使用 ChatGPT 端明确提供的稳定对话 ID；标题不能用于路由。token 只在创建/认领响应中显示。</p><div class="binding-list" id="binding-list"><span class="binding-note">读取绑定状态中…</span></div><details class="binding-create"><summary>创建一次性配对邀请</summary><form class="binding-form" id="binding-form"><input name="repository" placeholder="repository" required><input name="branch" placeholder="branch" required><input name="pr_number" type="number" min="1" placeholder="PR #" required><input name="web_conversation_id" placeholder="Web conversation ID" required><input name="web_conversation_title" placeholder="对话标题（可选）"><input name="expires_seconds" type="number" min="60" max="3600" value="900"><button class="primary" type="submit">生成邀请</button></form><div id="binding-token" class="binding-token" hidden></div></details>';deck.append(panel);
  const list=panel.querySelector('#binding-list'),form=panel.querySelector('#binding-form'),tokenBox=panel.querySelector('#binding-token');
  const render=data=>{const bindings=data.bindings||[];list.innerHTML=bindings.length?bindings.map(item=>'<div class="binding-row"><div><strong>'+esc(item.status)+' · '+esc(item.binding_id)+'</strong><small>'+esc(item.repository)+' / '+esc(item.branch)+' / PR #'+esc(item.pr_number)+'<br>Route: '+esc(item.route_id||'待认领')+' · Web: '+esc(item.web_conversation_title||item.web_conversation_id)+'<br>Local: '+esc(item.local_conversation_id||'待认领')+'<br>过期：'+esc(item.expires_at)+'</small></div>'+(item.status==='active'||item.status==='claimed'?'<button type="button" data-refresh-binding="'+esc(item.binding_id)+'">刷新 Web 对话名称</button>':'')+'</div>').join(''):'<span class="binding-note">当前没有配对绑定。</span>';list.querySelectorAll('[data-refresh-binding]').forEach(button=>button.addEventListener('click',async()=>{const response=await fetch('/api/bindings/refresh-name/'+encodeURIComponent(button.dataset.refreshBinding),{method:'POST'}),result=await response.json();notice(result.error||'刷新失败')}))};
  const load=()=>fetch('/api/bindings').then(response=>response.json()).then(render).catch(()=>{list.innerHTML='<span class="binding-note">绑定状态读取失败。</span>'});
  form.repository.value=document.querySelector('.repo-name')?.textContent?.trim()||'';form.addEventListener('submit',async event=>{event.preventDefault();const values=Object.fromEntries(new FormData(form));values.pr_number=Number(values.pr_number);values.expires_seconds=Number(values.expires_seconds);const response=await fetch('/api/bindings/invite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}),result=await response.json();tokenBox.hidden=false;tokenBox.textContent=result.ok?'一次性邀请 token（仅本次显示）：'+result.token:'创建失败：'+(result.error||'unknown error');if(result.ok){form.reset();form.repository.value=document.querySelector('.repo-name')?.textContent?.trim()||'';load()}});load();
})();
</script><script>
// The link is generated only after the local invite API returns a real token (URL fragment).
(function(){const form=document.querySelector('#binding-form'),box=document.querySelector('#binding-token');if(!form||!box)return;form.addEventListener('submit',async event=>{event.preventDefault();event.stopImmediatePropagation();const values=Object.fromEntries(new FormData(form));values.pr_number=Number(values.pr_number);values.expires_seconds=Number(values.expires_seconds);try{const response=await fetch('/api/bindings/invite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}),result=await response.json();box.hidden=false;if(!result.ok){box.textContent='创建失败：'+(result.error||'unknown error');return}const payload=btoa(unescape(encodeURIComponent(JSON.stringify({binding_id:result.binding.binding_id,token:result.token,repository:values.repository,branch:values.branch,pr_number:values.pr_number,web_conversation_id:values.web_conversation_id,expires_at:result.binding.expires_at}))));const link=location.origin+'/pair#invite='+payload;box.innerHTML='一次性配对链接（仅本次显示）：<a target="_blank" rel="noreferrer" href="'+link+'">打开/复制链接</a><br><code>'+esc(link)+'</code><br><button type="button" id="copy-pair-link">复制配对链接</button><p>Local Agent 粘贴此链接后，按其中字段调用 claim，再用返回的 confirm token 调用 confirm。</p>';box.querySelector('#copy-pair-link').addEventListener('click',()=>navigator.clipboard?.writeText(link));form.reset();form.repository.value=document.querySelector('.repo-name')?.textContent?.trim()||''}catch(error){box.hidden=false;box.textContent='本机邀请接口不可用：请在 Dashboard 生成并重试；'+error.message}} ,true)})();
</script>"""


def handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, data: Any, content_type: str = "application/json"):
            body = json.dumps(data).encode() if content_type == "application/json" else data.encode()
            self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path in {"/", "/pair"}: return self.reply(200, HTML, "text/html; charset=utf-8")
            if self.path == "/api/approvals": return self.reply(200, {"requests": pending_approval_requests()})
            if self.path == "/api/status":
                data = service.store.snapshot(); data["last_error"] = service.last_error; data["browser"] = service.browser_status; data["auto_mode"] = service.auto_mode(); data["repository"] = service.repository_status(); return self.reply(200, data)
            parsed = urlparse(self.path)
            if parsed.path == "/api/bindings":
                return self.reply(200, {"ok": True, "repository": service.repository_status(), "bindings": service.store.list_bindings(str(service.config.get("repository", "")))})
            if parsed.path == "/api/task":
                raw_pr = parse_qs(parsed.query).get("pr", ["unassigned"])[0]
                if raw_pr == "unassigned":
                    return self.reply(200, service.task_document())
                if not re.fullmatch(r"[1-9][0-9]*", raw_pr):
                    return self.reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid PR number"})
                return self.reply(200, service.task_document(int(raw_pr)))
            if parsed.path == "/api/task/history":
                raw_pr = parse_qs(parsed.query).get("pr", ["unassigned"])[0]
                if raw_pr == "unassigned":
                    return self.reply(200, service.task_history())
                if not re.fullmatch(r"[1-9][0-9]*", raw_pr):
                    return self.reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid PR number"})
                return self.reply(200, service.task_history(int(raw_pr)))
            return self.reply(404, {"error": "not found"})
        def do_POST(self):
            if self.path == "/api/poll": return self.reply(200, service.poll_once())
            if self.path == "/api/browser/check": return self.reply(200, service.check_browser())
            if self.path.startswith("/api/bindings/"):
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except (json.JSONDecodeError, TypeError):
                    return self.reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid JSON"})
                if not isinstance(data, dict):
                    return self.reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "JSON object required"})
                suffix = self.path.removeprefix("/api/bindings/")
                if suffix == "invite":
                    result = service.create_binding(data)
                elif suffix == "claim":
                    result = service.claim_binding(data)
                elif suffix == "confirm":
                    result = service.confirm_binding(data)
                elif suffix == "revoke":
                    result = service.revoke_binding(data)
                elif suffix.startswith("refresh-name/"):
                    result = service.refresh_binding_name(unquote(suffix.removeprefix("refresh-name/")))
                else:
                    return self.reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown binding action"})
                status = HTTPStatus.OK if result.get("ok") else (HTTPStatus.CONFLICT if result.get("status") in {"conflict", "claimed", "active", "revoked", "expired"} else HTTPStatus.BAD_REQUEST)
                if result.get("error", "").startswith("云端元数据不可访问"):
                    status = HTTPStatus.SERVICE_UNAVAILABLE
                return self.reply(status, result)
            if self.path == "/api/mode":
                length = int(self.headers.get("Content-Length", "0")); data = json.loads(self.rfile.read(length))
                if not isinstance(data.get("auto"), bool):
                    return self.reply(400, {"error": "invalid auto mode"})
                return self.reply(200, service.set_auto_mode(data["auto"]))
            if self.path.startswith("/api/approvals/"):
                request_id = unquote(self.path.removeprefix("/api/approvals/"))
                length = int(self.headers.get("Content-Length", "0")); data = json.loads(self.rfile.read(length))
                if not resolve_approval_request(request_id, data.get("decision", "")):
                    return self.reply(HTTPStatus.CONFLICT, {"error": "approval request is missing or already resolved"})
                return self.reply(200, {"ok": True})
            if self.path.startswith("/api/approve/"):
                event_key = unquote(self.path.removeprefix("/api/approve/"))
                event = service.store.event(event_key)
                if not event or event["status"] not in {"awaiting approval", "needs human"}:
                    return self.reply(HTTPStatus.CONFLICT, {"error": "event cannot be approved or retried"})
                service.dispatch_event(event_key); return self.reply(200, {"ok": True})
            if self.path == "/api/settings":
                length = int(self.headers.get("Content-Length", "0")); data = json.loads(self.rfile.read(length))
                if data.get("key") not in {"enabled", "agent_to_chatgpt", "chatgpt_to_agent", "auto_submit", "approval_required"} or not isinstance(data.get("value"), bool):
                    return self.reply(400, {"error": "invalid setting"})
                service.store.set_setting(data["key"], data["value"]); return self.reply(200, {"ok": True})
            return self.reply(404, {"error": "not found"})
        def log_message(self, *_: Any): pass
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(); config = load_config(args.config); store = Store(args.db); service = Service(config, store)
    if args.once: print(json.dumps(service.poll_once())); return
    def loop():
        while True:
            service.poll_once(); service.drain_auto_mode(); service.check_browser(); time.sleep(max(3, int(config.get("poll_interval_seconds", 15))))
    threading.Thread(target=loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(service))
    print(f"Dashboard: http://127.0.0.1:{args.port}"); server.serve_forever()

if __name__ == "__main__": main()
