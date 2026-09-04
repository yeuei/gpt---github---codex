import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).with_name("trigger.py")
SPEC = importlib.util.spec_from_file_location("handoff_trigger", MODULE)
trigger = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = trigger
SPEC.loader.exec_module(trigger)


class TriggerTests(unittest.TestCase):
    def test_commit_trailers_are_case_insensitive(self):
        commit = trigger.Commit("a" * 40, "Coordination-Origin: AGENT\nCoordination-Event-Id: evt-1\nCoordination-Caused-By: parent", "update")
        self.assertEqual(commit.origin, "agent")
        self.assertEqual(commit.event_id, "evt-1")
        self.assertEqual(commit.caused_by, "parent")

    def test_store_deduplicates_and_defaults_to_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            store = trigger.Store(Path(directory) / "state.sqlite3")
            event = {"event_key": "evt-1", "sha": "a" * 40, "ref": "origin/feature", "pr_number": 7, "origin": "agent",
                     "caused_by": None, "subject": "test", "observed_at": trigger.now(), "status": "detected"}
            self.assertTrue(store.add_event(event))
            self.assertFalse(store.add_event(event))
            self.assertTrue(store.setting("approval_required"))
            store.finish("evt-1", "awaiting approval")
            self.assertEqual(store.event("evt-1")["status"], "awaiting approval")

    def test_auto_mode_couples_approval_and_submit_and_drains_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; repo.mkdir()
            config = {"handoff_repo": str(repo), "remote": "origin", "branch": "main", "repository": "owner/repo",
                      "chatgpt": {}, "agent": {}}
            store = trigger.Store(Path(directory) / "state.sqlite3")
            service = trigger.Service(config, store)
            event = {"event_key": "evt-pending", "sha": "a" * 40, "ref": "origin/feature", "pr_number": None,
                     "origin": "agent", "caused_by": None, "subject": "pending", "observed_at": trigger.now(),
                     "status": "awaiting approval"}
            self.assertTrue(store.add_event(event))
            calls = []
            service.dispatch_event = lambda event_key: calls.append(event_key)

            result = service.set_auto_mode(True)
            self.assertTrue(result["auto_mode"])
            self.assertEqual(result["drained_event_keys"], ["evt-pending"])
            self.assertEqual(calls, ["evt-pending"])
            self.assertFalse(store.setting("approval_required"))
            self.assertTrue(store.setting("auto_submit"))

            result = service.set_auto_mode(False)
            self.assertFalse(result["auto_mode"])
            self.assertTrue(store.setting("approval_required"))
            self.assertFalse(store.setting("auto_submit"))

    def test_pending_events_excludes_needs_human_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            store = trigger.Store(Path(directory) / "state.sqlite3")
            for key, status in (("await", "awaiting approval"), ("failed", "needs human")):
                store.add_event({"event_key": key, "sha": key[0] * 40, "ref": "origin/feature", "pr_number": None,
                                 "origin": "agent", "caused_by": None, "subject": key, "observed_at": trigger.now(),
                                 "status": status})
            self.assertEqual([event["event_key"] for event in store.pending_events()], ["await"])

    def test_wake_prompt_preserves_actual_event_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; repo.mkdir()
            config = {"handoff_repo": str(repo), "remote": "origin", "branch": "main", "repository": "owner/repo",
                      "chatgpt": {}, "agent": {}}
            service = trigger.Service(config, trigger.Store(Path(directory) / "state.sqlite3"))
            prompt = service.wake_prompt_for_event({"sha": "a" * 40, "subject": "update", "ref": "origin/feature", "pr_number": 3, "event_key": "event-real"})
            self.assertIn("Event-ID: event-real", prompt)
            self.assertIn("Branch: origin/feature", prompt)

    def test_unassigned_event_requests_real_pr_instead_of_inventing_one(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; repo.mkdir()
            config = {"handoff_repo": str(repo), "remote": "origin", "branch": "main", "repository": "owner/repo",
                      "chatgpt": {}, "agent": {}}
            service = trigger.Service(config, trigger.Store(Path(directory) / "state.sqlite3"))
            prompt = service.wake_prompt_for_event({"sha": "b" * 40, "subject": "bootstrap", "ref": "origin/new-work", "pr_number": None, "event_key": "bootstrap-1"})
            self.assertIn("PR: #unassigned", prompt)
            self.assertIn("创建真实 PR", prompt)

    def test_browser_dispatch_uses_native_input_and_verifies_draft(self):
        config = {"chatgpt": {"conversation_url": "https://chatgpt.com/c/test", "browser": "chrome", "profile": "Default"}}
        adapter = trigger.OpenBrowserUse(config)
        calls = []
        original = trigger.run

        def fake_run(command, cwd=None, timeout=30):
            calls.append(command)
            if command[:3] == ["open-browser-use", "call", "--session-id"]:
                return '{"result":[{"id":42,"url":"https://chatgpt.com/c/test"}]}'
            if command[:2] == ["open-browser-use", "claim-tab"]:
                return '{"error":{"message":"Tab 42 is already part of browser session %s"}}' % adapter.session_id
            if "--method" in command:
                method = command[command.index("--method") + 1]
                if method == "Runtime.evaluate":
                    params = command[command.index("--params") + 1]
                    if "draftLength" in params:
                        return '{"result":{"result":{"type":"object","value":{"draftLength":0}}}}'
                    if "execCommand" in params:
                        return '{"result":{"result":{"type":"object","value":{"inserted":true}}}}'
                    return '{"result":{"result":{"type":"object","value":{"length":12,"matches":true}}}}'
                if method == "Input.insertText":
                    return '{"result":{}}'
            return '{}'

        try:
            trigger.run = fake_run
            self.assertEqual(adapter.dispatch("handoff test", submit=False), "filled; verified; waiting for user submit")
        finally:
            trigger.run = original
        methods = [call[call.index("--method") + 1] for call in calls if call[:2] == ["open-browser-use", "cdp"]]
        self.assertEqual(methods, ["Runtime.evaluate", "Runtime.evaluate", "Runtime.evaluate"])
        self.assertTrue(any(call[:2] == ["open-browser-use", "finalize-tabs"] for call in calls))

    def test_browser_session_id_is_stable_for_same_target(self):
        config = {"chatgpt": {"conversation_url": "https://chatgpt.com/c/test", "browser": "chrome", "profile": "Default"}}
        first = trigger.OpenBrowserUse(config)
        second = trigger.OpenBrowserUse(config)
        self.assertEqual(first.session_id, second.session_id)
        changed = trigger.OpenBrowserUse({"chatgpt": {"conversation_url": "https://chatgpt.com/c/test", "browser": "chrome", "profile": "Other"}})
        self.assertNotEqual(first.session_id, changed.session_id)

    def test_browser_health_reports_connected_target(self):
        config = {"chatgpt": {"conversation_url": "https://chatgpt.com/c/test", "browser": "chrome", "profile": "Default"}}
        adapter = trigger.OpenBrowserUse(config)
        original = trigger.run

        def fake_run(command, cwd=None, timeout=30):
            if command[1:3] == ["profiles", "--connected"]:
                return '[{"browser":"chrome","directory":"Default","target":"chrome:Default"}]'
            if command[1] == "ping":
                return "pong"
            return "{}"

        try:
            trigger.run = fake_run
            result = adapter.check_connection()
        finally:
            trigger.run = original
        self.assertEqual(result["state"], "connected")
        self.assertEqual(result["target"], "chrome:Default")

    def test_browser_rpc_errors_are_not_treated_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "already part of browser session"):
            trigger.OpenBrowserUse._result(
                '{"error":{"code":-32000,"message":"Tab 42 is already part of browser session old"}}',
                "claim ChatGPT tab",
            )


if __name__ == "__main__":
    unittest.main()
