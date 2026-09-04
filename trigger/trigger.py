#!/usr/bin/env python3
"""Local, deterministic GitHub <-> ChatGPT Web handoff trigger.

The browser adapter is deliberately a fixed Open Browser Use CLI workflow, not
an LLM agent.  It never reads cookies, passwords, or unrelated tabs.  Sending
is opt-in and disabled by default; the dashboard controls both directions.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.local.json"
DEFAULT_DB = ROOT / "state.sqlite3"
ORIGIN_RE = re.compile(r"^Coordination-Origin:\s*(agent|chatgpt)\s*$", re.M | re.I)
EVENT_RE = re.compile(r"^Coordination-Event-Id:\s*(\S+)\s*$", re.M | re.I)
CAUSE_RE = re.compile(r"^Coordination-Caused-By:\s*(\S+)\s*$", re.M | re.I)
PR_PATH_RE = re.compile(r"^coordination/PR-(\d+)/")


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

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            rows = self.db.execute("select * from events order by id desc limit 200").fetchall()
            settings = {r["key"]: json.loads(r["value"]) for r in self.db.execute("select * from settings")}
        events = [dict(row) for row in rows]
        prs: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            prs.setdefault(str(event["pr_number"] or "unassigned"), []).append(event)
        return {"settings": settings, "events": events, "prs": prs, "updated_at": now()}


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
        self.session_id = f"obu-trigger-{uuid.uuid4().hex[:12]}"
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
            self._rpc(["open-browser-use", "claim-tab", *common, "--tab-id", str(tab_id)], "claim ChatGPT tab")
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

    def set_auto_mode(self, enabled: bool) -> dict[str, Any]:
        """Set the explicit unattended mode and drain existing approvals.

        The two low-level settings remain available for diagnostics, but the
        user-facing mode is intentionally coupled: unattended mode means no
        per-event approval and an actual ChatGPT Web submit. Turning it off
        restores the safe fill-only approval workflow.
        """
        self.store.set_setting("approval_required", not enabled)
        self.store.set_setting("auto_submit", enabled)
        dispatched = []
        if enabled:
            for event in self.store.pending_events():
                self.dispatch_event(event["event_key"])
                dispatched.append(event["event_key"])
        return {"ok": True, "auto_mode": enabled, "drained_event_keys": dispatched}

    def wake_prompt(self, commit: Commit, pr: int | None, event_id: str | None = None, ref: str = "") -> str:
        follow_up = ("本事件尚未关联 PR。请检查上述分支是否只包含一个可关闭目标；若是且你拥有写权限，"
                     "创建真实 PR，并只在获得真实编号后从 TEMPLATE 实例化 coordination/PR-<N>/。"
                     "不得虚构 PR 编号。" if pr is None else
                     "请按 Coordinator 协议重新读取该 PR 的当前 HEAD、任务.md 和 agent汇报.md。")
        return ("GitHub 协作事件已到达。\n\n"
                f"Repository: {self.config['repository']}\nBranch: {ref or 'unknown'}\nPR: #{pr if pr else 'unassigned'}\n"
                f"Origin: agent\nHead: {commit.sha}\nEvent-ID: {event_id or commit.event_id}\n\n"
                f"{follow_up}\n本消息仅用于唤醒；不要依据本聊天中的旧状态猜测项目事实。")

    def wake_prompt_for_event(self, event: dict[str, Any]) -> str:
        return self.wake_prompt(Commit(event["sha"], "", event["subject"]), event["pr_number"], event["event_key"], event["ref"])

    def dispatch_agent(self, commit: Commit, pr: int | None) -> str:
        command = self.config["agent"].get("command", [])
        if not command:
            raise RuntimeError("agent.command is empty; configure a local command before enabling this route")
        prompt = (f"GitHub coordination event {commit.event_id}: ChatGPT updated PR #{pr or 'unknown'}. "
                  "Fetch the configured handoff repository, read its current README, task and chatgpt解惑.md, then continue only the current task.")
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

    def dispatch_event(self, event_key: str) -> None:
        event = self.store.event(event_key)
        if not event:
            raise RuntimeError("event not found")
        if event["status"] == "dispatched":
            return
        try:
            if event["origin"] == "agent":
                detail = self.browser.dispatch(self.wake_prompt_for_event(event), self.store.setting("auto_submit"))
            else:
                commit = Commit(event["sha"], "", event["subject"])
                detail = self.dispatch_agent(commit, event["pr_number"])
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


HTML = """<!doctype html><meta charset=utf-8><title>GitHub 协作触发器</title>
<style>body{font:14px system-ui;max-width:1100px;margin:30px auto;padding:0 18px;background:#111;color:#eee}button{padding:8px;margin:3px;border:1px solid #666;border-radius:6px;background:#222;color:#eee;cursor:pointer}button:hover{border-color:#74d99f}.mode{display:flex;align-items:center;gap:12px;padding:14px 16px;margin:14px 0;border:1px solid #555;border-radius:8px;background:#1b1b1b}.mode button{font-size:15px;font-weight:600}.mode .on{color:#74d99f;border-color:#74d99f}.mode .off{color:#ffc76d;border-color:#ffc76d}.mode small{color:#aaa}table{width:100%%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid #444;text-align:left;vertical-align:top}.ok{color:#74d99f}.warn{color:#ffc76d}.browser{display:flex;align-items:center;gap:12px;padding:12px 0}.browser .connected{color:#74d99f}.browser .disconnected{color:#ff8a8a}.browser .unknown{color:#ffc76d}.detail{color:#aaa}</style>
<h1>GitHub ↔ ChatGPT Web 协作触发器</h1><p>本机确定性服务。默认逐条审批；开启“自动审批模式”后才会自动发送。</p>
<div id=browser class=browser></div><div id=controls></div><h2>按 PR 查看交接时间线</h2><table><thead><tr><th>发现时间</th><th>PR</th><th>来源</th><th>提交</th><th>状态</th><th>详情与操作</th></tr></thead><tbody id=events></tbody></table>
<script>const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const label={enabled:'总开关',agent_to_chatgpt:'Agent → ChatGPT',chatgpt_to_agent:'ChatGPT → Agent'};const status={detected:'已发现', 'awaiting approval':'等待审批',dispatched:'已执行','needs human':'需要人工处理','skipped: paused':'已跳过：总开关暂停','skipped: agent_to_chatgpt disabled':'已跳过：Agent → ChatGPT 已关闭','skipped: chatgpt_to_agent disabled':'已跳过：ChatGPT → Agent 已关闭'};const origin={agent:'本地 Agent',chatgpt:'远程 ChatGPT'};async function set(k,v){await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k,value:v})});load()}async function setMode(v){let r=await fetch('/api/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auto:v})});let d=await r.json();if(!r.ok)alert(d.error||'切换模式失败');load()}async function poll(){await fetch('/api/poll',{method:'POST'});load()}async function approve(k){await fetch('/api/approve/'+encodeURIComponent(k),{method:'POST'});load()}async function checkBrowser(){let b=document.querySelector('#browser');b.innerHTML='浏览器连接：检测中…';let r=await fetch('/api/browser/check',{method:'POST'});let d=await r.json();renderBrowser(d)}function renderBrowser(b){document.querySelector('#browser').innerHTML=`<span class="${esc(b.state)}">● ${esc(b.label)}</span><span>${esc(b.target||'')}</span><span class=detail>${esc(b.detail||'')} ${b.checked_at?'（'+esc(b.checked_at)+'）':''}</span><button onclick="checkBrowser()">检测浏览器连接</button>`}async function load(){let d=await (await fetch('/api/status')).json(),s=d.settings;renderBrowser(d.browser);let c=document.querySelector('#controls');c.innerHTML=`<div class=mode><button class="${d.auto_mode?'on':'off'}" onclick="setMode(${!d.auto_mode})">${d.auto_mode?'✓ 自动审批模式：已开启（自动批准并发送）':'○ 自动审批模式：已关闭（逐条审批）'}</button><small>${d.auto_mode?'新事件和当前等待审批事件会自动处理。':'默认安全模式：每条事件都需要你点击批准；启用自动审批后才会自动发送。'}</small></div>`+['enabled','agent_to_chatgpt','chatgpt_to_agent'].map(k=>`<button onclick="set('${k}',${!s[k]})">${s[k]?'✓ 已开启':'○ 已关闭'}：${label[k]}</button>`).join('')+'<button onclick="poll()">立即检查 GitHub</button>';document.querySelector('#events').innerHTML=d.events.map(e=>{let retry=e.status==='awaiting approval'||e.status==='needs human';return `<tr><td>${esc(e.observed_at)}</td><td>${e.pr_number==null?'未关联':`#${esc(e.pr_number)}`}</td><td>${esc(origin[e.origin]||e.origin)}</td><td>${esc(e.sha.slice(0,8))}<br>${esc(e.subject)}</td><td class="${e.status==='dispatched'?'ok':'warn'}">${esc(status[e.status]||e.status)}</td><td>${esc(e.detail||'')}${retry?`<br><button onclick="approve('${encodeURIComponent(e.event_key)}')">${e.status==='needs human'?'修复后重试':'批准此事件'}</button>`:''}</td></tr>`}).join('')}load();setInterval(load,5000)</script>"""


def handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, data: Any, content_type: str = "application/json"):
            body = json.dumps(data).encode() if content_type == "application/json" else data.encode()
            self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path == "/": return self.reply(200, HTML, "text/html; charset=utf-8")
            if self.path == "/api/status":
                data = service.store.snapshot(); data["last_error"] = service.last_error; data["browser"] = service.browser_status; data["auto_mode"] = service.auto_mode(); return self.reply(200, data)
            return self.reply(404, {"error": "not found"})
        def do_POST(self):
            if self.path == "/api/poll": return self.reply(200, service.poll_once())
            if self.path == "/api/browser/check": return self.reply(200, service.check_browser())
            if self.path == "/api/mode":
                length = int(self.headers.get("Content-Length", "0")); data = json.loads(self.rfile.read(length))
                if not isinstance(data.get("auto"), bool):
                    return self.reply(400, {"error": "invalid auto mode"})
                return self.reply(200, service.set_auto_mode(data["auto"]))
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
            service.poll_once(); service.check_browser(); time.sleep(max(3, int(config.get("poll_interval_seconds", 15))))
    threading.Thread(target=loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(service))
    print(f"Dashboard: http://127.0.0.1:{args.port}"); server.serve_forever()

if __name__ == "__main__": main()
