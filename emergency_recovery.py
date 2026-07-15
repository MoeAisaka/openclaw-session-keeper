#!/usr/bin/env python3
"""Fail-closed deterministic recovery for an oversized inactive OpenClaw session.

The tool never rewrites an active transcript or ``sessions.json`` directly.
After creating verified private backups it delegates mutation to OpenClaw's
``sessions.compact --max-lines`` Gateway RPC, which owns lifecycle locking and
session-store updates.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


TERMINAL_STATUSES = {None, "", "done", "idle", "failed", "timed_out", "cancelled", "killed"}
SAFE_ERROR_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
MAX_CLI_JSON_CHARS = 8 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_private_config(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("config_file_invalid")
    stat = path.stat()
    if hasattr(os, "getuid") and stat.st_uid != os.getuid():
        raise RuntimeError("config_file_not_owned_by_current_user")
    if stat.st_mode & 0o022:
        raise RuntimeError("config_file_is_group_or_world_writable")


def validate_private_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label}_file_invalid")
    stat = path.stat()
    if hasattr(os, "getuid") and stat.st_uid != os.getuid():
        raise RuntimeError(f"{label}_file_not_owned_by_current_user")
    if stat.st_mode & 0o022:
        raise RuntimeError(f"{label}_file_is_group_or_world_writable")


def atomic_json(path: Path, value: Any) -> None:
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"transcript_json_invalid_at_line:{line_number}") from exc
            if not isinstance(record, dict):
                raise RuntimeError(f"transcript_record_invalid_at_line:{line_number}")
            records.append(record)
    if not records or records[0].get("type") != "session":
        raise RuntimeError("transcript_session_header_missing")
    identifiers = [str(item.get("id")) for item in records if item.get("id")]
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError("transcript_duplicate_identifiers")
    return records


def inspect_jsonl(path: Path) -> dict[str, int]:
    """Validate a transcript without retaining every record in memory."""
    record_count = 0
    identifiers: set[str] = set()
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"transcript_json_invalid_at_line:{line_number}") from exc
            if not isinstance(record, dict):
                raise RuntimeError(f"transcript_record_invalid_at_line:{line_number}")
            record_count += 1
            if record_count == 1 and record.get("type") != "session":
                raise RuntimeError("transcript_session_header_missing")
            identifier = record.get("id")
            if identifier:
                value = str(identifier)
                if value in identifiers:
                    raise RuntimeError("transcript_duplicate_identifiers")
                identifiers.add(value)
    if record_count == 0:
        raise RuntimeError("transcript_session_header_missing")
    return {"recordCount": record_count}


def safe_error_code(error: Exception) -> str:
    value = str(error)
    return value if SAFE_ERROR_RE.fullmatch(value) else error.__class__.__name__


class EmergencyRecovery:
    def __init__(self, config_path: Path):
        raw_config_path = config_path.expanduser()
        validate_private_config(raw_config_path)
        self.config_path = raw_config_path.resolve()
        self.config = read_json(self.config_path)
        if not isinstance(self.config, dict):
            raise RuntimeError("config_root_invalid")
        self.sessions_path = Path(os.path.abspath(Path(self.config["sessionsStorePath"]).expanduser()))
        configured_bin = str(self.config["openclawBin"]).strip()
        if not configured_bin or "\x00" in configured_bin:
            raise RuntimeError("openclaw_binary_invalid")
        resolved_bin = shutil.which(configured_bin)
        if not resolved_bin:
            raise RuntimeError("openclaw_binary_not_found")
        executable = Path(resolved_bin).resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("openclaw_binary_not_executable")
        executable_stat = executable.stat()
        if executable_stat.st_mode & 0o022:
            raise RuntimeError("openclaw_binary_is_group_or_world_writable")
        self.openclaw_bin = str(executable)
        state_root = Path(self.config["stateRoot"]).expanduser().resolve()
        self.recovery_root = Path(self.config.get("recoveryRoot") or state_root / "recoveries").expanduser().resolve()
        self.lock_path = state_root / ".recovery.lock"

    @contextmanager
    def lock(self):
        private_dir(self.lock_path.parent)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(self.lock_path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("recovery_lock_busy")
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def runtime_entry(self, session_key: str) -> dict[str, Any]:
        params = {"activeMinutes": 5_256_000, "limit": 1000}
        result = subprocess.run(
            [self.openclaw_bin, "gateway", "call", "sessions.list", "--params", json.dumps(params), "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError("gateway_sessions_list_failed")
        payload = self.parse_json_output(
            result.stdout,
            lambda item: isinstance(item.get("sessions"), list)
            or isinstance(item.get("result", {}).get("sessions"), list),
        )
        candidates = payload.get("sessions") or payload.get("result", {}).get("sessions") or []
        for entry in candidates:
            if isinstance(entry, dict) and (entry.get("key") == session_key or entry.get("sessionKey") == session_key):
                return entry
        raise RuntimeError("gateway_runtime_session_missing")

    @staticmethod
    def parse_json_output(
        value: str,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        if len(value) > MAX_CLI_JSON_CHARS:
            raise RuntimeError("openclaw_json_output_too_large")
        decoder = json.JSONDecoder()
        candidates: list[dict[str, Any]] = []
        for index, character in enumerate(value):
            if character != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(value, index)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and (predicate is None or predicate(payload)):
                candidates.append(payload)
        if candidates:
            return candidates[-1]
        raise RuntimeError("openclaw_json_output_invalid")

    def agent_id_for_session(self, session_key: str) -> str | None:
        parts = session_key.split(":", 2)
        if len(parts) == 3 and parts[0] == "agent" and parts[1]:
            return parts[1]
        configured = str(self.config.get("agentId") or "").strip()
        if configured:
            return configured
        if session_key == "global":
            raise RuntimeError("agent_id_required_for_global_session")
        return None

    def compact_via_gateway(self, session_key: str, max_lines: int) -> dict[str, Any]:
        command = [
            self.openclaw_bin,
            "sessions",
            "compact",
            session_key,
            "--max-lines",
            str(max_lines),
        ]
        agent_id = self.agent_id_for_session(session_key)
        if agent_id:
            command.extend(["--agent", agent_id])
        command.extend(["--timeout", "120000", "--json"])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=150,
        )
        if result.returncode != 0:
            raise RuntimeError("gateway_deterministic_compaction_failed")
        payload = self.parse_json_output(
            result.stdout,
            lambda item: "ok" in item and ("compacted" in item or "reason" in item),
        )
        if payload.get("ok") is not True:
            raise RuntimeError("gateway_deterministic_compaction_rejected")
        if payload.get("compacted") is not True:
            raise RuntimeError("gateway_deterministic_compaction_not_applied")
        return payload

    @staticmethod
    def assert_idle(store_entry: dict[str, Any], runtime_entry: dict[str, Any]) -> dict[str, Any]:
        if runtime_entry.get("hasActiveRun") is True:
            raise RuntimeError("session_has_active_run")
        runtime_status = runtime_entry.get("status")
        if runtime_status not in TERMINAL_STATUSES:
            raise RuntimeError("runtime_session_not_idle")
        store_status = store_entry.get("status")
        if store_status == "running" and runtime_status in TERMINAL_STATUSES:
            return {
                "effectiveStatus": runtime_status or "done",
                "storedStatusStale": True,
            }
        if store_status not in TERMINAL_STATUSES:
            raise RuntimeError("stored_session_not_idle")
        return {
            "effectiveStatus": runtime_status or store_status or "idle",
            "storedStatusStale": False,
        }

    def assert_source_unchanged(
        self,
        session_key: str,
        entry: dict[str, Any],
        transcript: Path,
        transcript_hash: str,
    ) -> None:
        current = read_json(self.sessions_path).get(session_key)
        if not isinstance(current, dict):
            raise RuntimeError("session_entry_changed_before_compaction")
        current_transcript = Path(str(current.get("sessionFile") or "")).expanduser().resolve()
        if current.get("sessionId") != entry.get("sessionId") or current_transcript != transcript.resolve():
            raise RuntimeError("session_entry_changed_before_compaction")
        if not current_transcript.is_file() or sha256(current_transcript) != transcript_hash:
            raise RuntimeError("session_transcript_changed_before_compaction")

    def inspect(self, session_key: str, retain_records: int) -> dict[str, Any]:
        validate_private_file(self.sessions_path, "sessions_store")
        sessions = read_json(self.sessions_path)
        entry = sessions.get(session_key)
        if not isinstance(entry, dict):
            raise RuntimeError("session_entry_missing")
        transcript = Path(str(entry.get("sessionFile") or "")).expanduser()
        validate_private_file(transcript, "session_transcript")
        transcript_stats = inspect_jsonl(transcript)
        keep = max(10, min(5000, int(retain_records)))
        return {
            "sessionKey": session_key,
            "sessionId": entry.get("sessionId"),
            "status": entry.get("status"),
            "transcriptPath": str(transcript),
            "transcriptBytes": transcript.stat().st_size,
            "recordCount": transcript_stats["recordCount"],
            "retainRecords": min(keep, max(0, transcript_stats["recordCount"] - 1)),
        }

    def recover(self, session_key: str, retain_records: int, execute: bool) -> dict[str, Any]:
        with self.lock():
            validate_private_file(self.sessions_path, "sessions_store")
            sessions = read_json(self.sessions_path)
            entry = sessions.get(session_key)
            if not isinstance(entry, dict):
                raise RuntimeError("session_entry_missing")
            runtime = self.runtime_entry(session_key)
            status_before = self.assert_idle(entry, runtime)
            transcript = Path(str(entry.get("sessionFile") or "")).expanduser()
            validate_private_file(transcript, "session_transcript")
            transcript_stats = inspect_jsonl(transcript)
            keep = max(10, min(5000, int(retain_records)))
            before = {
                "transcriptBytes": transcript.stat().st_size,
                "recordCount": transcript_stats["recordCount"],
                "transcriptSha256": sha256(transcript),
            }
            preview = {
                "ok": True,
                "execute": execute,
                "sessionKey": session_key,
                "sessionId": entry.get("sessionId"),
                "statusBefore": entry.get("status"),
                "runtimeStatusBefore": runtime.get("status"),
                **status_before,
                **before,
                "retainRecords": keep,
            }
            if not execute:
                return preview
            if transcript_stats["recordCount"] <= keep + 1:
                return {**preview, "execute": True, "compacted": False, "reason": "transcript_already_within_limit"}

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            recovery_dir = self.recovery_root / f"{timestamp}-{hashlib.sha256(session_key.encode()).hexdigest()[:12]}"
            private_dir(recovery_dir)
            transcript_backup = recovery_dir / "transcript.jsonl"
            entry_backup = recovery_dir / "session-entry.json"
            shutil.copy2(transcript, transcript_backup)
            os.chmod(transcript_backup, 0o600)
            atomic_json(entry_backup, entry)
            if sha256(transcript_backup) != before["transcriptSha256"]:
                raise RuntimeError("transcript_backup_hash_mismatch")
            backup = {
                "transcript": transcript_backup.name,
                "transcriptSha256": sha256(transcript_backup),
                "sessionEntry": entry_backup.name,
                "sessionEntrySha256": sha256(entry_backup),
            }
            gateway_invoked = False
            try:
                self.assert_source_unchanged(session_key, entry, transcript, before["transcriptSha256"])
                gateway_invoked = True
                gateway_result = self.compact_via_gateway(session_key, keep)
                sessions_after = read_json(self.sessions_path)
                entry_after = sessions_after.get(session_key)
                if not isinstance(entry_after, dict):
                    raise RuntimeError("session_entry_missing_after_compaction")
                transcript_after = Path(str(entry_after.get("sessionFile") or "")).expanduser().resolve()
                if not transcript_after.is_file():
                    raise RuntimeError("session_transcript_missing_after_compaction")
                after_stats = inspect_jsonl(transcript_after)
                if after_stats["recordCount"] >= transcript_stats["recordCount"]:
                    raise RuntimeError("gateway_compaction_did_not_reduce_transcript")
                if after_stats["recordCount"] > keep + 2:
                    raise RuntimeError("gateway_compaction_retention_limit_exceeded")
                if transcript_after.stat().st_size >= before["transcriptBytes"]:
                    raise RuntimeError("gateway_compaction_did_not_reduce_bytes")
                runtime_after = self.runtime_entry(session_key)
                status_after = self.assert_idle(entry_after, runtime_after)
                result_summary = {
                    key: gateway_result.get(key)
                    for key in ("ok", "compacted", "archived", "kept", "reason", "key")
                    if key in gateway_result
                }
                manifest = {
                    "schemaVersion": 3,
                    "createdAt": now_iso(),
                    "completed": True,
                    **preview,
                    "recoveryDir": str(recovery_dir),
                    "backup": backup,
                    "gatewayResult": result_summary,
                    "after": {
                        "sessionId": entry_after.get("sessionId"),
                        "transcriptBytes": transcript_after.stat().st_size,
                        "recordCount": after_stats["recordCount"],
                        "transcriptSha256": sha256(transcript_after),
                        "storedStatus": entry_after.get("status"),
                        "runtimeStatus": runtime_after.get("status"),
                        "abortedLastRun": entry_after.get("abortedLastRun"),
                        **status_after,
                    },
                }
                atomic_json(recovery_dir / "manifest.json", manifest)
                return {**manifest, "ok": True, "execute": True, "compacted": True}
            except Exception as exc:
                failure_manifest = {
                    "schemaVersion": 3,
                    "createdAt": now_iso(),
                    "completed": False,
                    **preview,
                    "recoveryDir": str(recovery_dir),
                    "backup": backup,
                    "error": safe_error_code(exc),
                    "gatewayInvoked": gateway_invoked,
                    "liveMutationMayHaveOccurred": gateway_invoked,
                }
                try:
                    atomic_json(recovery_dir / "manifest.json", failure_manifest)
                except OSError:
                    pass
                raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--retain-records", type=int, default=50)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="After verified backups, request Gateway-owned deterministic trimming; default is preview",
    )
    args = parser.parse_args()
    try:
        payload = EmergencyRecovery(args.config).recover(args.session_key, args.retain_records, args.execute)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": safe_error_code(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
