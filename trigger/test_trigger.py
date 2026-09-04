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
            event = {"event_key": "evt-1", "sha": "a" * 40, "pr_number": 7, "origin": "agent",
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
            prompt = service.wake_prompt_for_event({"sha": "a" * 40, "subject": "update", "pr_number": 3, "event_key": "event-real"})
            self.assertIn("Event-ID: event-real", prompt)


if __name__ == "__main__":
    unittest.main()
