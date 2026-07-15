import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "session_rollover.py"
SPEC = importlib.util.spec_from_file_location("session_rollover", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RolloverTests(unittest.TestCase):
    def make_manager(self, root: Path):
        config = {
            "enabled": True,
            "sessionsStorePath": str(root / "sessions.json"),
            "routerStatePath": str(root / "router.json"),
            "workflowTaskDir": str(root / "tasks"),
            "stateRoot": str(root / "state"),
            "openclawBin": "/bin/false",
            "nmemBin": "/bin/false",
            "sessionPreferences": {
                "thinkingLevel": "xhigh",
                "fastMode": False,
            },
            "thresholds": {
                "checkpointTokens": 210000,
                "rolloverTokens": 260000,
                "emergencyTokens": 320000,
                "checkpointTranscriptBytes": 1000,
                "rolloverTranscriptBytes": 2000,
                "emergencyTranscriptBytes": 3000,
                "maxPhysicalAgeDays": 45,
                "minIdleSeconds": 120,
                "recentMessageCount": 12,
                "recentMessageChars": 30000,
                "continuityContextChars": 16000,
            },
            "sessions": {"agent:main:project-test": {"label": "测试", "project": "test", "query": "test"}},
        }
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return MODULE.RolloverManager(path)

    def test_jsonl_state_is_owner_only(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "events.jsonl"
            MODULE.append_jsonl(path, {"event": "test"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_lock_state_is_owner_only(self):
        with tempfile.TemporaryDirectory() as value:
            manager = self.make_manager(Path(value))
            with manager._lock() as acquired:
                self.assertTrue(acquired)
            self.assertEqual(manager.state_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(manager.lock_path.stat().st_mode & 0o777, 0o600)

    def test_thresholds(self):
        with tempfile.TemporaryDirectory() as value:
            manager = self.make_manager(Path(value))
            self.assertEqual(manager._action(209999), "healthy")
            self.assertEqual(manager._action(210000), "checkpoint")
            self.assertEqual(manager._action(260000), "rollover")
            self.assertEqual(manager._action(320000), "emergency")

    def test_transcript_size_can_trigger_checkpoint_rollover_and_emergency(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            transcript = root / "session.jsonl"
            entry = {"sessionFile": str(transcript)}
            transcript.write_bytes(b"x" * 1000)
            self.assertEqual(manager._decision(1, entry)["reason"], "checkpoint_transcript_limit")
            transcript.write_bytes(b"x" * 2000)
            self.assertEqual(manager._decision(1, entry)["reason"], "rollover_transcript_limit")
            transcript.write_bytes(b"x" * 3000)
            decision = manager._decision(1, entry)
            self.assertEqual(decision["reason"], "emergency_transcript_limit")
            self.assertEqual(decision["transcriptBytes"], 3000)

    def test_session_thresholds_override_global_context_policy(self):
        with tempfile.TemporaryDirectory() as value:
            manager = self.make_manager(Path(value))
            spec = {"thresholds": {
                "checkpointTokens": 260000,
                "rolloverTokens": 320000,
                "emergencyTokens": 345000,
            }}
            self.assertEqual(manager._action(260000, spec=spec), "checkpoint")
            self.assertEqual(manager._action(320000, spec=spec), "rollover")
            self.assertEqual(manager._action(345000, spec=spec), "emergency")

    def test_monthly_age_rollover(self):
        with tempfile.TemporaryDirectory() as value:
            manager = self.make_manager(Path(value))
            now_ms = 1_800_000_000_000
            entry = {"sessionStartedAt": now_ms - (45 * 86_400_000)}
            with patch.object(MODULE.time, "time", return_value=now_ms / 1000):
                decision = manager._decision(1000, entry)
            self.assertEqual(decision["action"], "rollover")
            self.assertEqual(decision["reason"], "max_physical_age")

    def test_age_does_not_override_emergency_tokens(self):
        with tempfile.TemporaryDirectory() as value:
            manager = self.make_manager(Path(value))
            decision = manager._decision(320000, {"sessionStartedAt": 1})
            self.assertEqual(decision["action"], "emergency")
            self.assertEqual(decision["reason"], "emergency_token_limit")

    def test_running_session_never_rolls(self):
        with tempfile.TemporaryDirectory() as value:
            manager = self.make_manager(Path(value))
            self.assertFalse(manager._is_idle({"status": "running", "updatedAt": 0}))

    def test_codex_binding_key_matches_openclaw(self):
        self.assertEqual(
            MODULE.codex_binding_store_key("agent:main:project-example"),
            "session-key:main:I5gbbCCp3yEkzqR7QUG4Zz5gE5HiQ_zB749GjX42uqo",
        )

    def test_scan_detects_retired_binding_for_current_session(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            database = root / "openclaw.sqlite"
            manager.config["codexStateDbPath"] = str(database)
            binding_key = MODULE.codex_binding_store_key("agent:main:project-test")
            with MODULE.sqlite3.connect(database) as connection:
                connection.execute("""
                    CREATE TABLE plugin_state_entries (
                      plugin_id TEXT NOT NULL,
                      namespace TEXT NOT NULL,
                      entry_key TEXT NOT NULL,
                      value_json TEXT NOT NULL,
                      created_at INTEGER NOT NULL,
                      expires_at INTEGER,
                      PRIMARY KEY (plugin_id, namespace, entry_key)
                    )
                """)
                connection.execute(
                    "INSERT INTO plugin_state_entries VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        "codex",
                        "app-server-thread-bindings",
                        binding_key,
                        json.dumps({
                            "version": 1,
                            "state": "cleared",
                            "retired": True,
                            "sessionId": "current-session",
                        }),
                        123456,
                    ),
                )
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "current-session",
                    "totalTokens": 100,
                    "status": "done",
                    "updatedAt": 0,
                    "thinkingLevel": "xhigh",
                    "fastMode": False,
                }
            }), encoding="utf-8")
            payload = manager.scan(dry_run=True)
            self.assertEqual(payload["results"][0]["action"], "would_recover_retired_binding")
            self.assertEqual(payload["results"][0]["bindingCreatedAtMs"], 123456)

    def test_retired_binding_for_old_generation_is_ignored(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            database = root / "openclaw.sqlite"
            manager.config["codexStateDbPath"] = str(database)
            binding_key = MODULE.codex_binding_store_key("agent:main:project-test")
            with MODULE.sqlite3.connect(database) as connection:
                connection.execute("""
                    CREATE TABLE plugin_state_entries (
                      plugin_id TEXT NOT NULL,
                      namespace TEXT NOT NULL,
                      entry_key TEXT NOT NULL,
                      value_json TEXT NOT NULL,
                      created_at INTEGER NOT NULL,
                      expires_at INTEGER,
                      PRIMARY KEY (plugin_id, namespace, entry_key)
                    )
                """)
                connection.execute(
                    "INSERT INTO plugin_state_entries VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        "codex",
                        "app-server-thread-bindings",
                        binding_key,
                        json.dumps({
                            "version": 1,
                            "state": "cleared",
                            "retired": True,
                            "sessionId": "old-session",
                        }),
                        123456,
                    ),
                )
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "current-session",
                    "totalTokens": 100,
                    "status": "done",
                    "updatedAt": 0,
                    "thinkingLevel": "xhigh",
                    "fastMode": False,
                }
            }), encoding="utf-8")
            payload = manager.scan(dry_run=True)
            self.assertEqual(payload["results"][0]["action"], "healthy")

    def test_live_scan_forces_safe_rollover_for_retired_binding(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            database = root / "openclaw.sqlite"
            manager.config["codexStateDbPath"] = str(database)
            binding_key = MODULE.codex_binding_store_key("agent:main:project-test")
            with MODULE.sqlite3.connect(database) as connection:
                connection.execute("""
                    CREATE TABLE plugin_state_entries (
                      plugin_id TEXT NOT NULL,
                      namespace TEXT NOT NULL,
                      entry_key TEXT NOT NULL,
                      value_json TEXT NOT NULL,
                      created_at INTEGER NOT NULL,
                      expires_at INTEGER,
                      PRIMARY KEY (plugin_id, namespace, entry_key)
                    )
                """)
                connection.execute(
                    "INSERT INTO plugin_state_entries VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        "codex",
                        "app-server-thread-bindings",
                        binding_key,
                        json.dumps({
                            "version": 1,
                            "state": "cleared",
                            "retired": True,
                            "sessionId": "current-session",
                        }),
                        123456,
                    ),
                )
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "current-session",
                    "totalTokens": 100,
                    "status": "done",
                    "updatedAt": 0,
                    "thinkingLevel": "xhigh",
                    "fastMode": False,
                }
            }), encoding="utf-8")
            with patch.object(
                manager,
                "_rollover_unlocked",
                return_value={"event": "rollover_completed", "sessionKey": "agent:main:project-test"},
            ) as rollover:
                payload = manager.scan()
            rollover.assert_called_once_with(
                "agent:main:project-test",
                force=True,
                trigger_override="codex_binding_generation_retired",
            )
            self.assertEqual(payload["results"][0]["event"], "rollover_completed")
            self.assertEqual(payload["results"][0]["recoveredBinding"], binding_key)

    def test_recent_messages_exclude_tools(self):
        with tempfile.TemporaryDirectory() as value:
            path = Path(value) / "session.jsonl"
            events = [
                {"type": "message", "message": {"role": "user", "content": "目标"}},
                {"type": "message", "message": {"role": "toolResult", "content": "噪音"}},
                {"type": "message", "message": {"role": "assistant", "content": "结果"}},
            ]
            path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8")
            messages = MODULE.recent_visible_messages(path, 10, 1000)
            self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
            self.assertNotIn("噪音", json.dumps(messages, ensure_ascii=False))

    def test_visible_notice_is_bounded_and_does_not_replay_recent_history(self):
        handoff = {
            "label": "测试项目",
            "sessionKey": "agent:main:project-test",
            "oldSessionId": "old-session",
            "handoffPath": "/tmp/handoff.json",
            "recentMessages": [{"role": "user", "text": "secret-value"}],
        }
        notice = MODULE.render_visible_continuity_notice(handoff, "new-session")
        self.assertIn("会话已安全换代", notice)
        self.assertIn("old-session", notice)
        self.assertIn("new-session", notice)
        self.assertIn("/tmp/handoff.json", notice)
        self.assertIn(MODULE.continuity_notice_marker("old-session", "new-session"), notice)
        self.assertNotIn("secret-value", notice)

    def test_visible_notice_uses_chat_inject_and_reads_marker_back(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            manager.config["visibleContinuity"] = {
                "enabled": True,
                "label": "会话换代",
                "historyCheckLimit": 200,
            }
            entry = {
                "sessionId": "new-session",
                "status": "done",
                "updatedAt": 0,
                "thinkingLevel": "xhigh",
                "fastMode": False,
            }
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": entry,
            }), encoding="utf-8")
            handoff_path = root / "handoff.json"
            handoff = {
                "label": "测试",
                "sessionKey": "agent:main:project-test",
                "oldSessionId": "old-session",
                "handoffPath": str(handoff_path),
            }
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            record = {
                "oldSessionId": "old-session",
                "handoffPath": str(handoff_path),
                "handoffSha256": MODULE.sha256_file(handoff_path),
            }
            marker = MODULE.continuity_notice_marker("old-session", "new-session")
            calls = []

            def gateway_call(method, params, require_ok=True):
                calls.append((method, params, require_ok))
                if method == "chat.inject":
                    self.assertIn(marker, params["message"])
                    return {"ok": True, "messageId": "notice-message"}
                history_calls = sum(1 for call in calls if call[0] == "chat.history")
                return {"messages": [] if history_calls == 1 else [{
                    "role": "assistant",
                    "content": [{"type": "text", "text": marker}],
                }]}

            with patch.object(manager, "_gateway_call", side_effect=gateway_call):
                result = manager._ensure_visible_continuity(
                    "agent:main:project-test", record, "new-session"
                )
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["verification"], "injected_and_read_back")
            self.assertEqual(result["messageId"], "notice-message")
            self.assertEqual([call[0] for call in calls], [
                "chat.history", "chat.inject", "chat.history"
            ])

    def test_visible_notice_retry_is_idempotent_when_marker_exists(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            manager.config["visibleContinuity"] = {"enabled": True}
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "new-session",
                    "status": "done",
                    "updatedAt": 0,
                }
            }), encoding="utf-8")
            handoff_path = root / "handoff.json"
            handoff = {
                "label": "测试",
                "sessionKey": "agent:main:project-test",
                "oldSessionId": "old-session",
                "handoffPath": str(handoff_path),
            }
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            record = {
                "oldSessionId": "old-session",
                "handoffPath": str(handoff_path),
                "handoffSha256": MODULE.sha256_file(handoff_path),
            }
            marker = MODULE.continuity_notice_marker("old-session", "new-session")
            with patch.object(manager, "_gateway_call", return_value={
                "messages": [{"role": "assistant", "content": marker}],
            }) as gateway:
                result = manager._ensure_visible_continuity(
                    "agent:main:project-test", record, "new-session"
                )
            self.assertEqual(result["verification"], "existing_notice")
            gateway.assert_called_once_with(
                "chat.history",
                {"sessionKey": "agent:main:project-test", "limit": 200},
                require_ok=False,
            )

    def test_visible_notice_does_not_inject_while_session_is_running(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            manager.config["visibleContinuity"] = {"enabled": True}
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "new-session",
                    "status": "running",
                    "updatedAt": 0,
                }
            }), encoding="utf-8")
            handoff_path = root / "handoff.json"
            handoff = {
                "label": "测试",
                "sessionKey": "agent:main:project-test",
                "oldSessionId": "old-session",
                "handoffPath": str(handoff_path),
            }
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            record = {
                "oldSessionId": "old-session",
                "handoffPath": str(handoff_path),
            }
            with patch.object(manager, "_gateway_call", return_value={"messages": []}) as gateway:
                result = manager._ensure_visible_continuity(
                    "agent:main:project-test", record, "new-session"
                )
            self.assertEqual(result["status"], "pending_busy")
            self.assertEqual(gateway.call_count, 1)

    def test_reconcile_keeps_prepared_state_when_visible_notice_fails(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            manager.config["visibleContinuity"] = {"enabled": True}
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "new-session",
                    "status": "done",
                    "updatedAt": 0,
                    "thinkingLevel": "xhigh",
                    "fastMode": False,
                }
            }), encoding="utf-8")
            handoff_path = root / "handoff.json"
            handoff = {
                "label": "测试",
                "sessionKey": "agent:main:project-test",
                "oldSessionId": "old-session",
                "handoffPath": str(handoff_path),
            }
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            manager._update_current("agent:main:project-test", {
                "status": "prepared",
                "oldSessionId": "old-session",
                "newSessionId": "new-session",
                "handoffPath": str(handoff_path),
                "handoffSha256": MODULE.sha256_file(handoff_path),
            })
            with patch.object(manager, "_gateway_call", side_effect=RuntimeError("gateway down")):
                recovered = manager._reconcile_prepared(manager._sessions())
            self.assertEqual(recovered[0]["event"], "visibility_notice_retry_failed")
            current = MODULE.read_json(manager.current_path, {})
            record = current["sessions"]["agent:main:project-test"]
            self.assertEqual(record["status"], "prepared")
            self.assertEqual(record["visibilityNotice"]["status"], "pending_retry")

    def test_scan_auto_repairs_allowlisted_existing_rollover_once(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            manager.config["visibleContinuity"] = {
                "enabled": True,
                "repairExistingSessionKeys": ["agent:main:project-test"],
            }
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "new-session",
                    "totalTokens": 100,
                    "status": "done",
                    "updatedAt": 0,
                    "thinkingLevel": "xhigh",
                    "fastMode": False,
                }
            }), encoding="utf-8")
            manager._update_current("agent:main:project-test", {
                "status": "active",
                "oldSessionId": "old-session",
                "newSessionId": "new-session",
                "handoffPath": "/tmp/handoff.json",
            })
            verified = {
                "status": "verified",
                "verification": "injected_and_read_back",
                "marker": "marker",
            }
            with patch.object(
                manager, "_ensure_visible_continuity", return_value=verified
            ) as ensure:
                payload = manager.scan()
            ensure.assert_called_once()
            self.assertEqual(payload["visibilityRepairs"][0]["status"], "verified")
            current = MODULE.read_json(manager.current_path, {})
            self.assertEqual(
                current["sessions"]["agent:main:project-test"]["visibilityNotice"],
                verified,
            )

            with patch.object(manager, "_ensure_visible_continuity") as ensure_again:
                payload = manager.scan()
            ensure_again.assert_not_called()
            self.assertEqual(payload["visibilityRepairs"][0]["status"], "already_verified")

    def test_manual_visibility_repair_live_path_returns_event(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            manager._update_current("agent:main:project-test", {
                "status": "active",
                "oldSessionId": "old-session",
                "newSessionId": "new-session",
                "handoffPath": "/tmp/handoff.json",
            })
            verified = {
                "status": "verified",
                "verification": "injected_and_read_back",
                "marker": "marker",
            }
            with patch.object(
                manager, "_ensure_visible_continuity", return_value=verified
            ) as ensure:
                payload = manager.repair_visibility("agent:main:project-test")
            ensure.assert_called_once()
            self.assertEqual(payload["event"], "visibility_notice_repaired")
            self.assertEqual(payload["visibilityNotice"], verified)
            current = MODULE.read_json(manager.current_path, {})
            self.assertEqual(
                current["sessions"]["agent:main:project-test"]["visibilityNotice"],
                verified,
            )

    def test_context_is_bounded(self):
        handoff = {
            "project": "test",
            "label": "测试",
            "sessionKey": "agent:main:project-test",
            "oldSessionId": "old",
            "handoffPath": "/tmp/handoff.json",
            "workflowTasks": [],
            "memories": [],
            "paths": [],
            "recentMessages": [{"role": "user", "text": "x" * 5000}],
        }
        context = MODULE.render_continuity_context(handoff, 1000)
        self.assertLessEqual(len(context), 1000)
        self.assertIn("handoff_file", context)

    def test_dry_run_checkpoint_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "message", "message": {"role": "user", "content": "测试"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            sessions = {
                "agent:main:project-test": {
                    "sessionId": "old",
                    "sessionFile": str(transcript),
                    "totalTokens": 220000,
                    "status": "done",
                    "updatedAt": 0,
                    "label": "测试",
                    "thinkingLevel": "xhigh",
                    "fastMode": False,
                }
            }
            (root / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
            payload = manager.scan(dry_run=True)
            self.assertEqual(payload["results"][0]["action"], "would_checkpoint")
            self.assertFalse((root / "state").exists())

    def test_prepared_reset_is_recovered_after_partial_commit(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "new-session",
                    "thinkingLevel": "xhigh",
                    "fastMode": False,
                }
            }), encoding="utf-8")
            manager._update_current("agent:main:project-test", {
                "status": "prepared",
                "oldSessionId": "old-session",
                "handoffPath": "/tmp/handoff.json",
            })
            recovered = manager._reconcile_prepared(manager._sessions())
            self.assertEqual(len(recovered), 1)
            current = MODULE.read_json(manager.current_path, {})
            record = current["sessions"]["agent:main:project-test"]
            self.assertEqual(record["status"], "active")
            self.assertEqual(record["newSessionId"], "new-session")
            self.assertEqual(record["verification"], "recovered_after_partial_commit")

    def test_rollover_rotates_session_and_arms_continuity(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "message", "message": {"role": "user", "content": "继续处理"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            old_entry = {
                "sessionId": "old-session",
                "sessionFile": str(transcript),
                "totalTokens": 270000,
                "status": "done",
                "updatedAt": 0,
                "label": "测试",
                "thinkingLevel": "xhigh",
                "fastMode": False,
            }
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": old_entry
            }), encoding="utf-8")
            handoff_path = root / "handoff.json"
            handoff_path.write_text("{}\n", encoding="utf-8")
            handoff = {
                "handoffPath": str(handoff_path),
                "continuityContext": "verified handoff",
            }

            def gateway_reset(_session_key):
                new_entry = {**old_entry, "sessionId": "new-session", "totalTokens": 0}
                (root / "sessions.json").write_text(json.dumps({
                    "agent:main:project-test": new_entry
                }), encoding="utf-8")
                return {"ok": True, "key": "agent:main:project-test", "entry": new_entry}

            with patch.object(manager, "_handoff", return_value=handoff), patch.object(
                manager, "_gateway_reset", side_effect=gateway_reset
            ):
                result = manager.rollover("agent:main:project-test")
            self.assertEqual(result["event"], "rollover_completed")
            current = MODULE.read_json(manager.current_path, {})
            record = current["sessions"]["agent:main:project-test"]
            self.assertEqual(record["status"], "active")
            self.assertEqual(record["newSessionId"], "new-session")
            self.assertEqual(record["maxInjections"], 3)
            self.assertEqual(record["sessionPreferences"], {
                "thinkingLevel": "xhigh",
                "fastMode": False,
            })

    def test_scan_dry_run_detects_preference_drift(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": {
                    "sessionId": "session",
                    "totalTokens": 100,
                    "status": "done",
                    "updatedAt": 0,
                }
            }), encoding="utf-8")
            payload = manager.scan(dry_run=True)
            self.assertEqual(payload["results"][0]["action"], "would_repair_preferences")

    def test_ensure_preferences_uses_gateway_patch_and_verifies(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            entry = {"sessionId": "session", "thinkingLevel": None, "fastMode": None}

            def gateway_call(method, params):
                self.assertEqual(method, "sessions.patch")
                self.assertEqual(params["thinkingLevel"], "xhigh")
                self.assertEqual(params["fastMode"], False)
                return {"ok": True, "entry": {**entry, **params}}

            with patch.object(manager, "_gateway_call", side_effect=gateway_call):
                repaired_entry, repaired = manager._ensure_preferences(
                    "agent:main:project-test",
                    manager.config["sessions"]["agent:main:project-test"],
                    entry,
                )
            self.assertTrue(repaired)
            self.assertEqual(repaired_entry["thinkingLevel"], "xhigh")
            self.assertEqual(repaired_entry["fastMode"], False)

    def test_session_specific_preferences_override_global_defaults(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            spec = {
                "label": "小说敏感内容",
                "project": "novel",
                "query": "novel sensitive",
                "preferences": {"thinkingLevel": "low", "fastMode": False},
            }
            self.assertEqual(manager._desired_preferences(spec), {
                "thinkingLevel": "low",
                "fastMode": False,
            })

    def test_manual_model_override_suspends_thinking_repair_but_keeps_fast_disabled(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            spec = {
                "label": "可手动切换项目",
                "project": "manual",
                "query": "manual",
                "allowManualModelOverride": True,
                "preferences": {"thinkingLevel": "xhigh", "fastMode": False},
            }
            entry = {
                "sessionId": "session",
                "providerOverride": "deepseek",
                "modelOverride": "deepseek-v4-pro",
                "modelOverrideSource": "user",
                "thinkingLevel": "high",
                "fastMode": False,
            }
            self.assertEqual(manager._desired_preferences(spec, entry), {"fastMode": False})
            with patch.object(manager, "_gateway_call") as gateway_call:
                repaired_entry, repaired = manager._ensure_preferences(
                    "agent:main:project-test", spec, entry
                )
            gateway_call.assert_not_called()
            self.assertFalse(repaired)
            self.assertEqual(repaired_entry["thinkingLevel"], "high")

    def test_rollover_preserves_user_model_selection(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            manager.config["sessions"]["agent:main:project-test"]["allowManualModelOverride"] = True
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "message", "message": {"role": "user", "content": "继续处理"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            old_entry = {
                "sessionId": "old-session",
                "sessionFile": str(transcript),
                "totalTokens": 270000,
                "status": "done",
                "updatedAt": 0,
                "label": "测试",
                "providerOverride": "deepseek",
                "modelOverride": "deepseek-v4-pro",
                "modelOverrideSource": "user",
                "thinkingLevel": "high",
                "fastMode": False,
            }
            (root / "sessions.json").write_text(json.dumps({
                "agent:main:project-test": old_entry
            }), encoding="utf-8")
            handoff_path = root / "handoff.json"
            handoff_path.write_text("{}\n", encoding="utf-8")
            handoff = {
                "handoffPath": str(handoff_path),
                "continuityContext": "verified handoff",
            }

            def gateway_reset(_session_key):
                new_entry = {**old_entry, "sessionId": "new-session", "totalTokens": 0}
                (root / "sessions.json").write_text(json.dumps({
                    "agent:main:project-test": new_entry
                }), encoding="utf-8")
                return {"ok": True, "key": "agent:main:project-test", "entry": new_entry}

            with patch.object(manager, "_handoff", return_value=handoff), patch.object(
                manager, "_gateway_reset", side_effect=gateway_reset
            ):
                result = manager.rollover("agent:main:project-test")
            self.assertEqual(result["event"], "rollover_completed")
            self.assertEqual(result["providerOverride"], "deepseek")
            self.assertEqual(result["modelOverride"], "deepseek-v4-pro")
            record = MODULE.read_json(manager.current_path, {})["sessions"]["agent:main:project-test"]
            self.assertEqual(record["manualModelSelection"], {
                "providerOverride": "deepseek",
                "modelOverride": "deepseek-v4-pro",
            })
            self.assertEqual(record["sessionPreferences"], {"fastMode": False})

    def test_model_driven_thinking_can_clear_session_override(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            manager = self.make_manager(root)
            spec = {
                "label": "动态模型项目",
                "project": "mixed",
                "query": "mixed",
                "preferences": {"thinkingLevel": None, "fastMode": False},
            }
            entry = {"sessionId": "session", "thinkingLevel": "xhigh", "fastMode": False}

            def gateway_call(method, params):
                self.assertEqual(method, "sessions.patch")
                self.assertIsNone(params["thinkingLevel"])
                return {"ok": True, "entry": {**entry, **params}}

            with patch.object(manager, "_gateway_call", side_effect=gateway_call):
                repaired_entry, repaired = manager._ensure_preferences(
                    "agent:main:project-test", spec, entry
                )
            self.assertTrue(repaired)
            self.assertIsNone(repaired_entry["thinkingLevel"])
            self.assertEqual(repaired_entry["fastMode"], False)


if __name__ == "__main__":
    unittest.main()
