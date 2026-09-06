from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_runtime", HERE / "trigger.py")
trigger = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(trigger)


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return p.stdout.strip()


def make_repo() -> Path:
    root = Path(tempfile.mkdtemp())
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# test\n")
    git(root, "add", "."); git(root, "commit", "-m", "initial")
    return root


def config(repo: Path) -> dict:
    return {
        "handoff_repo": str(repo), "remote": "origin", "watch_branches": "all",
        "repository": "yeuei/gpt---github---codex",
        "chatgpt": {"conversation_url": "https://chatgpt.com/c/test", "browser": "chrome", "profile": "Default"},
        "agent": {"command": []}, "binding": {"require_active": False},
    }


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.dbdir = tempfile.TemporaryDirectory()
        self.store = trigger.Store(Path(self.dbdir.name) / "state.sqlite3")
        self.service = trigger.Service(config(self.repo), self.store)

    def tearDown(self):
        try: self.store.db.close()
        except Exception: pass
        self.dbdir.cleanup()

    def commit_task(self, pr: int, content: str, message: str = "task") -> str:
        path = self.repo / "coordination" / f"PR-{pr}" / "任务.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(self.repo, "add", "."); git(self.repo, "commit", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_background_scan_never_fetches(self):
        original = trigger.run
        calls = []
        def spy(args, cwd=None, timeout=30):
            calls.append(args)
            if args[:2] == ["git", "fetch"]:
                raise AssertionError("scan_local must never fetch")
            return original(args, cwd, timeout)
        with mock.patch.object(trigger, "run", side_effect=spy):
            result = self.service.scan_local()
        self.assertTrue(result["ok"])
        self.assertFalse(any(c[:2] == ["git", "fetch"] for c in calls))

    def test_explicit_refresh_failure_preserves_event_state(self):
        self.store.add_event({"event_key":"e1","sha":"0"*40,"ref":"main","pr_number":1,"origin":"agent","caused_by":None,"subject":"x","observed_at":trigger.now(),"approval_state":"awaiting","delivery_state":"not_started","delivery_detail":"","status":"detected"})
        before = self.store.event("e1")
        real = trigger.run
        def fail_fetch(args, cwd=None, timeout=30):
            if args[:2] == ["git", "fetch"]:
                raise RuntimeError("network unavailable")
            return real(args, cwd, timeout)
        with mock.patch.object(trigger, "run", side_effect=fail_fetch):
            result = self.service.refresh_from_github()
        after = self.store.event("e1")
        self.assertFalse(result["ok"])
        self.assertTrue(result["local_state_preserved"])
        self.assertIn("network unavailable", result["error"])
        self.assertEqual(before["approval_state"], after["approval_state"])
        self.assertEqual(before["delivery_state"], after["delivery_state"])

    def test_task_snapshot_is_exact_commit_sha(self):
        first = self.commit_task(7, "# [ ] PR #7 总任务\n- [ ] T7.1 old\n", "old task")
        second = self.commit_task(7, "# [ ] PR #7 总任务\n- [x] T7.1 new\n", "new task")
        snap = self.service.task_snapshot(7, first)
        self.assertTrue(snap["ok"]); self.assertEqual(snap["commit_sha"], first)
        self.assertIn("old", snap["content"]); self.assertNotIn("new", snap["content"])
        self.assertNotEqual(first, second)

    def test_missing_historical_snapshot_never_falls_back_to_current(self):
        before = git(self.repo, "rev-parse", "HEAD")
        self.commit_task(3, "# [ ] PR #3 总任务\n- [x] T3.1 current\n")
        snap = self.service.task_snapshot(3, before)
        self.assertFalse(snap["ok"]); self.assertFalse(snap["available"])
        self.assertIn("历史任务快照不可用", snap["error"])
        self.assertNotIn("current", json.dumps(snap, ensure_ascii=False))

    def test_history_still_works_after_current_file_deleted(self):
        sha = self.commit_task(4, "# [ ] PR #4 总任务\n- [x] T4.1 historical\n")
        path = self.repo / "coordination" / "PR-4" / "任务.md"
        path.unlink(); git(self.repo, "add", "-A"); git(self.repo, "commit", "-m", "cleanup merged PR")
        self.assertFalse(self.service.current_task(4)["ok"])
        snap = self.service.task_snapshot(4, sha)
        self.assertTrue(snap["ok"]); self.assertIn("historical", snap["content"])

    def test_unassigned_or_non_sha_snapshot_is_unavailable(self):
        self.assertFalse(self.service.task_snapshot(None, "0"*40)["ok"])
        self.assertFalse(self.service.task_snapshot(1, "abc")["ok"])

    def test_three_state_domains_are_independent_and_legacy_not_guessed(self):
        self.store.add_event({"event_key":"new","sha":"1"*40,"ref":"main","pr_number":2,"origin":"agent","caused_by":None,"subject":"new","observed_at":trigger.now(),"approval_state":"awaiting","delivery_state":"not_started","delivery_detail":"","status":"awaiting approval"})
        with self.store.lock:
            self.store.db.execute("insert into events(event_key,sha,ref,pr_number,origin,subject,observed_at,status,detail) values(?,?,?,?,?,?,?,?,?)", ("legacy","2"*40,"main",2,"agent","legacy",trigger.now(),"dispatched","old text")); self.store.db.commit()
        snap = self.store.snapshot(); by = {e["event_key"]:e for e in snap["events"]}
        self.assertEqual(by["new"]["approval_state"], "awaiting")
        self.assertEqual(by["new"]["delivery_state"], "not_started")
        self.assertEqual(by["new"]["github_pr_state"], "unknown")
        self.assertEqual(by["legacy"]["approval_state"], "legacy_unknown")
        self.assertEqual(by["legacy"]["delivery_state"], "legacy_unknown")

    def test_binding_claim_confirm_is_single_use(self):
        invite = self.service.create_binding({"repository":"yeuei/gpt---github---codex","branch":"main","pr_number":8,"web_conversation_id":"web-1","expires_seconds":900})
        self.assertTrue(invite["ok"])
        claim = self.service.claim_binding({"binding_id":invite["binding"]["binding_id"],"token":invite["token"],"repository":"yeuei/gpt---github---codex","branch":"main","pr_number":8,"route_id":"route-1","local_agent_id":"agent-1","local_conversation_id":"local-1"})
        self.assertTrue(claim["ok"]); self.assertEqual(claim["status"], "claimed")
        again = self.service.claim_binding({"binding_id":invite["binding"]["binding_id"],"token":invite["token"],"repository":"yeuei/gpt---github---codex","branch":"main","pr_number":8,"route_id":"route-2","local_agent_id":"agent-2","local_conversation_id":"local-2"})
        self.assertFalse(again["ok"])
        confirmed = self.service.confirm_binding({"binding_id":invite["binding"]["binding_id"],"confirm_token":claim["confirm_token"],"repository":"yeuei/gpt---github---codex","branch":"main","pr_number":8,"route_id":"route-1","local_agent_id":"agent-1","local_conversation_id":"local-1"})
        self.assertTrue(confirmed["ok"]); self.assertEqual(confirmed["status"], "active")


class StaticContractTests(unittest.TestCase):
    def test_single_renderer_and_exact_snapshot_endpoint(self):
        runtime = (HERE / "trigger.py").read_text(encoding="utf-8")
        html = (HERE / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("HTML +=", runtime)
        self.assertNotIn("event_time", html)
        self.assertNotIn("/api/task/history", html)
        self.assertIn("/api/task/snapshot?pr=", html)
        self.assertIn("历史任务快照不可用", html)

    def test_refresh_is_explicit_and_background_loop_is_local(self):
        runtime = (HERE / "trigger.py").read_text(encoding="utf-8")
        self.assertIn("def refresh_from_github", runtime)
        self.assertIn("def scan_local", runtime)
        loop = runtime[runtime.index("def loop()") :]
        self.assertIn("service.scan_local()", loop)
        self.assertNotIn("service.refresh_from_github()", loop)

    def test_pair_link_uses_fragment(self):
        html = (HERE / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("/pair#${q}", html)
        self.assertIn("/api/bindings/invite", html)


if __name__ == "__main__":
    unittest.main()
