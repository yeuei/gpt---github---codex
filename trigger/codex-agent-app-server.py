#!/usr/bin/env python3
"""Run Codex through app-server and surface its approval requests locally.

``codex exec`` is intentionally non-interactive when launched by the trigger,
so its ``on-request`` policy cannot display the desktop approval dialog.  This
small JSON-RPC client keeps the policy while acting as the approval UI client.
The default approval UI is the local Trigger dashboard; a macOS dialog remains
an explicit fallback (`CODEX_APPROVAL_UI=native`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def send(stream: Any, message: dict[str, Any]) -> None:
    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    stream.flush()


def decision_from_available(params: dict[str, Any], choice: str) -> Any:
    """Return a protocol-valid decision, falling back safely when unavailable."""
    available = params.get("availableDecisions") or []
    strings = {item for item in available if isinstance(item, str)}
    amendment = params.get("proposedExecpolicyAmendment")
    if choice == "acceptForSession":
        choice = "session"
    if choice == "decline":
        if "decline" in strings or not available:
            return "decline"
        return "cancel" if "cancel" in strings else "decline"
    if choice == "accept":
        return "accept" if "accept" in strings or not available else ("acceptForSession" if "acceptForSession" in strings else "decline")
    if choice == "session":
        if "acceptForSession" in strings:
            return "acceptForSession"
        if amendment:
            return {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": amendment}}
        if "accept" in strings or not available:
            return "accept"
        return "decline"
    if "cancel" in strings:
        return "cancel"
    return "decline"


def ask_native(params: dict[str, Any]) -> str:
    """Show a native macOS approval dialog and return a user choice."""
    command = str(params.get("command") or "file change")
    reason = str(params.get("reason") or "Codex requests permission to continue.")
    message = f"{reason}\n\n命令/操作:\n{command}"
    script = '''on run argv
set promptText to item 1 of argv
display dialog promptText with title "Codex 需要批准" buttons {"拒绝", "允许这一次", "始终允许"} default button "允许这一次" cancel button "拒绝"
return button returned of result
end run'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script, "--", message],
            text=True,
            capture_output=True,
            timeout=3600,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "decline"
    if result.returncode != 0:
        return "decline"
    answer = result.stdout.strip()
    return {"允许这一次": "accept", "始终允许": "session", "拒绝": "decline"}.get(answer, "decline")


def ask_dashboard(request_id: str, method: str, params: dict[str, Any], repo: Path) -> str:
    """Publish a pending request for the local trigger dashboard and wait."""
    inbox = repo / "trigger" / "approval-requests"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{request_id}.json"
    payload = {"id": request_id, "method": method, "params": params,
               "created_at": time.time(), "decision": None}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    deadline = time.time() + 3600
    while time.time() < deadline:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("decision"):
                decision = str(current["decision"])
                path.unlink(missing_ok=True)
                return decision
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return "decline"
        time.sleep(0.25)
    path.unlink(missing_ok=True)
    return "decline"


def ask_user(request_id: str, method: str, params: dict[str, Any], repo: Path) -> Any:
    # Dashboard is the default because this process is not owned by Codex's
    # desktop UI. ``native`` remains an explicit macOS-only fallback.
    mode = os.environ.get("CODEX_APPROVAL_UI", "dashboard")
    if mode == "accept-once":
        return decision_from_available(params, "accept")
    if mode == "accept-session":
        return decision_from_available(params, "session")
    if mode in {"none", "deny", "decline"}:
        return decision_from_available(params, "decline")
    if mode == "native":
        return decision_from_available(params, ask_native(params))
    return decision_from_available(params, ask_dashboard(request_id, method, params, repo))


def main(argv: list[str]) -> int:
    if not argv:
        print("missing handoff prompt", file=sys.stderr)
        return 2
    repo = Path(__file__).resolve().parent.parent
    codex_bin = os.environ.get("CODEX_BIN") or "codex"
    process = subprocess.Popen(
        [codex_bin, "app-server", "--stdio"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin and process.stdout
    next_id = 1
    send(process.stdin, {"method": "initialize", "id": next_id, "params": {
        "clientInfo": {"name": "local-github-trigger", "title": "Local GitHub Trigger", "version": "1.0"}
    }})
    next_id += 1
    send(process.stdin, {"method": "initialized", "params": {}})
    send(process.stdin, {"method": "thread/start", "id": next_id, "params": {
        "cwd": str(repo), "approvalPolicy": "on-request", "sandbox": "workspace-write", "ephemeral": True
    }})
    thread_request_id = next_id
    next_id += 1
    thread_id: str | None = None
    turn_started = False
    try:
        for line in process.stdout:
            if not line.strip():
                continue
            message = json.loads(line)
            if message.get("id") == thread_request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                thread_id = message.get("result", {}).get("thread", {}).get("id")
                if not thread_id:
                    raise RuntimeError("app-server did not return a thread id")
                send(process.stdin, {"method": "turn/start", "id": next_id, "params": {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": " ".join(argv)}],
                    "approvalPolicy": "on-request",
                    "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False},
                }})
                next_id += 1
                turn_started = True
                continue
            method = message.get("method", "")
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
            } and "id" in message:
                params = message.get("params") or {}
                request_id = f"{os.getpid()}-{message['id']}"
                decision = ask_user(request_id, method, params, repo)
                send(process.stdin, {"id": message["id"], "result": {"decision": decision}})
                continue
            if "error" in message and turn_started:
                raise RuntimeError(str(message["error"]))
            if method == "turn/completed":
                status = (message.get("params") or {}).get("status")
                return 0 if status in {None, "completed"} else 1
    except (BrokenPipeError, json.JSONDecodeError) as error:
        print(f"app-server protocol error: {error}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        if process.stderr:
            diagnostics = process.stderr.read().strip()
            if diagnostics:
                print(diagnostics, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
