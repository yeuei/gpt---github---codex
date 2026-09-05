import importlib.util
import json
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

APP_SERVER = Path(__file__).with_name("codex-agent-app-server.py")
APP_SPEC = importlib.util.spec_from_file_location("codex_agent_app_server", APP_SERVER)
app_server = importlib.util.module_from_spec(APP_SPEC)
assert APP_SPEC.loader
sys.modules[APP_SPEC.name] = app_server
APP_SPEC.loader.exec_module(app_server)


class TriggerTests(unittest.TestCase):
    def test_app_server_approval_decisions_follow_available_options(self):
        params = {"availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
                  "proposedExecpolicyAmendment": ["touch", "/tmp/probe"]}
        self.assertEqual(app_server.decision_from_available(params, "accept"), "accept")
        self.assertEqual(app_server.decision_from_available(params, "session"), "acceptForSession")
        self.assertEqual(app_server.decision_from_available(params, "acceptForSession"), "acceptForSession")
        self.assertEqual(app_server.decision_from_available(params, "decline"), "decline")

    def test_app_server_session_falls_back_to_execpolicy_amendment(self):
        params = {"availableDecisions": ["accept", "decline"],
                  "proposedExecpolicyAmendment": ["npm", "test"]}
        self.assertEqual(
            app_server.decision_from_available(params, "session"),
            {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["npm", "test"]}},
        )

    def test_dashboard_approval_request_can_be_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "approval-requests"
            inbox.mkdir()
            path = inbox / "pid-1.json"
            path.write_text('{"id":"pid-1","method":"item/commandExecution/requestApproval","params":{"command":"echo test"},"decision":null}', encoding="utf-8")
            original = trigger.APPROVAL_DIR
            try:
                trigger.APPROVAL_DIR = inbox
                self.assertEqual([item["id"] for item in trigger.pending_approval_requests()], ["pid-1"])
                self.assertTrue(trigger.resolve_approval_request("pid-1", "accept"))
                self.assertEqual(trigger.pending_approval_requests(), [])
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["decision"], "accept")
            finally:
                trigger.APPROVAL_DIR = original

    def test_dashboard_keeps_raw_event_key_until_the_approval_request(self):
        """The event button must not URL-encode a key before ``approve`` does."""
        self.assertIn('data-event-key', trigger.HTML)
        self.assertIn('approve(button.dataset.eventKey)', trigger.HTML)
        self.assertNotIn("approve('${encodeURIComponent(e.event_key)}')", trigger.HTML)

    def test_repository_status_is_read_only_and_identifies_the_configured_repo(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; repo.mkdir()
            config = {"handoff_repo": str(repo), "remote": "origin", "watch_branches": "all",
                      "repository": "owner/repo", "chatgpt": {}, "agent": {}}
            service = trigger.Service(config, trigger.Store(Path(directory) / "state.sqlite3"))
            status = service.repository_status()
            self.assertEqual(status["name"], "owner/repo")
            self.assertEqual(status["local_path"], str(repo.resolve()))
            self.assertEqual(status["watch_branches"], "全部远端分支")
            self.assertFalse(status["can_switch_live"])

    def test_task_document_reads_only_the_allowed_handoff_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; (repo / "coordination/PR-7").mkdir(parents=True)
            task = repo / "coordination/PR-7/任务.md"
            task.write_text("# 任务\n- [x] 完成\n- [~] 进行中\n", encoding="utf-8")
            config = {"handoff_repo": str(repo), "remote": "origin", "watch_branches": "all",
                      "repository": "owner/repo", "chatgpt": {}, "agent": {}}
            service = trigger.Service(config, trigger.Store(Path(directory) / "state.sqlite3"))
            document = service.task_document(7)
            self.assertTrue(document["ok"])
            self.assertEqual(document["path"], "coordination/PR-7/任务.md")
            self.assertIn("进行中", document["content"])
            self.assertEqual(document["sections"][0]["items"][0]["state"], "done")
            self.assertEqual(document["sections"][0]["items"][1]["state"], "in-progress")
            self.assertEqual(service.task_document()["path"], document["path"])
            self.assertFalse(service.task_document(8)["ok"])

    def test_task_document_counts_real_completed_subtasks_without_counting_status_legend(self):
        config = {"handoff_repo": str(Path.cwd()), "remote": "origin", "watch_branches": "all",
                  "repository": "owner/repo", "chatgpt": {}, "agent": {}}
        service = trigger.Service(config, trigger.Store(Path(tempfile.mkdtemp()) / "state.sqlite3"))
        document = service.task_document(1)
        self.assertTrue(document["all_complete"])
        self.assertEqual(document["state_counts"]["done"], 6)
        self.assertEqual(sum(document["state_counts"].values()), 6)
        self.assertIn("真实任务文件当前全部完成", document["summary"])

    def test_task_history_is_dynamic_and_separates_historical_state_from_current(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; task_dir = repo / "coordination/PR-7"; task_dir.mkdir(parents=True)
            trigger.run(["git", "init", "-q"], repo)
            trigger.run(["git", "config", "user.email", "test@example.com"], repo)
            trigger.run(["git", "config", "user.name", "Test"], repo)
            task = task_dir / "任务.md"
            task.write_text("## 子任务\n- [ ] T7.1 初始\n", encoding="utf-8")
            trigger.run(["git", "add", "coordination/PR-7/任务.md"], repo)
            trigger.run(["git", "commit", "-qm", "start"], repo)
            first = trigger.run(["git", "rev-parse", "HEAD"], repo).strip()
            task.write_text("## 子任务\n- [x] T7.1 初始\n", encoding="utf-8")
            trigger.run(["git", "add", "coordination/PR-7/任务.md"], repo)
            trigger.run(["git", "commit", "-qm", "finish"], repo)
            second = trigger.run(["git", "rev-parse", "HEAD"], repo).strip()
            config = {"handoff_repo": str(repo), "remote": "origin", "watch_branches": "all",
                      "repository": "owner/repo", "chatgpt": {}, "agent": {}}
            service = trigger.Service(config, trigger.Store(Path(directory) / "state.sqlite3"))
            history = service.task_history(7)
            self.assertTrue(history["ok"])
            self.assertEqual(history["path"], "coordination/PR-7/任务.md")
            self.assertEqual(history["history_count"], 2)
            self.assertEqual(history["snapshots"][0]["commit_sha"], first)
            self.assertEqual(history["snapshots"][-1]["commit_sha"], second)
            self.assertFalse(history["snapshots"][0]["all_complete"])
            self.assertEqual(history["snapshots"][0]["state_counts"]["todo"], 1)
            self.assertTrue(history["snapshots"][-1]["all_complete"])
            self.assertTrue(history["current"]["all_complete"])

    def test_binding_state_machine_is_single_use_and_single_active_per_target(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; repo.mkdir()
            config = {"handoff_repo": str(repo), "remote": "origin", "repository": "owner/repo",
                      "chatgpt": {}, "agent": {}}
            store = trigger.Store(Path(directory) / "state.sqlite3")
            service = trigger.Service(config, store)
            invite = service.create_binding({"repository": "owner/repo", "branch": "main", "pr_number": 7,
                                             "web_conversation_id": "conv-7", "web_conversation_title": "Example chat"})
            self.assertTrue(invite["ok"])
            self.assertNotIn("token_hash", invite["binding"])
            claim = service.claim_binding({"binding_id": invite["binding"]["binding_id"], "token": invite["token"],
                                           "route_id": "route-7", "local_agent_id": "agent-7", "local_conversation_id": "local-7",
                                           "repository": "owner/repo", "branch": "main", "pr_number": 7})
            self.assertTrue(claim["ok"])
            repeated = service.claim_binding({"binding_id": invite["binding"]["binding_id"], "token": invite["token"],
                                               "route_id": "route-other", "local_agent_id": "agent-other", "local_conversation_id": "local-other",
                                               "repository": "owner/repo", "branch": "main", "pr_number": 7})
            self.assertFalse(repeated["ok"])
            active = service.confirm_binding({"binding_id": invite["binding"]["binding_id"], "confirm_token": claim["confirm_token"],
                                              "route_id": "route-7", "local_agent_id": "agent-7", "local_conversation_id": "local-7",
                                              "repository": "owner/repo", "branch": "main", "pr_number": 7})
            self.assertEqual(active["status"], "active")
            duplicate = service.create_binding({"repository": "owner/repo", "branch": "main", "pr_number": 7,
                                                 "web_conversation_id": "conv-other"})
            self.assertEqual(duplicate["status"], "conflict")
            self.assertEqual(service.active_binding_for_event({"ref": "origin/main", "pr_number": 7})["route_id"], "route-7")
            refresh = service.refresh_binding_name(invite["binding"]["binding_id"])
            self.assertFalse(refresh["ok"])
            self.assertIn("云端元数据不可访问", refresh["error"])
            revoked = service.revoke_binding({"binding_id": invite["binding"]["binding_id"], "token": invite["token"]})
            self.assertEqual(revoked["status"], "revoked")

    def test_dashboard_contains_repository_configuration_and_pr_timeline_surfaces(self):
        source = MODULE.read_text(encoding="utf-8")
        self.assertIn('当前交接仓库', trigger.HTML)
        self.assertIn('不提供运行中热切换', trigger.HTML)
        self.assertIn('PR 交接时间线', trigger.HTML)
        self.assertIn('color-scheme:light', trigger.HTML)
        self.assertIn('timeline-card:not(:last-child)::after', trigger.HTML)
        self.assertIn('aria-expanded', trigger.HTML)
        self.assertIn('baseRenderTimeline', trigger.HTML)
        self.assertIn('timelineCardKey', trigger.HTML)
        self.assertIn('expanded.has(timelineCardKey(card))', trigger.HTML)
        self.assertIn('prefers-reduced-motion', trigger.HTML)
        self.assertIn('canvas-viewport', trigger.HTML)
        self.assertIn('canvas-pr-filter', trigger.HTML)
        self.assertIn('canvas-edge', trigger.HTML)
        self.assertIn('canvas-detail', trigger.HTML)
        self.assertIn('pointerdown', trigger.HTML)
        self.assertIn('canvas-fit', trigger.HTML)
        self.assertIn("legacyEvents.style.display='none'", trigger.HTML)
        self.assertIn('selectedEventKey', trigger.HTML)
        self.assertIn('zoomCanvas', trigger.HTML)
        self.assertIn('zoomCanvasAt', trigger.HTML)
        self.assertIn('getBoundingClientRect', trigger.HTML)
        self.assertIn('event.clientX', trigger.HTML)
        self.assertIn('canvas-reset', trigger.HTML)
        self.assertIn('canvasTracks', trigger.HTML)
        self.assertIn('unassigned:chain:', trigger.HTML)
        self.assertIn('caused_by===key', trigger.HTML)
        self.assertIn('track.edges', trigger.HTML)
        self.assertIn('全部轨道', trigger.HTML)
        self.assertIn('/api/task', trigger.HTML)
        self.assertIn('任务.md / 交接进度', trigger.HTML)
        self.assertIn('展开完整任务.md', trigger.HTML)
        self.assertIn('taskOpenByEvent', trigger.HTML)
        self.assertIn('taskOpenState', trigger.HTML)
        self.assertIn('task-document-details', trigger.HTML)
        self.assertIn('task-document-other', trigger.HTML)
        self.assertIn('disclosure.addEventListener', trigger.HTML)
        self.assertIn('otherDisclosure.addEventListener', trigger.HTML)
        self.assertIn('state.raw', trigger.HTML)
        self.assertIn('state.other', trigger.HTML)
        self.assertIn('atBottom', trigger.HTML)
        self.assertIn('pageMax', trigger.HTML)
        self.assertIn('收起原始任务.md', trigger.HTML)
        self.assertIn('task-item', trigger.HTML)
        self.assertIn('renderTaskSections', trigger.HTML)
        self.assertIn('查看原始任务.md', trigger.HTML)
        self.assertIn('captureScrollPosition', trigger.HTML)
        self.assertIn('restoreScrollPosition', trigger.HTML)
        self.assertIn('window.scrollTo', trigger.HTML)
        self.assertIn('preserveScrollSelector', trigger.HTML)
        self.assertIn('task-document-raw', trigger.HTML)
        self.assertIn('task-complete-note', trigger.HTML)
        self.assertIn('all_complete', trigger.HTML)
        self.assertIn('task_history', source)
        self.assertIn('/api/task/history', source)
        self.assertIn('matchTaskHistory', trigger.HTML)
        self.assertIn('commit_sha', trigger.HTML)
        self.assertIn('event_time', trigger.HTML)
        self.assertIn('无历史快照', trigger.HTML)
        self.assertIn('该事件时点的任务进度', trigger.HTML)
        self.assertIn('当前最新任务进度', trigger.HTML)
        self.assertIn('安全配对绑定', trigger.HTML)
        self.assertIn('/api/bindings/invite', source)
        self.assertIn('suffix == "claim"', source)
        self.assertIn('suffix == "confirm"', source)
        self.assertIn('suffix == "revoke"', source)
        self.assertIn('/api/bindings/refresh-name/', source)
        self.assertIn('active_binding_for_event', source)
        self.assertIn('require_active', source)
        self.assertIn('statusFingerprint', trigger.HTML)
        self.assertIn('changed=fingerprint!==lastStatusFingerprint', trigger.HTML)
        self.assertIn('browser?.state', trigger.HTML)
        self.assertNotIn('browser:data.browser,repository', trigger.HTML)
        self.assertIn('position:sticky', trigger.HTML)
        self.assertIn('overscroll-behavior:contain', trigger.HTML)
        self.assertIn('height:min(72vh,720px)', trigger.HTML)
        self.assertIn('max-width:900px', trigger.HTML)
        self.assertIn('lastUserScrollAt', trigger.HTML)
        self.assertIn('restoringScroll', trigger.HTML)
        self.assertIn('detailTop=detail.scrollTop', trigger.HTML)

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

    def test_auto_mode_submits_verified_fill_only_draft_once(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"; repo.mkdir()
            config = {"handoff_repo": str(repo), "remote": "origin", "branch": "main", "repository": "owner/repo",
                      "chatgpt": {}, "agent": {}}
            store = trigger.Store(Path(directory) / "state.sqlite3")
            store.add_event({"event_key": "evt-filled", "sha": "b" * 40, "ref": "origin/feature", "pr_number": None,
                             "origin": "agent", "caused_by": None, "subject": "filled", "observed_at": trigger.now(),
                             "status": "dispatched"})
            store.finish("evt-filled", "dispatched", "filled; verified; waiting for user submit")
            service = trigger.Service(config, store)
            calls = []
            service.dispatch_event = lambda event_key, allow_fill_only_resubmit=False: calls.append((event_key, allow_fill_only_resubmit))
            result = service.set_auto_mode(True)
            self.assertEqual(result["drained_event_keys"], ["evt-filled"])
            self.assertEqual(calls, [("evt-filled", True)])

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
