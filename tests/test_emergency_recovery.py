import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "emergency_recovery.py"
SPEC = importlib.util.spec_from_file_location("emergency_recovery", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class EmergencyRecoveryTests(unittest.TestCase):
    def fixture(self, root: Path):
        transcript = root / "session.jsonl"
        records = [{"type": "session", "id": "session-1", "timestamp": "2026-01-01T00:00:00Z"}]
        parent = None
        for index in range(100):
            identifier = f"message-{index}"
            records.append({"type": "message", "id": identifier, "parentId": parent, "message": {"role": "user", "content": str(index)}})
            parent = identifier
        transcript.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        sessions = root / "sessions.json"
        sessions.write_text(json.dumps({"agent:main:project-test": {
            "sessionId": "session-1",
            "sessionFile": str(transcript),
            "status": "failed",
            "abortedLastRun": False,
            "totalTokens": 999999,
        }}), encoding="utf-8")
        config = root / "config.json"
        config.write_text(json.dumps({
            "sessionsStorePath": str(sessions),
            "stateRoot": str(root / "state"),
            "recoveryRoot": str(root / "recoveries"),
            "openclawBin": "openclaw",
        }), encoding="utf-8")
        return MODULE.EmergencyRecovery(config), sessions, transcript

    @staticmethod
    def simulate_gateway_compaction(sessions: Path, transcript: Path, keep: int):
        records = MODULE.load_jsonl(transcript)
        retained = [dict(records[0]), *[dict(item) for item in records[-keep:]]]
        if len(retained) > 1:
            retained[1]["parentId"] = None
        transcript.write_text("".join(json.dumps(item) + "\n" for item in retained), encoding="utf-8")
        store = json.loads(sessions.read_text(encoding="utf-8"))
        store["agent:main:project-test"].update({"status": "done", "totalTokens": 0})
        sessions.write_text(json.dumps(store), encoding="utf-8")
        return {"ok": True, "compacted": True, "retainedLines": keep}

    def test_preview_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as value:
            manager, sessions, transcript = self.fixture(Path(value))
            before = transcript.read_bytes()
            with patch.object(manager, "runtime_entry", return_value={"key": "agent:main:project-test", "status": "done", "hasActiveRun": False}):
                result = manager.recover("agent:main:project-test", 20, False)
            self.assertFalse(result["execute"])
            self.assertEqual(transcript.read_bytes(), before)
            self.assertEqual(json.loads(sessions.read_text())["agent:main:project-test"]["status"], "failed")

    def test_execute_backs_up_and_delegates_mutation_to_gateway(self):
        with tempfile.TemporaryDirectory() as value:
            manager, sessions, transcript = self.fixture(Path(value))
            gateway = lambda _key, keep: self.simulate_gateway_compaction(sessions, transcript, keep)
            with (
                patch.object(manager, "runtime_entry", return_value={"key": "agent:main:project-test", "status": "done", "hasActiveRun": False}),
                patch.object(manager, "compact_via_gateway", side_effect=gateway) as compact,
            ):
                result = manager.recover("agent:main:project-test", 20, True)
            compact.assert_called_once_with("agent:main:project-test", 20)
            records = MODULE.load_jsonl(transcript)
            self.assertEqual(len(records), 21)
            self.assertIsNone(records[1]["parentId"])
            entry = json.loads(sessions.read_text())["agent:main:project-test"]
            self.assertEqual(entry["status"], "done")
            self.assertEqual(entry["totalTokens"], 0)
            recovery_dir = Path(result["recoveryDir"])
            self.assertTrue((recovery_dir / "manifest.json").is_file())
            self.assertEqual((recovery_dir / "transcript.jsonl").stat().st_mode & 0o777, 0o600)
            self.assertEqual(result["after"]["runtimeStatus"], "done")

    def test_gateway_failure_leaves_live_state_unchanged(self):
        with tempfile.TemporaryDirectory() as value:
            manager, sessions, transcript = self.fixture(Path(value))
            before_transcript = transcript.read_bytes()
            before_sessions = sessions.read_bytes()
            with (
                patch.object(manager, "runtime_entry", return_value={"key": "agent:main:project-test", "status": "done", "hasActiveRun": False}),
                patch.object(manager, "compact_via_gateway", side_effect=RuntimeError("gateway_failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "gateway_failed"):
                    manager.recover("agent:main:project-test", 20, True)
            self.assertEqual(transcript.read_bytes(), before_transcript)
            self.assertEqual(sessions.read_bytes(), before_sessions)
            self.assertEqual(len(list((Path(value) / "recoveries").glob("*/transcript.jsonl"))), 1)
            manifest = next((Path(value) / "recoveries").glob("*/manifest.json"))
            failure = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertFalse(failure["completed"])
            self.assertTrue(failure["gatewayInvoked"])
            self.assertTrue(failure["liveMutationMayHaveOccurred"])
            self.assertEqual(failure["error"], "gateway_failed")

    def test_gateway_command_uses_argv_without_shell(self):
        with tempfile.TemporaryDirectory() as value:
            manager, _, _ = self.fixture(Path(value))
            completed = MODULE.subprocess.CompletedProcess([], 0, stdout='prefix\n{"ok":true,"compacted":true}\n', stderr="")
            with patch.object(MODULE.subprocess, "run", return_value=completed) as run:
                result = manager.compact_via_gateway("agent:main:project-test", 50)
            self.assertTrue(result["ok"])
            args, kwargs = run.call_args
            self.assertTrue(Path(args[0][0]).is_absolute())
            self.assertTrue(Path(args[0][0]).name.startswith("openclaw"))
            self.assertEqual(args[0][1:4], ["sessions", "compact", "agent:main:project-test"])
            self.assertIn("--agent", args[0])
            self.assertEqual(args[0][args[0].index("--agent") + 1], "main")
            self.assertNotIn("shell", kwargs)

    def test_active_run_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as value:
            manager, _, _ = self.fixture(Path(value))
            with patch.object(manager, "runtime_entry", return_value={"key": "agent:main:project-test", "status": "running", "hasActiveRun": True}):
                with self.assertRaisesRegex(RuntimeError, "session_has_active_run"):
                    manager.recover("agent:main:project-test", 20, True)

    def test_json_parser_ignores_unrelated_objects_and_uses_matching_payload(self):
        value = 'prefix {"level":"info"}\n{"ok":true,"compacted":true,"kept":50}\n'
        payload = MODULE.EmergencyRecovery.parse_json_output(
            value,
            lambda item: item.get("ok") is True and "compacted" in item,
        )
        self.assertEqual(payload["kept"], 50)

    def test_json_parser_rejects_unbounded_cli_output(self):
        with self.assertRaisesRegex(RuntimeError, "openclaw_json_output_too_large"):
            MODULE.EmergencyRecovery.parse_json_output("x" * (MODULE.MAX_CLI_JSON_CHARS + 1))

    def test_stale_stored_running_status_uses_gateway_effective_status(self):
        status = MODULE.EmergencyRecovery.assert_idle(
            {"status": "running"},
            {"status": "done", "hasActiveRun": False},
        )
        self.assertEqual(status["effectiveStatus"], "done")
        self.assertTrue(status["storedStatusStale"])

    def test_source_change_aborts_before_gateway_and_records_failure(self):
        with tempfile.TemporaryDirectory() as value:
            manager, _, _ = self.fixture(Path(value))
            with (
                patch.object(manager, "runtime_entry", return_value={"key": "agent:main:project-test", "status": "done", "hasActiveRun": False}),
                patch.object(manager, "assert_source_unchanged", side_effect=RuntimeError("session_transcript_changed_before_compaction")),
                patch.object(manager, "compact_via_gateway") as compact,
            ):
                with self.assertRaisesRegex(RuntimeError, "session_transcript_changed_before_compaction"):
                    manager.recover("agent:main:project-test", 20, True)
            compact.assert_not_called()
            manifest = next((Path(value) / "recoveries").glob("*/manifest.json"))
            failure = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertFalse(failure["gatewayInvoked"])
            self.assertFalse(failure["liveMutationMayHaveOccurred"])

    def test_config_must_not_be_group_or_world_writable(self):
        with tempfile.TemporaryDirectory() as value:
            _, _, _ = self.fixture(Path(value))
            config = Path(value) / "config.json"
            os.chmod(config, 0o666)
            with self.assertRaisesRegex(RuntimeError, "config_file_is_group_or_world_writable"):
                MODULE.EmergencyRecovery(config)

    def test_config_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as value:
            _, _, _ = self.fixture(Path(value))
            config = Path(value) / "config.json"
            link = Path(value) / "linked-config.json"
            link.symlink_to(config)
            with self.assertRaisesRegex(RuntimeError, "config_file_invalid"):
                MODULE.EmergencyRecovery(link)

    def test_sessions_store_must_not_be_group_or_world_writable(self):
        with tempfile.TemporaryDirectory() as value:
            manager, sessions, _ = self.fixture(Path(value))
            os.chmod(sessions, 0o666)
            with self.assertRaisesRegex(RuntimeError, "sessions_store_file_is_group_or_world_writable"):
                manager.inspect("agent:main:project-test", 20)
