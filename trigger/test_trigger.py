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
            if "--method" in command:
                method = command[command.index("--method") + 1]
                if method == "Runtime.evaluate":
                    params = command[command.index("--params") + 1]
                    if "draftLength" in params:
                        return '{"result":{"result":{"type":"object","value":{"draftLength":0}}}}'
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
        self.assertEqual(methods, ["Runtime.evaluate", "Input.insertText", "Runtime.evaluate"])
        self.assertTrue(any(call[:2] == ["open-browser-use", "turn-ended"] for call in calls))

    def test_browser_rpc_errors_are_not_treated_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "already part of browser session"):
            trigger.OpenBrowserUse._result(
                '{"error":{"code":-32000,"message":"Tab 42 is already part of browser session old"}}',
                "claim ChatGPT tab",
            )


if __name__ == "__main__":
    unittest.main()
