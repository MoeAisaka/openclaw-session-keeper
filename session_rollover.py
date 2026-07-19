#!/usr/bin/env python3
"""Safe physical-session rollover behind stable OpenClaw project session keys."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path(
    os.environ.get(
        "OPENCLAW_SESSION_KEEPER_CONFIG",
        Path.home() / ".config" / "openclaw-session-keeper" / "config.json",
    )
).expanduser()
VERSION = "0.3.3"
ACTIVE_TASK_STATUSES = {"planned", "in_progress", "waiting", "blocked"}
IDLE_STATUSES = {None, "", "done", "idle", "killed", "failed", "timed_out", "cancelled"}
PATH_RE = re.compile(
    r"(?P<path>(?:(?:/Users/|/home/|~/)[^\s\]\)>'\"]+|[A-Za-z]:\\[^\s\]\)>'\"]+))"  # secret-scan: allow
)
VALID_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"}
VALID_FAST_MODES = {True, False, "auto"}
DEFAULT_VISIBLE_CONTINUITY = {
    "enabled": False,
    "label": "会话换代",
    "historyCheckLimit": 200,
    "lastAssistantOutcomeChars": 6000,
    "firstDispatchStartTimeoutSeconds": 180,
    "repairExistingSessionKeys": [],
}
DEFAULT_ROLLOVER_TIMING = {
    "deferUntilNextUserMessage": False,
}


def ensure_private_dir(path: Path) -> None:
    """Create a state directory and keep it private under any process umask."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso_age_seconds(value: Any) -> float | None:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
    except (TypeError, ValueError):
        return None


def atomic_write_json(path: Path, value: Any) -> None:
    ensure_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_text(path: Path, value: str) -> None:
    ensure_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def session_slug(session_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", session_key).strip("-._")


def codex_binding_store_key(session_key: str) -> str:
    """Match OpenClaw's stable-session Codex binding key derivation."""
    parts = session_key.split(":", 2)
    if len(parts) != 3 or parts[0] != "agent" or not parts[1]:
        raise ValueError(f"unsupported_stable_session_key:{session_key}")
    digest = base64.urlsafe_b64encode(hashlib.sha256(session_key.encode("utf-8")).digest())
    return f"session-key:{parts[1]}:{digest.decode('ascii').rstrip('=')}"


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        value = content.get("text") or content.get("content")
        return value if isinstance(value, str) else ""
    return ""


def bounded_excerpt(value: str, limit: int) -> str:
    """Keep both the conclusion lead and trailing evidence within a hard bound."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 80:
        return text[:limit]
    marker = "\n…[内容已按交接上限截断]…\n"
    remaining = max(0, limit - len(marker))
    head = max(1, remaining * 2 // 3)
    tail = max(0, remaining - head)
    return text[:head] + marker + (text[-tail:] if tail else "")


def continuation_decision(
    messages: list[dict[str, str]],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a deterministic resume directive without interpreting free-form prose."""
    last_role = messages[-1]["role"] if messages else None
    resumable = [
        task for task in tasks
        if task.get("status") in {"planned", "in_progress"}
    ]
    paused = [
        task for task in tasks
        if task.get("status") in {"waiting", "blocked"}
    ]
    if resumable:
        action = "resume_registered_work"
        reason = "registered_resumable_workflow_exists"
        should_auto_continue = True
    elif paused:
        action = "report_registered_blocker"
        reason = "registered_workflow_is_waiting_or_blocked"
        should_auto_continue = False
    elif last_role == "user":
        action = "respond_to_pending_user"
        reason = "old_generation_ended_with_unanswered_user_message"
        should_auto_continue = True
    elif last_role == "assistant":
        action = "await_current_user_request"
        reason = "previous_turn_replied_and_no_active_workflow"
        should_auto_continue = False
    else:
        action = "manual_review_required"
        reason = "no_visible_turn_state"
        should_auto_continue = False
    return {
        "schemaVersion": 1,
        "previousTurnState": (
            "assistant_replied" if last_role == "assistant"
            else "user_message_unanswered" if last_role == "user"
            else "unknown"
        ),
        "lastVisibleRole": last_role,
        "activeWorkflowTaskCount": len(tasks),
        "resumableWorkflowTaskIds": [task.get("task_id") for task in resumable],
        "pausedWorkflowTaskIds": [task.get("task_id") for task in paused],
        "action": action,
        "shouldAutoContinue": should_auto_continue,
        "reason": reason,
        "sourceOfTruth": "workflow_ledger_then_visible_turn_order",
    }


def last_assistant_outcome(messages: list[dict[str, str]], limit: int) -> str | None:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("text", "").strip():
            return bounded_excerpt(message["text"], limit)
    return None


def continuity_notice_marker(old_session_id: str, new_session_id: str) -> str:
    """Stable marker used to make chat.inject retries idempotent."""
    return (
        "<!-- openclaw-session-keeper "
        f"old={old_session_id} new={new_session_id} -->"
    )


def render_visible_continuity_notice(
    handoff: dict[str, Any],
    new_session_id: str,
    outcome_char_limit: int = 6000,
) -> str:
    """Render the previous outcome and deterministic continuation decision."""
    old_session_id = str(handoff["oldSessionId"])
    marker = continuity_notice_marker(old_session_id, new_session_id)
    decision = handoff.get("continuationDecision")
    if not isinstance(decision, dict):
        decision = continuation_decision(
            handoff.get("recentMessages", []),
            handoff.get("workflowTasks", []),
        )
    previous_turn = {
        "assistant_replied": "已产出最终答复",
        "user_message_unanswered": "存在未答复的用户消息",
    }.get(decision.get("previousTurnState"), "无法确定")
    auto_continue = "是" if decision.get("shouldAutoContinue") else "否"
    lines = [
        "会话已安全换代；上一轮结果与续跑决策已恢复。",
        "",
        f"- 项目：{handoff['label']}",
        f"- 稳定入口：`{handoff['sessionKey']}`",
        f"- 上一代会话：`{old_session_id}`",
        f"- 当前会话：`{new_session_id}`",
        "- 旧历史：完整归档，未删除",
        f"- 上一轮执行：{previous_turn}",
        f"- 登记中的活动工作流：{decision.get('activeWorkflowTaskCount', 0)}",
        f"- 自动续跑：{auto_continue}（`{decision.get('action')}`）",
        f"- 交接记录：`{handoff['handoffPath']}`",
        "",
    ]
    outcome = last_assistant_outcome(
        handoff.get("recentMessages", []),
        outcome_char_limit,
    )
    if outcome:
        lines.extend(["上一轮最终答复（原文节选）：", "", outcome, ""])
    lines.extend([
        "说明：换代校验成功不等于新会话任务已执行；首个新会话任务会单独记录开始与结束状态。",
        marker,
    ])
    return "\n".join(lines)


def recent_visible_messages(path: Path, count: int, char_limit: int) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "message":
                continue
            message = event.get("message") or {}
            role = message.get("role") or event.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = content_to_text(message.get("content", event.get("content", ""))).strip()
            if not text:
                continue
            messages.append({"role": role, "text": text})
    selected = messages[-count:]
    result: list[dict[str, str]] = []
    remaining = char_limit
    for item in reversed(selected):
        if remaining <= 0:
            break
        text = item["text"][-min(len(item["text"]), remaining):]
        result.append({"role": item["role"], "text": text})
        remaining -= len(text)
    return list(reversed(result))


def active_ledger_tasks(task_dir: Path, project: str, label: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    needles = {project.lower(), label.lower()}
    if not task_dir.exists():
        return tasks
    for path in task_dir.glob("*.json"):
        task = read_json(path, {})
        if task.get("status") not in ACTIVE_TASK_STATUSES:
            continue
        haystack = " ".join([
            str(task.get("title", "")),
            str(task.get("goal", "")),
            str(task.get("context_path", "")),
            " ".join(map(str, task.get("tags", []))),
        ]).lower()
        if not any(needle and needle in haystack for needle in needles):
            continue
        tasks.append({
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "status": task.get("status"),
            "current_step": task.get("current_step"),
            "next_step": task.get("next_step"),
            "artifacts": task.get("artifacts", [])[-12:],
            "blockers": task.get("blockers", [])[-6:],
        })
    return sorted(tasks, key=lambda item: str(item.get("task_id")))[:12]


def nmem_recall(nmem_bin: str, query: str) -> tuple[bool, list[dict[str, Any]], str | None]:
    try:
        status = subprocess.run([nmem_bin, "status"], capture_output=True, text=True, timeout=15)
        if status.returncode != 0 or not re.search(r"^\s*status\s+ok\s*$", status.stdout, re.MULTILINE):
            return False, [], "nmem_status_failed"
        result = subprocess.run(
            [nmem_bin, "m", "search", query, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, [], "nmem_search_failed"
        payload = json.loads(result.stdout)
        memories = []
        for item in payload.get("memories", [])[:8]:
            memories.append({
                "id": item.get("id"),
                "title": item.get("title"),
                "unit_type": item.get("unit_type"),
                "content": str(item.get("content", ""))[:1200],
            })
        return True, memories, None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return False, [], f"nmem_exception:{type(exc).__name__}"


def referenced_paths(messages: list[dict[str, str]], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: set[str] = set()
    for item in messages:
        for match in PATH_RE.finditer(item["text"]):
            candidates.add(os.path.expanduser(match.group("path").rstrip(".,:;")))
    for task in tasks:
        for value in task.get("artifacts", []):
            if isinstance(value, str) and value.startswith(("/", "~/")):
                candidates.add(os.path.expanduser(value))
    output = []
    for value in sorted(candidates)[:50]:
        path = Path(value)
        try:
            stat = path.stat()
        except OSError:
            output.append({"path": value, "exists": False})
            continue
        item = {
            "path": value,
            "exists": True,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "kind": "directory" if path.is_dir() else "file",
        }
        if path.is_file() and stat.st_size <= 5 * 1024 * 1024:
            item["sha256"] = sha256_file(path)
        output.append(item)
    return output


def render_continuity_context(handoff: dict[str, Any], char_limit: int) -> str:
    decision = handoff.get("continuationDecision")
    if not isinstance(decision, dict):
        decision = continuation_decision(
            handoff.get("recentMessages", []),
            handoff.get("workflowTasks", []),
        )
    required = [
        "[PROJECT_SESSION_CONTINUITY]",
        f"project={handoff['project']}",
        f"label={handoff['label']}",
        f"stable_session_key={handoff['sessionKey']}",
        f"previous_session_id={handoff['oldSessionId']}",
        f"handoff_file={handoff['handoffPath']}",
        "This is a verified rollover handoff. Treat files and external state as source of truth; re-check them before mutation.",
        f"previous_turn_state={decision.get('previousTurnState')}",
        f"continuation_action={decision.get('action')}",
        f"should_auto_continue={str(bool(decision.get('shouldAutoContinue'))).lower()}",
        f"continuation_reason={decision.get('reason')}",
        "When the user asks for progress or status, surface the preserved last assistant outcome first.",
        "Do not silently rerun completed work. Auto-continue only when should_auto_continue=true and the registered workflow state supports it.",
    ]
    desired_preferences = handoff.get("sessionPreferences", {}).get("desired", {})
    if desired_preferences:
        required.append(
            "session_preferences="
            f"thinkingLevel:{desired_preferences.get('thinkingLevel')},"
            f"fastMode:{desired_preferences.get('fastMode')}"
        )
    blocks: list[str] = []
    if handoff.get("workflowTasks"):
        task_lines = ["Active workflow tasks:"]
        for task in handoff["workflowTasks"]:
            task_lines.append(
                f"- {task.get('task_id')}: {task.get('status')} | current={task.get('current_step') or '-'} | next={task.get('next_step') or '-'}"
            )
        blocks.append("\n".join(task_lines))
    outcome = last_assistant_outcome(
        handoff.get("recentMessages", []),
        min(6000, max(800, char_limit // 2)),
    )
    if outcome:
        blocks.append("Last assistant outcome (verbatim, bounded):\n" + outcome)
    if handoff.get("recentMessages"):
        recent_lines = ["Recent visible conversation (verbatim, newest state preserved):"]
        for message in handoff["recentMessages"][-6:]:
            role = "USER" if message["role"] == "user" else "ASSISTANT"
            recent_lines.append(f"{role}: {bounded_excerpt(message['text'], 2400)}")
        blocks.append("\n".join(recent_lines))
    if handoff.get("memories"):
        memory_lines = ["Relevant durable memories:"]
        for memory in handoff["memories"]:
            memory_lines.append(f"- {memory.get('id')}: {memory.get('title')}")
        blocks.append("\n".join(memory_lines))
    if handoff.get("paths"):
        path_lines = ["Referenced artifacts:"]
        for item in handoff["paths"][:20]:
            path_lines.append(f"- {item.get('path')} | exists={item.get('exists')}")
        blocks.append("\n".join(path_lines))

    closing = "[/PROJECT_SESSION_CONTINUITY]"
    truncation = "[additional continuity context omitted; read handoff_file for full verified state]"
    value = "\n".join(required)
    budget = max(0, char_limit - len(closing) - 1)
    value = value[:budget]
    for block in blocks:
        separator = "\n" if not value else "\n"
        available = budget - len(value) - len(separator)
        if available <= 0:
            break
        if len(block) <= available:
            value += separator + block
            continue
        if available > len(truncation) + 80:
            excerpt_limit = available - len(truncation) - 1
            value += separator + bounded_excerpt(block, excerpt_limit) + "\n" + truncation
        break
    return value + "\n" + closing


class RolloverManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = read_json(config_path)
        if not isinstance(self.config, dict):
            raise ValueError(f"Invalid config: {config_path}")
        self.state_root = Path(self.config["stateRoot"]).expanduser()
        self.lock_path = self.state_root / ".lock"
        self.events_path = self.state_root / "events.jsonl"
        self.current_path = self.state_root / "current.json"
        self.status_path = self.state_root / "status.json"

    def _sessions(self) -> dict[str, Any]:
        payload = read_json(Path(self.config["sessionsStorePath"]).expanduser(), {})
        if not isinstance(payload, dict):
            raise RuntimeError("sessions_store_invalid")
        return payload

    def _retired_codex_binding(
        self,
        session_key: str,
        entry: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return a terminal binding only when it targets the current physical session."""
        database_value = self.config.get("codexStateDbPath")
        if not database_value:
            return None
        database_path = Path(str(database_value)).expanduser()
        if not database_path.is_file():
            raise RuntimeError("codex_state_database_missing")
        binding_key = codex_binding_store_key(session_key)
        uri = f"{database_path.resolve().as_uri()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
                row = connection.execute(
                    """
                    SELECT value_json, created_at
                    FROM plugin_state_entries
                    WHERE plugin_id = ? AND namespace = ? AND entry_key = ?
                    """,
                    ("codex", "app-server-thread-bindings", binding_key),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"codex_binding_read_failed:{type(exc).__name__}") from exc
        if not row:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("codex_binding_value_invalid") from exc
        current_session_id = str(entry.get("sessionId") or "")
        if not (
            isinstance(value, dict)
            and value.get("state") == "cleared"
            and value.get("retired") is True
            and str(value.get("sessionId") or "") == current_session_id
            and current_session_id
        ):
            return None
        return {
            "bindingKey": binding_key,
            "sessionId": current_session_id,
            "createdAtMs": int(row[1]),
        }

    @contextmanager
    def _lock(self):
        ensure_private_dir(self.state_root)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _router_state(self) -> dict[str, Any]:
        payload = read_json(Path(self.config["routerStatePath"]).expanduser(), {"sessions": {}})
        return payload if isinstance(payload, dict) else {"sessions": {}}

    @staticmethod
    def _manual_model_selection(entry: dict[str, Any] | None) -> dict[str, str] | None:
        if not isinstance(entry, dict) or entry.get("modelOverrideSource") != "user":
            return None
        provider = str(entry.get("providerOverride") or "").strip()
        model = str(entry.get("modelOverride") or "").strip()
        if not provider or not model:
            return None
        if model.lower().startswith(f"{provider.lower()}/"):
            model = model[len(provider) + 1 :].strip()
        if not model:
            return None
        return {"providerOverride": provider, "modelOverride": model}

    def _desired_preferences(
        self,
        spec: dict[str, Any],
        entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        preferences = {
            **self.config.get("sessionPreferences", {}),
            **spec.get("preferences", {}),
        }
        if spec.get("allowManualModelOverride") is True and self._manual_model_selection(entry):
            preferences.pop("thinkingLevel", None)
        thinking_level = preferences.get("thinkingLevel")
        fast_mode = preferences.get("fastMode")
        if spec.get("allowManualFastMode") is True and isinstance(entry, dict):
            observed_fast_mode = entry.get("fastMode")
            if isinstance(observed_fast_mode, bool):
                fast_mode = observed_fast_mode
        if thinking_level is not None and thinking_level not in VALID_THINKING_LEVELS:
            raise RuntimeError(f"invalid_thinking_level:{thinking_level}")
        if fast_mode not in VALID_FAST_MODES:
            raise RuntimeError(f"invalid_fast_mode:{fast_mode}")
        desired: dict[str, Any] = {"fastMode": fast_mode}
        if "thinkingLevel" in preferences:
            desired["thinkingLevel"] = thinking_level
        return desired

    @staticmethod
    def _preferences_match(entry: dict[str, Any], desired: dict[str, Any]) -> bool:
        return all(entry.get(key) == value for key, value in desired.items())

    @staticmethod
    def _physical_age_days(entry: dict[str, Any]) -> float | None:
        started_ms = int(entry.get("sessionStartedAt") or 0)
        if started_ms <= 0:
            return None
        age_ms = max(0, int(time.time() * 1000) - started_ms)
        return age_ms / 86_400_000

    def _decision(
        self,
        tokens: int,
        entry: dict[str, Any] | None = None,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        thresholds = {**self.config["thresholds"], **(spec or {}).get("thresholds", {})}
        age_days = self._physical_age_days(entry or {})
        transcript_value = str((entry or {}).get("sessionFile") or "")
        transcript_path = Path(transcript_value).expanduser() if transcript_value else None
        try:
            transcript_bytes = transcript_path.stat().st_size if transcript_path and transcript_path.is_file() else 0
        except OSError:
            transcript_bytes = 0
        emergency_bytes = int(thresholds.get("emergencyTranscriptBytes") or 0)
        rollover_bytes = int(thresholds.get("rolloverTranscriptBytes") or 0)
        checkpoint_bytes = int(thresholds.get("checkpointTranscriptBytes") or 0)
        if emergency_bytes and transcript_bytes >= emergency_bytes:
            action, reason = "emergency", "emergency_transcript_limit"
        elif tokens >= int(thresholds["emergencyTokens"]):
            action, reason = "emergency", "emergency_token_limit"
        elif rollover_bytes and transcript_bytes >= rollover_bytes:
            action, reason = "rollover", "rollover_transcript_limit"
        elif tokens >= int(thresholds["rolloverTokens"]):
            action, reason = "rollover", "rollover_token_limit"
        elif age_days is not None and age_days >= float(thresholds["maxPhysicalAgeDays"]):
            action, reason = "rollover", "max_physical_age"
        elif checkpoint_bytes and transcript_bytes >= checkpoint_bytes:
            action, reason = "checkpoint", "checkpoint_transcript_limit"
        elif tokens >= int(thresholds["checkpointTokens"]):
            action, reason = "checkpoint", "checkpoint_token_limit"
        else:
            action, reason = "healthy", "within_limits"
        return {
            "action": action,
            "reason": reason,
            "physicalAgeDays": round(age_days, 3) if age_days is not None else None,
            "transcriptBytes": transcript_bytes,
        }

    def _gateway_call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        require_ok: bool = True,
    ) -> dict[str, Any]:
        result = subprocess.run(
            [
                self.config["openclawBin"],
                "gateway",
                "call",
                method,
                "--params",
                json.dumps(params, ensure_ascii=False),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"gateway_{method.replace('.', '_')}_failed:{detail}")
        start = result.stdout.find("{")
        if start < 0:
            raise RuntimeError(f"gateway_{method.replace('.', '_')}_invalid_output")
        payload = json.loads(result.stdout[start:])
        if require_ok and not payload.get("ok"):
            raise RuntimeError(f"gateway_{method.replace('.', '_')}_not_ok")
        return payload

    def _visible_continuity_config(self) -> dict[str, Any]:
        value = self.config.get("visibleContinuity", {})
        if not isinstance(value, dict):
            raise RuntimeError("visible_continuity_config_invalid")
        merged = {**DEFAULT_VISIBLE_CONTINUITY, **value}
        if not isinstance(merged.get("enabled"), bool):
            raise RuntimeError("visible_continuity_enabled_invalid")
        label = merged.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise RuntimeError("visible_continuity_label_invalid")
        limit = merged.get("historyCheckLimit")
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise RuntimeError("visible_continuity_history_limit_invalid")
        repair_keys = merged.get("repairExistingSessionKeys")
        if not isinstance(repair_keys, list) or not all(
            isinstance(item, str) and item.strip() for item in repair_keys
        ):
            raise RuntimeError("visible_continuity_repair_keys_invalid")
        outcome_limit = merged.get("lastAssistantOutcomeChars")
        if not isinstance(outcome_limit, int) or not 500 <= outcome_limit <= 20000:
            raise RuntimeError("visible_continuity_outcome_limit_invalid")
        start_timeout = merged.get("firstDispatchStartTimeoutSeconds")
        if not isinstance(start_timeout, int) or not 30 <= start_timeout <= 3600:
            raise RuntimeError("visible_continuity_start_timeout_invalid")
        return merged

    def _rollover_timing_config(self) -> dict[str, Any]:
        value = self.config.get("rolloverTiming", {})
        if not isinstance(value, dict):
            raise RuntimeError("rollover_timing_config_invalid")
        merged = {**DEFAULT_ROLLOVER_TIMING, **value}
        if not isinstance(merged.get("deferUntilNextUserMessage"), bool):
            raise RuntimeError("rollover_timing_defer_invalid")
        return merged

    def _history_contains_notice(self, session_key: str, marker: str, limit: int) -> bool:
        payload = self._gateway_call(
            "chat.history",
            {"sessionKey": session_key, "limit": limit},
            require_ok=False,
        )
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            raise RuntimeError("chat_history_messages_invalid")
        return any(
            marker in content_to_text(message.get("content", ""))
            for message in messages
            if isinstance(message, dict)
        )

    def _load_verified_handoff(
        self,
        session_key: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        handoff_path = Path(str(record.get("handoffPath") or ""))
        if not handoff_path.is_file():
            raise RuntimeError("visibility_handoff_missing")
        expected_sha = str(record.get("handoffSha256") or "")
        if expected_sha and sha256_file(handoff_path) != expected_sha:
            raise RuntimeError("visibility_handoff_hash_mismatch")
        handoff = read_json(handoff_path)
        if not isinstance(handoff, dict):
            raise RuntimeError("visibility_handoff_invalid")
        if handoff.get("sessionKey") != session_key:
            raise RuntimeError("visibility_handoff_session_key_mismatch")
        if handoff.get("oldSessionId") != record.get("oldSessionId"):
            raise RuntimeError("visibility_handoff_generation_mismatch")
        return handoff

    def _ensure_visible_continuity(
        self,
        session_key: str,
        record: dict[str, Any],
        new_session_id: str,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        config = self._visible_continuity_config()
        if not config["enabled"] and not force:
            return {"status": "disabled"}
        entry = self._sessions().get(session_key)
        if not isinstance(entry, dict) or str(entry.get("sessionId") or "") != new_session_id:
            raise RuntimeError("visibility_current_generation_mismatch")
        handoff = self._load_verified_handoff(session_key, record)
        marker = continuity_notice_marker(str(record["oldSessionId"]), new_session_id)
        if self._history_contains_notice(session_key, marker, int(config["historyCheckLimit"])):
            return {
                "status": "verified",
                "marker": marker,
                "verification": "existing_notice",
                "verifiedAt": now_iso(),
            }
        if not self._is_idle(entry):
            return {
                "status": "pending_busy",
                "marker": marker,
                "sessionStatus": entry.get("status"),
            }
        message = render_visible_continuity_notice(
            handoff,
            new_session_id,
            int(config["lastAssistantOutcomeChars"]),
        )
        if dry_run:
            return {
                "status": "would_inject",
                "marker": marker,
                "label": config["label"],
                "message": message,
            }
        response = self._gateway_call(
            "chat.inject",
            {
                "sessionKey": session_key,
                "message": message,
                "label": config["label"],
            },
        )
        if not self._history_contains_notice(session_key, marker, int(config["historyCheckLimit"])):
            raise RuntimeError("visibility_notice_verification_failed")
        return {
            "status": "verified",
            "marker": marker,
            "messageId": response.get("messageId"),
            "verification": "injected_and_read_back",
            "verifiedAt": now_iso(),
        }

    def _ensure_preferences(
        self,
        session_key: str,
        spec: dict[str, Any],
        entry: dict[str, Any] | None = None,
        desired: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        current = entry if isinstance(entry, dict) else self._sessions().get(session_key, {})
        if not isinstance(current, dict):
            raise RuntimeError("session_entry_missing")
        expected = dict(desired) if isinstance(desired, dict) else self._desired_preferences(spec, current)
        repaired = False
        if not self._preferences_match(current, expected):
            payload = self._gateway_call("sessions.patch", {"key": session_key, **expected})
            patched = payload.get("entry")
            current = patched if isinstance(patched, dict) else self._sessions().get(session_key, {})
            repaired = True
        if not isinstance(current, dict) or not self._preferences_match(current, expected):
            raise RuntimeError("session_preferences_verification_failed")
        return current, repaired

    def _action(
        self,
        tokens: int,
        entry: dict[str, Any] | None = None,
        spec: dict[str, Any] | None = None,
    ) -> str:
        return str(self._decision(tokens, entry, spec)["action"])

    def _is_idle(self, entry: dict[str, Any]) -> bool:
        if entry.get("status") not in IDLE_STATUSES:
            return False
        last = max(int(entry.get("updatedAt") or 0), int(entry.get("lastInteractionAt") or 0))
        idle_ms = int(self.config["thresholds"]["minIdleSeconds"]) * 1000
        return int(time.time() * 1000) - last >= idle_ms

    def _handoff(self, session_key: str, spec: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        transcript = Path(entry.get("sessionFile") or "")
        if not transcript.is_file():
            raise RuntimeError("session_transcript_missing")
        thresholds = self.config["thresholds"]
        recent = recent_visible_messages(
            transcript,
            int(thresholds["recentMessageCount"]),
            int(thresholds["recentMessageChars"]),
        )
        if not recent:
            raise RuntimeError("no_visible_messages_for_handoff")
        tasks = active_ledger_tasks(
            Path(self.config["workflowTaskDir"]).expanduser(),
            spec["project"],
            spec["label"],
        )
        nmem_ok, memories, nmem_error = nmem_recall(self.config["nmemBin"], spec["query"])
        if not nmem_ok:
            raise RuntimeError(nmem_error or "nmem_unavailable")
        router = self._router_state().get("sessions", {}).get(session_key, {})
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        archive_dir = self.state_root / "handoffs" / session_slug(session_key) / f"{timestamp}-{entry['sessionId']}-{uuid.uuid4().hex[:8]}"
        handoff_path = archive_dir / "handoff.json"
        handoff = {
            "schemaVersion": 3,
            "createdAt": now_iso(),
            "sessionKey": session_key,
            "oldSessionId": entry["sessionId"],
            "label": spec["label"],
            "project": spec["project"],
            "routeState": router,
            "tokens": int(entry.get("totalTokens") or 0),
            "status": entry.get("status"),
            "sessionPreferences": {
                "observed": {
                    "providerOverride": entry.get("providerOverride"),
                    "modelOverride": entry.get("modelOverride"),
                    "modelOverrideSource": entry.get("modelOverrideSource"),
                    "thinkingLevel": entry.get("thinkingLevel"),
                    "fastMode": entry.get("fastMode"),
                },
                "desired": self._desired_preferences(spec, entry),
            },
            "transcript": {
                "sourcePath": str(transcript),
                "sha256": sha256_file(transcript),
                "size": transcript.stat().st_size,
            },
            "workflowTasks": tasks,
            "memories": memories,
            "recentMessages": recent,
            "paths": [],
            "handoffPath": str(handoff_path),
        }
        handoff["continuationDecision"] = continuation_decision(recent, tasks)
        handoff["paths"] = referenced_paths(recent, tasks)
        handoff["continuityContext"] = render_continuity_context(
            handoff, int(thresholds["continuityContextChars"])
        )
        ensure_private_dir(archive_dir)
        transcript_copy = archive_dir / "transcript.jsonl"
        shutil.copy2(transcript, transcript_copy)
        os.chmod(transcript_copy, 0o600)
        if sha256_file(transcript_copy) != handoff["transcript"]["sha256"]:
            raise RuntimeError("transcript_archive_hash_mismatch")
        handoff["transcript"]["archivePath"] = str(transcript_copy)
        atomic_write_json(handoff_path, handoff)
        atomic_write_text(archive_dir / "continuity.txt", handoff["continuityContext"] + "\n")
        return handoff

    def _update_current(self, session_key: str, value: dict[str, Any]) -> None:
        current = read_json(self.current_path, {"schemaVersion": 1, "sessions": {}})
        if not isinstance(current, dict):
            current = {"schemaVersion": 1, "sessions": {}}
        current.setdefault("sessions", {})[session_key] = value
        current["updatedAt"] = now_iso()
        atomic_write_json(self.current_path, current)

    def _gateway_reset(self, session_key: str) -> dict[str, Any]:
        # The production core extension makes this an atomic reject-if-active
        # lifecycle mutation. Never let Session Keeper abort admitted work.
        return self._gateway_call("sessions.reset", {
            "key": session_key,
            "reason": "reset",
            "interruptActiveWork": False,
        })

    def _activation_value(
        self,
        *,
        prepared: dict[str, Any],
        new_session_id: str,
        response: dict[str, Any] | None = None,
        verification: str = "verified",
        visibility_notice: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            **prepared,
            "status": "active",
            "newSessionId": new_session_id,
            "maxInjections": 3,
            "activatedAt": now_iso(),
            "verification": verification,
            "visibilityNotice": visibility_notice or {"status": "disabled"},
            "resetSafety": {
                "mode": "reject_if_active",
                "verifiedAt": now_iso(),
            },
            "gatewayResponse": {
                "ok": (response or {}).get("ok"),
                "key": (response or {}).get("key"),
            },
        }

    def _arm_rollover_unlocked(
        self,
        session_key: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Prepare a verified handoff but leave the current generation visible."""
        spec = self.config.get("sessions", {}).get(session_key)
        if not isinstance(spec, dict):
            raise RuntimeError("session_not_allowlisted")
        entry = self._sessions().get(session_key)
        if not isinstance(entry, dict):
            raise RuntimeError("session_entry_missing")
        tokens = int(entry.get("totalTokens") or 0)
        decision = self._decision(tokens, entry, spec)
        if decision["action"] != "rollover":
            return {
                "sessionKey": session_key,
                "action": "skip_not_deferred_rollover",
                "tokens": tokens,
                **{key: value for key, value in decision.items() if key != "action"},
            }
        if not self._is_idle(entry):
            return {
                "sessionKey": session_key,
                "action": "pending_busy",
                "tokens": tokens,
                "status": entry.get("status"),
                **{key: value for key, value in decision.items() if key != "action"},
            }
        current = read_json(self.current_path, {"sessions": {}})
        existing = current.get("sessions", {}).get(session_key, {}) if isinstance(current, dict) else {}
        if (
            isinstance(existing, dict)
            and existing.get("status") in {
                "pending_next_user",
                "draining_current_run",
                "ready_after_run",
            }
            and existing.get("oldSessionId") == entry.get("sessionId")
            and Path(str(existing.get("handoffPath") or "")).is_file()
        ):
            return {
                "sessionKey": session_key,
                "action": f"rollover_{existing.get('status')}",
                "tokens": tokens,
                "sessionId": entry.get("sessionId"),
                "armedAt": existing.get("armedAt"),
                **{key: value for key, value in decision.items() if key != "action"},
            }
        if dry_run:
            return {
                "sessionKey": session_key,
                "action": "would_defer_rollover",
                "tokens": tokens,
                "sessionId": entry.get("sessionId"),
                "trigger": decision["reason"],
                **{key: value for key, value in decision.items() if key != "action"},
            }
        entry, preferences_repaired = self._ensure_preferences(session_key, spec, entry)
        handoff = self._handoff(session_key, spec, entry)
        record = {
            "status": "pending_next_user",
            "sessionKey": session_key,
            "oldSessionId": entry.get("sessionId"),
            "label": spec["label"],
            "project": spec["project"],
            "tokens": tokens,
            "trigger": decision["reason"],
            "transcriptBytes": decision["transcriptBytes"],
            "physicalAgeDays": decision["physicalAgeDays"],
            "handoffPath": handoff["handoffPath"],
            "continuityContext": handoff["continuityContext"],
            "handoffSha256": sha256_file(Path(handoff["handoffPath"])),
            "sessionPreferences": self._desired_preferences(spec, entry),
            "manualModelSelection": self._manual_model_selection(entry),
            "preferencesRepairedBeforeArm": preferences_repaired,
            "maxInjections": 3,
            "armedAt": now_iso(),
        }
        self._update_current(session_key, record)
        event = {
            "ts": now_iso(),
            "event": "rollover_deferred",
            "sessionKey": session_key,
            "oldSessionId": entry.get("sessionId"),
            "tokens": tokens,
            "trigger": decision["reason"],
            "handoffPath": handoff["handoffPath"],
        }
        append_jsonl(self.events_path, event)
        return {**event, "action": "rollover_deferred"}

    def _activate_pending_unlocked(
        self,
        session_key: str,
        *,
        dry_run: bool = False,
        trigger_override: str = "next_user_message",
    ) -> dict[str, Any]:
        """Manually rotate an armed generation only when the core can reject active work."""
        spec = self.config.get("sessions", {}).get(session_key)
        if not isinstance(spec, dict):
            raise RuntimeError("session_not_allowlisted")
        current = read_json(self.current_path, {"sessions": {}})
        record = current.get("sessions", {}).get(session_key, {}) if isinstance(current, dict) else {}
        if not isinstance(record, dict) or record.get("status") not in {
            "pending_next_user",
            "prepared",
        }:
            return {"sessionKey": session_key, "action": "no_pending_rollover"}
        entry = self._sessions().get(session_key)
        if not isinstance(entry, dict):
            raise RuntimeError("session_entry_missing")
        old_session_id = str(record.get("oldSessionId") or "")
        if not old_session_id or str(entry.get("sessionId") or "") != old_session_id:
            raise RuntimeError("pending_rollover_generation_mismatch")
        if not self._is_idle(entry):
            return {
                "sessionKey": session_key,
                "action": "pending_busy",
                "status": entry.get("status"),
            }
        handoff_path = Path(str(record.get("handoffPath") or ""))
        if not handoff_path.is_file():
            raise RuntimeError("pending_rollover_handoff_missing")
        expected_sha = str(record.get("handoffSha256") or "")
        if not expected_sha or sha256_file(handoff_path) != expected_sha:
            raise RuntimeError("pending_rollover_handoff_hash_mismatch")
        if dry_run:
            return {
                "sessionKey": session_key,
                "action": "would_activate_pending_rollover",
                "oldSessionId": old_session_id,
                "handoffPath": str(handoff_path),
            }
        entry, preferences_repaired_before_reset = self._ensure_preferences(session_key, spec, entry)
        desired_preferences = self._desired_preferences(spec, entry)
        manual_selection = self._manual_model_selection(entry)
        prepared = {
            **record,
            "status": "prepared",
            "sessionPreferences": desired_preferences,
            "manualModelSelection": manual_selection,
            "preferencesRepairedBeforeReset": preferences_repaired_before_reset,
            "activationTrigger": trigger_override,
            "activationStartedAt": now_iso(),
        }
        self._update_current(session_key, prepared)
        response = self._gateway_reset(session_key)
        refreshed = self._sessions().get(session_key, {})
        response_entry = response.get("entry") if isinstance(response.get("entry"), dict) else {}
        new_session_id = refreshed.get("sessionId") or response_entry.get("sessionId")
        if not new_session_id or str(new_session_id) == old_session_id:
            raise RuntimeError("gateway_reset_did_not_rotate_session_id")
        if refreshed.get("label", response_entry.get("label")) != entry.get("label"):
            raise RuntimeError("stable_label_not_preserved")
        refreshed, preferences_repaired_after_reset = self._ensure_preferences(
            session_key,
            spec,
            refreshed,
            desired=desired_preferences,
        )
        if self._manual_model_selection(refreshed) != manual_selection:
            raise RuntimeError("manual_model_selection_not_preserved")
        try:
            visibility_notice = self._ensure_visible_continuity(
                session_key,
                prepared,
                str(new_session_id),
            )
            if visibility_notice.get("status") == "pending_busy":
                visibility_notice = {
                    **visibility_notice,
                    "status": "deferred_until_first_dispatch_complete",
                    "deferredAt": now_iso(),
                }
        except Exception as exc:
            # The reset has already committed. Visibility is auxiliary and must
            # not consume the user's triggering message after that point.
            visibility_notice = {
                "status": "pending_retry",
                "lastError": str(exc),
                "lastAttemptAt": now_iso(),
            }
        active = self._activation_value(
            prepared={
                **prepared,
                "preferencesRepairedAfterReset": preferences_repaired_after_reset,
            },
            new_session_id=str(new_session_id),
            response=response,
            visibility_notice=visibility_notice,
        )
        self._update_current(session_key, active)
        event = {
            "ts": now_iso(),
            "event": "rollover_completed",
            "action": "pending_rollover_activated",
            "sessionKey": session_key,
            "label": spec["label"],
            "oldSessionId": old_session_id,
            "newSessionId": new_session_id,
            "tokensBefore": int(record.get("tokens") or entry.get("totalTokens") or 0),
            "transcriptBytesBefore": int(record.get("transcriptBytes") or 0),
            "trigger": trigger_override,
            "thinkingLevel": refreshed.get("thinkingLevel"),
            "fastMode": refreshed.get("fastMode"),
            "providerOverride": refreshed.get("providerOverride"),
            "modelOverride": refreshed.get("modelOverride"),
            "modelOverrideSource": refreshed.get("modelOverrideSource"),
            "handoffPath": str(handoff_path),
            "visibilityNotice": visibility_notice,
        }
        append_jsonl(self.events_path, event)
        return event

    def _reconcile_prepared(self, sessions: dict[str, Any]) -> list[dict[str, Any]]:
        """Recover a reset that committed before the manager could mark it active."""
        current = read_json(self.current_path, {"schemaVersion": 1, "sessions": {}})
        records = current.get("sessions", {}) if isinstance(current, dict) else {}
        recovered: list[dict[str, Any]] = []
        for session_key, value in list(records.items()):
            if not isinstance(value, dict) or value.get("status") != "prepared":
                continue
            entry = sessions.get(session_key)
            new_session_id = entry.get("sessionId") if isinstance(entry, dict) else None
            if not new_session_id or new_session_id == value.get("oldSessionId"):
                continue
            spec = self.config.get("sessions", {}).get(session_key)
            if not isinstance(spec, dict):
                continue
            try:
                entry, repaired = self._ensure_preferences(session_key, spec, entry)
                visibility_notice = self._ensure_visible_continuity(
                    session_key,
                    value,
                    str(new_session_id),
                )
                if visibility_notice.get("status") == "pending_busy":
                    visibility_notice = {
                        **visibility_notice,
                        "status": "deferred_until_first_dispatch_complete",
                        "deferredAt": now_iso(),
                    }
            except Exception as exc:
                visibility_notice = {
                    "status": "pending_retry",
                    "lastError": str(exc),
                    "lastAttemptAt": now_iso(),
                }
                event = {
                    "ts": now_iso(),
                    "event": "visibility_notice_retry_failed",
                    "sessionKey": session_key,
                    "oldSessionId": value.get("oldSessionId"),
                    "newSessionId": new_session_id,
                    "error": str(exc),
                }
                append_jsonl(self.events_path, event)
                recovered.append(event)
            active = self._activation_value(
                prepared={
                    **value,
                    "sessionPreferences": self._desired_preferences(spec, entry),
                    "preferencesRepaired": repaired,
                },
                new_session_id=str(new_session_id),
                verification="recovered_after_partial_commit",
                visibility_notice=visibility_notice,
            )
            self._update_current(session_key, active)
            event = {
                "ts": now_iso(),
                "event": "rollover_recovered",
                "sessionKey": session_key,
                "oldSessionId": value.get("oldSessionId"),
                "newSessionId": new_session_id,
                "handoffPath": value.get("handoffPath"),
            }
            append_jsonl(self.events_path, event)
            recovered.append(event)
        return recovered

    def record_first_dispatch_start(
        self,
        session_key: str,
        run_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark the first real post-threshold run; never reset from this hook."""
        with self._lock() as acquired:
            if not acquired:
                return {"ok": True, "sessionKey": session_key, "action": "lock_busy"}
            current = read_json(self.current_path, {"sessions": {}})
            record = current.get("sessions", {}).get(session_key, {}) if isinstance(current, dict) else {}
            if not isinstance(record, dict):
                return {"ok": True, "sessionKey": session_key, "action": "no_pending_rollover"}
            status = record.get("status")
            drain = record.get("drainRun") if isinstance(record.get("drainRun"), dict) else {}
            if status == "draining_current_run":
                action = (
                    "deferred_rollover_run_start_idempotent"
                    if drain.get("runId") == run_id
                    else "deferred_rollover_already_draining"
                )
                return {"ok": True, "sessionKey": session_key, "action": action}
            if status == "ready_after_run":
                return {"ok": True, "sessionKey": session_key, "action": "deferred_rollover_already_ready"}
            if status != "pending_next_user":
                return {"ok": True, "sessionKey": session_key, "action": "no_pending_rollover"}
            old_session_id = str(record.get("oldSessionId") or "")
            if session_id and old_session_id != str(session_id):
                raise RuntimeError("deferred_rollover_generation_mismatch")
            drain = {
                "runId": run_id,
                "sessionId": session_id or old_session_id,
                "startedAt": now_iso(),
            }
            self._update_current(session_key, {
                **record,
                "status": "draining_current_run",
                "drainRun": drain,
            })
            event = {
                "ts": now_iso(),
                "event": "deferred_rollover_run_started",
                "sessionKey": session_key,
                "oldSessionId": old_session_id,
                "runId": run_id,
            }
            append_jsonl(self.events_path, event)
            return {"ok": True, "action": "deferred_rollover_run_started", **event}

    def record_first_dispatch_end(
        self,
        session_key: str,
        run_id: str,
        *,
        success: bool,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Mark the boundary run finished; the idle scanner performs the reset later."""
        with self._lock() as acquired:
            if not acquired:
                return {"ok": True, "sessionKey": session_key, "action": "lock_busy"}
            current = read_json(self.current_path, {"sessions": {}})
            record = current.get("sessions", {}).get(session_key, {}) if isinstance(current, dict) else {}
            if not isinstance(record, dict):
                return {"ok": True, "sessionKey": session_key, "action": "no_draining_rollover"}
            drain = record.get("drainRun") if isinstance(record.get("drainRun"), dict) else {}
            if record.get("status") == "ready_after_run" and drain.get("runId") == run_id:
                return {
                    "ok": True,
                    "sessionKey": session_key,
                    "action": "deferred_rollover_run_end_idempotent",
                }
            if record.get("status") != "draining_current_run" or drain.get("runId") != run_id:
                return {"ok": True, "sessionKey": session_key, "action": "no_draining_rollover"}
            finished = {
                **drain,
                "status": "completed" if success else "failed",
                "success": success,
                "finishedAt": now_iso(),
            }
            if error_code:
                finished["errorCode"] = error_code[:120]
            self._update_current(session_key, {
                **record,
                "status": "ready_after_run",
                "drainRun": finished,
                "readyAt": now_iso(),
            })
            event = {
                "ts": now_iso(),
                "event": (
                    "deferred_rollover_run_completed"
                    if success
                    else "deferred_rollover_run_failed"
                ),
                "sessionKey": session_key,
                "oldSessionId": record.get("oldSessionId"),
                "runId": run_id,
                "success": success,
            }
            if error_code:
                event["errorCode"] = error_code[:120]
            append_jsonl(self.events_path, event)
            return {"ok": True, "action": event["event"], **event}

    def _rollover_unlocked(
        self,
        session_key: str,
        *,
        force: bool = False,
        dry_run: bool = False,
        trigger_override: str | None = None,
    ) -> dict[str, Any]:
        spec = self.config.get("sessions", {}).get(session_key)
        if not spec:
            raise RuntimeError("session_not_allowlisted")
        sessions = self._sessions()
        entry = sessions.get(session_key)
        if not isinstance(entry, dict):
            raise RuntimeError("session_entry_missing")
        tokens = int(entry.get("totalTokens") or 0)
        decision = self._decision(tokens, entry, spec)
        action = decision["action"]
        if not force and action not in {"rollover", "emergency"}:
            return {
                "sessionKey": session_key,
                "action": "skip_below_rollover",
                "tokens": tokens,
                **{key: value for key, value in decision.items() if key != "action"},
            }
        if not self._is_idle(entry):
            return {
                "sessionKey": session_key,
                "action": "pending_busy",
                "tokens": tokens,
                "status": entry.get("status"),
                **{key: value for key, value in decision.items() if key != "action"},
            }
        if dry_run:
            return {
                "sessionKey": session_key,
                "action": "would_rollover",
                "tokens": tokens,
                "sessionId": entry.get("sessionId"),
                "trigger": trigger_override or decision["reason"],
                **{key: value for key, value in decision.items() if key != "action"},
            }
        entry, preferences_repaired_before_reset = self._ensure_preferences(session_key, spec, entry)
        desired_preferences_before_reset = self._desired_preferences(spec, entry)
        manual_selection_before_reset = self._manual_model_selection(entry)
        handoff = self._handoff(session_key, spec, entry)
        old_session_id = entry["sessionId"]
        prepared = {
            "status": "prepared",
            "sessionKey": session_key,
            "oldSessionId": old_session_id,
            "label": spec["label"],
            "project": spec["project"],
            "handoffPath": handoff["handoffPath"],
            "continuityContext": handoff["continuityContext"],
            "handoffSha256": sha256_file(Path(handoff["handoffPath"])),
            "sessionPreferences": desired_preferences_before_reset,
            "manualModelSelection": manual_selection_before_reset,
            "preferencesRepairedBeforeReset": preferences_repaired_before_reset,
            "maxInjections": 3,
            "preparedAt": now_iso(),
        }
        self._update_current(session_key, prepared)
        response = self._gateway_reset(session_key)
        refreshed = self._sessions().get(session_key, {})
        response_entry = response.get("entry") if isinstance(response.get("entry"), dict) else {}
        new_session_id = refreshed.get("sessionId") or response_entry.get("sessionId")
        if not new_session_id or new_session_id == old_session_id:
            raise RuntimeError("gateway_reset_did_not_rotate_session_id")
        observed_label = refreshed.get("label", response_entry.get("label"))
        if observed_label != entry.get("label"):
            raise RuntimeError("stable_label_not_preserved")
        refreshed, preferences_repaired_after_reset = self._ensure_preferences(
            session_key,
            spec,
            refreshed,
            desired=desired_preferences_before_reset,
        )
        manual_selection_after_reset = self._manual_model_selection(refreshed)
        if manual_selection_after_reset != manual_selection_before_reset:
            raise RuntimeError("manual_model_selection_not_preserved")
        try:
            visibility_notice = self._ensure_visible_continuity(
                session_key,
                prepared,
                str(new_session_id),
            )
            if visibility_notice.get("status") == "pending_busy":
                raise RuntimeError("visibility_notice_pending_busy")
        except Exception as exc:
            self._update_current(session_key, {
                **prepared,
                "newSessionId": str(new_session_id),
                "visibilityNotice": {
                    "status": "pending_retry",
                    "lastError": str(exc),
                    "lastAttemptAt": now_iso(),
                },
            })
            raise RuntimeError(f"visibility_notice_failed:{exc}") from exc
        active = self._activation_value(
            prepared={
                **prepared,
                "preferencesRepairedAfterReset": preferences_repaired_after_reset,
            },
            new_session_id=str(new_session_id),
            response=response,
            visibility_notice=visibility_notice,
        )
        self._update_current(session_key, active)
        event = {
            "ts": now_iso(),
            "event": "rollover_completed",
            "sessionKey": session_key,
            "label": spec["label"],
            "oldSessionId": old_session_id,
            "newSessionId": new_session_id,
            "tokensBefore": tokens,
            "transcriptBytesBefore": decision["transcriptBytes"],
            "trigger": trigger_override or decision["reason"],
            "physicalAgeDaysBefore": decision["physicalAgeDays"],
            "thinkingLevel": refreshed.get("thinkingLevel"),
            "fastMode": refreshed.get("fastMode"),
            "providerOverride": refreshed.get("providerOverride"),
            "modelOverride": refreshed.get("modelOverride"),
            "modelOverrideSource": refreshed.get("modelOverrideSource"),
            "handoffPath": handoff["handoffPath"],
            "visibilityNotice": visibility_notice,
        }
        append_jsonl(self.events_path, event)
        return event

    def rollover(self, session_key: str, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        trigger_override = "manual_force" if force else None
        if dry_run:
            return self._rollover_unlocked(
                session_key,
                force=force,
                dry_run=True,
                trigger_override=trigger_override,
            )
        with self._lock() as acquired:
            if not acquired:
                return {"sessionKey": session_key, "action": "lock_busy"}
            return self._rollover_unlocked(
                session_key,
                force=force,
                dry_run=dry_run,
                trigger_override=trigger_override,
            )

    def activate_pending(self, session_key: str, *, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return self._activate_pending_unlocked(session_key, dry_run=True)
        with self._lock() as acquired:
            if not acquired:
                return {"sessionKey": session_key, "action": "lock_busy"}
            current = read_json(self.current_path, {"sessions": {}})
            record = current.get("sessions", {}).get(session_key, {}) if isinstance(current, dict) else {}
            if isinstance(record, dict) and record.get("status") == "prepared":
                sessions = self._sessions()
                entry = sessions.get(session_key, {})
                old_session_id = str(record.get("oldSessionId") or "")
                current_session_id = str(entry.get("sessionId") or "") if isinstance(entry, dict) else ""
                if current_session_id and current_session_id != old_session_id:
                    self._reconcile_prepared(sessions)
                    refreshed = read_json(self.current_path, {"sessions": {}})
                    refreshed_record = (
                        refreshed.get("sessions", {}).get(session_key, {})
                        if isinstance(refreshed, dict)
                        else {}
                    )
                    if isinstance(refreshed_record, dict) and refreshed_record.get("status") == "active":
                        return {
                            "sessionKey": session_key,
                            "action": "pending_rollover_reconciled",
                            "oldSessionId": old_session_id,
                            "newSessionId": current_session_id,
                        }
                    raise RuntimeError("pending_rollover_reconcile_failed")
            return self._activate_pending_unlocked(session_key)

    def repair_visibility(
        self,
        session_key: str,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            current = read_json(self.current_path, {"sessions": {}})
            record = current.get("sessions", {}).get(session_key, {}) if isinstance(current, dict) else {}
            if not isinstance(record, dict) or record.get("status") not in {"active", "prepared"}:
                raise RuntimeError("visibility_rollover_record_missing")
            new_session_id = str(record.get("newSessionId") or "")
            if not new_session_id:
                raise RuntimeError("visibility_new_session_id_missing")
            result = self._ensure_visible_continuity(
                session_key,
                record,
                new_session_id,
                force=force,
                dry_run=dry_run,
            )
            if dry_run or result.get("status") in {"disabled", "pending_busy", "would_inject"}:
                return {"sessionKey": session_key, **result}
            self._update_current(session_key, {**record, "visibilityNotice": result})
            event = {
                "ts": now_iso(),
                "event": "visibility_notice_repaired",
                "sessionKey": session_key,
                "oldSessionId": record.get("oldSessionId"),
                "newSessionId": new_session_id,
                "visibilityNotice": result,
            }
            append_jsonl(self.events_path, event)
            return event

        if dry_run:
            return run()
        with self._lock() as acquired:
            if not acquired:
                return {"sessionKey": session_key, "action": "lock_busy"}
            return run()

    def _repair_existing_visibility_notices(self, *, dry_run: bool) -> list[dict[str, Any]]:
        config = self._visible_continuity_config()
        if not config["enabled"]:
            return []
        current = read_json(self.current_path, {"sessions": {}})
        records = current.get("sessions", {}) if isinstance(current, dict) else {}
        results: list[dict[str, Any]] = []
        for session_key in config["repairExistingSessionKeys"]:
            record = records.get(session_key)
            if not isinstance(record, dict) or record.get("status") != "active":
                results.append({
                    "sessionKey": session_key,
                    "status": "rollover_record_missing",
                })
                continue
            visibility = record.get("visibilityNotice")
            if isinstance(visibility, dict) and visibility.get("status") == "verified":
                results.append({
                    "sessionKey": session_key,
                    "status": "already_verified",
                })
                continue
            new_session_id = str(record.get("newSessionId") or "")
            if not new_session_id:
                results.append({
                    "sessionKey": session_key,
                    "status": "new_session_id_missing",
                })
                continue
            try:
                result = self._ensure_visible_continuity(
                    session_key,
                    record,
                    new_session_id,
                    dry_run=dry_run,
                )
                output = {"sessionKey": session_key, **result}
                results.append(output)
                if dry_run or result.get("status") != "verified":
                    continue
                self._update_current(session_key, {**record, "visibilityNotice": result})
                append_jsonl(self.events_path, {
                    "ts": now_iso(),
                    "event": "visibility_notice_auto_repaired",
                    "sessionKey": session_key,
                    "oldSessionId": record.get("oldSessionId"),
                    "newSessionId": new_session_id,
                    "visibilityNotice": result,
                })
            except Exception as exc:
                error = {
                    "sessionKey": session_key,
                    "status": "repair_error",
                    "error": str(exc),
                }
                results.append(error)
                if not dry_run:
                    append_jsonl(self.events_path, {
                        "ts": now_iso(),
                        "event": "visibility_notice_auto_repair_failed",
                        **error,
                    })
        return results

    def _repair_deferred_visibility_notices(self, *, dry_run: bool) -> list[dict[str, Any]]:
        """Surface the old outcome only when the first new-generation run did not succeed."""
        config = self._visible_continuity_config()
        if not config["enabled"]:
            return []
        current = read_json(self.current_path, {"sessions": {}})
        records = current.get("sessions", {}) if isinstance(current, dict) else {}
        results: list[dict[str, Any]] = []
        for session_key, record in records.items():
            if not isinstance(record, dict) or record.get("status") != "active":
                continue
            visibility = record.get("visibilityNotice")
            first = record.get("firstDispatch")
            if not isinstance(visibility, dict) or not isinstance(first, dict):
                continue
            first_status = first.get("status")
            start_age = iso_age_seconds(first.get("armedAt"))
            start_timed_out = (
                first_status == "awaiting_agent_start"
                and start_age is not None
                and start_age >= int(config["firstDispatchStartTimeoutSeconds"])
            )
            if first_status != "failed" and not start_timed_out:
                continue
            if start_timed_out:
                timeout_result = {
                    "sessionKey": session_key,
                    "status": "start_timeout",
                    "firstDispatchStatus": first_status,
                    "startTimedOut": True,
                    "visibilityStatus": visibility.get("status"),
                }
                results.append(timeout_result)
                if not dry_run:
                    updated_first = {
                        **first,
                        "status": "start_timeout",
                        "startTimeoutDetectedAt": now_iso(),
                    }
                    self._update_current(session_key, {
                        **record,
                        "firstDispatch": updated_first,
                    })
                    append_jsonl(self.events_path, {
                        "ts": now_iso(),
                        "event": "first_dispatch_start_timed_out",
                        "sessionKey": session_key,
                        "newSessionId": record.get("newSessionId"),
                        "visibilityStatus": visibility.get("status"),
                    })
                    first = updated_first
                if visibility.get("status") == "verified":
                    continue
            if visibility.get("status") not in {
                "deferred_until_first_dispatch_complete",
                "pending_retry",
            }:
                continue
            new_session_id = str(record.get("newSessionId") or "")
            if not new_session_id:
                continue
            try:
                result = self._ensure_visible_continuity(
                    session_key,
                    record,
                    new_session_id,
                    dry_run=dry_run,
                )
                output = {
                    "sessionKey": session_key,
                    "firstDispatchStatus": first_status,
                    "startTimedOut": start_timed_out,
                    **result,
                }
                if not start_timed_out:
                    results.append(output)
                if dry_run or result.get("status") != "verified":
                    continue
                updated_first = {
                    **first,
                    "continuityNoticeVerifiedAt": now_iso(),
                }
                self._update_current(session_key, {
                    **record,
                    "firstDispatch": updated_first,
                    "visibilityNotice": result,
                })
                append_jsonl(self.events_path, {
                    "ts": now_iso(),
                    "event": "first_dispatch_recovery_notice_verified",
                    "sessionKey": session_key,
                    "newSessionId": new_session_id,
                    "firstDispatchStatus": first_status,
                    "startTimedOut": start_timed_out,
                })
            except Exception as exc:
                error = {
                    "sessionKey": session_key,
                    "status": "repair_error",
                    "error": str(exc),
                }
                results.append(error)
                if not dry_run:
                    append_jsonl(self.events_path, {
                        "ts": now_iso(),
                        "event": "first_dispatch_recovery_notice_failed",
                        **error,
                    })
        return results

    def scan(self, *, dry_run: bool = False) -> dict[str, Any]:
        if not self.config.get("enabled", False):
            return {"ok": True, "enabled": False, "results": []}
        if dry_run:
            return self._scan_unlocked(dry_run=True)
        with self._lock() as acquired:
            if not acquired:
                return {"ok": True, "skipped": "lock_busy", "results": []}
            return self._scan_unlocked(dry_run=False)

    def _scan_unlocked(self, *, dry_run: bool) -> dict[str, Any]:
        sessions = self._sessions()
        recovered = [] if dry_run else self._reconcile_prepared(sessions)
        visibility_repairs = self._repair_existing_visibility_notices(dry_run=dry_run)
        deferred_visibility_repairs = self._repair_deferred_visibility_notices(dry_run=dry_run)
        results = []
        for session_key, spec in self.config.get("sessions", {}).items():
                entry = sessions.get(session_key)
                if not isinstance(entry, dict):
                    results.append({"sessionKey": session_key, "action": "missing"})
                    continue
                try:
                    retired_binding = self._retired_codex_binding(session_key, entry)
                except Exception as exc:
                    error = {
                        "sessionKey": session_key,
                        "action": "binding_state_error",
                        "error": str(exc),
                    }
                    results.append(error)
                    if not dry_run:
                        append_jsonl(self.events_path, {"ts": now_iso(), "event": "binding_state_failed", **error})
                    continue
                if retired_binding:
                    if not self._is_idle(entry):
                        results.append({
                            "sessionKey": session_key,
                            "action": "pending_busy_retired_binding",
                            "sessionId": entry.get("sessionId"),
                            "status": entry.get("status"),
                        })
                        continue
                    if dry_run:
                        results.append({
                            "sessionKey": session_key,
                            "action": "would_recover_retired_binding",
                            "sessionId": entry.get("sessionId"),
                            "bindingCreatedAtMs": retired_binding["createdAtMs"],
                        })
                        continue
                    try:
                        result = self._rollover_unlocked(
                            session_key,
                            force=True,
                            trigger_override="codex_binding_generation_retired",
                        )
                        results.append({**result, "recoveredBinding": retired_binding["bindingKey"]})
                    except Exception as exc:
                        error = {
                            "sessionKey": session_key,
                            "action": "binding_recovery_error",
                            "error": str(exc),
                        }
                        results.append(error)
                        append_jsonl(self.events_path, {"ts": now_iso(), "event": "binding_recovery_failed", **error})
                    continue
                preferences_repaired = False
                if not dry_run:
                    try:
                        entry, preferences_repaired = self._ensure_preferences(session_key, spec, entry)
                        if preferences_repaired:
                            sessions[session_key] = entry
                    except Exception as exc:
                        error = {
                            "sessionKey": session_key,
                            "action": "preferences_error",
                            "error": str(exc),
                        }
                        results.append(error)
                        append_jsonl(self.events_path, {"ts": now_iso(), "event": "preferences_failed", **error})
                        continue
                elif not self._preferences_match(entry, self._desired_preferences(spec, entry)):
                    results.append({
                        "sessionKey": session_key,
                        "action": "would_repair_preferences",
                        "tokens": int(entry.get("totalTokens") or 0),
                    })
                    continue
                tokens = int(entry.get("totalTokens") or 0)
                decision = self._decision(tokens, entry, spec)
                action = decision["action"]
                current = read_json(self.current_path, {"sessions": {}})
                current_record = current.get("sessions", {}).get(session_key, {}) if isinstance(current, dict) else {}
                deferred_statuses = {
                    "pending_next_user",
                    "draining_current_run",
                    "ready_after_run",
                }
                deferred_current_generation = (
                    isinstance(current_record, dict)
                    and current_record.get("status") in deferred_statuses
                    and current_record.get("oldSessionId") == entry.get("sessionId")
                )
                if (
                    isinstance(current_record, dict)
                    and current_record.get("status") in deferred_statuses
                    and not deferred_current_generation
                    and not dry_run
                ):
                    self._update_current(session_key, {
                        **current_record,
                        "status": "superseded",
                        "supersededAt": now_iso(),
                        "observedSessionId": entry.get("sessionId"),
                    })
                if deferred_current_generation:
                    deferred_status = current_record.get("status")
                    if deferred_status == "pending_next_user":
                        if action != "emergency":
                            results.append({
                                "sessionKey": session_key,
                                "action": "rollover_awaiting_agent_run",
                                "tokens": tokens,
                                "status": entry.get("status"),
                            })
                            continue
                    elif deferred_status == "draining_current_run":
                        if not self._is_idle(entry):
                            results.append({
                                "sessionKey": session_key,
                                "action": "rollover_run_draining",
                                "tokens": tokens,
                                "status": entry.get("status"),
                                "runId": (current_record.get("drainRun") or {}).get("runId"),
                            })
                            continue
                        if dry_run:
                            results.append({
                                "sessionKey": session_key,
                                "action": "would_reconcile_rollover_run_end",
                                "tokens": tokens,
                            })
                            continue
                        drain = current_record.get("drainRun") or {}
                        current_record = {
                            **current_record,
                            "status": "ready_after_run",
                            "drainRun": {
                                **drain,
                                "status": "lifecycle_reconciled_idle",
                                "finishedAt": now_iso(),
                            },
                            "readyAt": now_iso(),
                        }
                        self._update_current(session_key, current_record)
                    if deferred_status in {"ready_after_run", "draining_current_run"}:
                        if not self._is_idle(entry):
                            results.append({
                                "sessionKey": session_key,
                                "action": "post_run_rollover_waiting_idle",
                                "tokens": tokens,
                                "status": entry.get("status"),
                            })
                            continue
                        try:
                            results.append(self._rollover_unlocked(
                                session_key,
                                force=True,
                                dry_run=dry_run,
                                trigger_override="post_run_idle",
                            ))
                        except Exception as exc:
                            error = {
                                "sessionKey": session_key,
                                "action": "post_run_rollover_error",
                                "error": str(exc),
                                "tokens": tokens,
                            }
                            results.append(error)
                            if not dry_run:
                                append_jsonl(self.events_path, {
                                    "ts": now_iso(),
                                    "event": "post_run_rollover_failed",
                                    **error,
                                })
                        continue
                if action == "emergency":
                    try:
                        results.append(self._rollover_unlocked(
                            session_key,
                            force=deferred_current_generation,
                            dry_run=dry_run,
                            trigger_override=decision["reason"],
                        ))
                    except Exception as exc:
                        error = {"sessionKey": session_key, "action": "error", "error": str(exc), "tokens": tokens}
                        results.append(error)
                        if not dry_run:
                            append_jsonl(self.events_path, {"ts": now_iso(), "event": "rollover_failed", **error})
                elif action == "rollover":
                    try:
                        if self._rollover_timing_config()["deferUntilNextUserMessage"]:
                            results.append(self._arm_rollover_unlocked(session_key, dry_run=dry_run))
                        else:
                            results.append(self._rollover_unlocked(session_key, dry_run=dry_run))
                    except Exception as exc:
                        error = {"sessionKey": session_key, "action": "error", "error": str(exc), "tokens": tokens}
                        results.append(error)
                        if not dry_run:
                            append_jsonl(self.events_path, {"ts": now_iso(), "event": "rollover_failed", **error})
                elif action == "checkpoint":
                    if dry_run:
                        results.append({
                            "sessionKey": session_key,
                            "action": "would_checkpoint",
                            "tokens": tokens,
                            **{key: value for key, value in decision.items() if key != "action"},
                        })
                        continue
                    current = read_json(self.current_path, {"sessions": {}}).get("sessions", {}).get(session_key, {})
                    if current.get("oldSessionId") != entry.get("sessionId") or int(current.get("tokens", 0)) + 20000 <= tokens:
                        try:
                            handoff = self._handoff(session_key, spec, entry)
                            value = {
                                "status": "checkpoint",
                                "sessionKey": session_key,
                                "oldSessionId": entry.get("sessionId"),
                                "label": spec["label"],
                                "project": spec["project"],
                                "tokens": tokens,
                                "handoffPath": handoff["handoffPath"],
                                "continuityContext": handoff["continuityContext"],
                                "preparedAt": now_iso(),
                            }
                            if not dry_run:
                                self._update_current(session_key, value)
                            results.append({
                                "sessionKey": session_key,
                                "action": "checkpoint_created",
                                "tokens": tokens,
                                **{key: value for key, value in decision.items() if key != "action"},
                            })
                        except Exception as exc:
                            results.append({"sessionKey": session_key, "action": "checkpoint_error", "error": str(exc), "tokens": tokens})
                    else:
                        results.append({
                            "sessionKey": session_key,
                            "action": "checkpoint_current",
                            "tokens": tokens,
                            **{key: value for key, value in decision.items() if key != "action"},
                        })
                else:
                    results.append({
                        "sessionKey": session_key,
                        "action": "preferences_repaired" if preferences_repaired else "healthy",
                        "tokens": tokens,
                        **{key: value for key, value in decision.items() if key != "action"},
                    })
        payload = {
            "ok": not any(str(item.get("action", "")).endswith("error") for item in results),
            "checkedAt": now_iso(),
            "dryRun": dry_run,
            "recovered": recovered,
            "visibilityRepairs": visibility_repairs,
            "deferredVisibilityRepairs": deferred_visibility_repairs,
            "results": results,
        }
        if not dry_run:
            atomic_write_json(self.status_path, payload)
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--dry-run", action="store_true")
    rollover = sub.add_parser("rollover")
    rollover.add_argument("--session-key", required=True)
    rollover.add_argument("--force", action="store_true")
    rollover.add_argument("--dry-run", action="store_true")
    activate = sub.add_parser("activate-pending")
    activate.add_argument("--session-key", required=True)
    activate.add_argument("--dry-run", action="store_true")
    dispatch_start = sub.add_parser("record-first-dispatch-start")
    dispatch_start.add_argument("--session-key", required=True)
    dispatch_start.add_argument("--run-id", required=True)
    dispatch_start.add_argument("--session-id")
    dispatch_end = sub.add_parser("record-first-dispatch-end")
    dispatch_end.add_argument("--session-key", required=True)
    dispatch_end.add_argument("--run-id", required=True)
    dispatch_end.add_argument("--success", choices=("true", "false"), required=True)
    dispatch_end.add_argument("--error-code")
    visibility = sub.add_parser("repair-visibility")
    visibility.add_argument("--session-key", required=True)
    visibility.add_argument("--force", action="store_true")
    visibility.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args()
    manager = RolloverManager(args.config)
    try:
        if args.command == "scan":
            payload = manager.scan(dry_run=args.dry_run)
        elif args.command == "rollover":
            payload = manager.rollover(args.session_key, force=args.force, dry_run=args.dry_run)
        elif args.command == "activate-pending":
            payload = manager.activate_pending(args.session_key, dry_run=args.dry_run)
        elif args.command == "record-first-dispatch-start":
            payload = manager.record_first_dispatch_start(
                args.session_key,
                args.run_id,
                args.session_id,
            )
        elif args.command == "record-first-dispatch-end":
            payload = manager.record_first_dispatch_end(
                args.session_key,
                args.run_id,
                success=args.success == "true",
                error_code=args.error_code,
            )
        elif args.command == "repair-visibility":
            payload = manager.repair_visibility(
                args.session_key,
                force=args.force,
                dry_run=args.dry_run,
            )
        else:
            payload = read_json(manager.status_path, {"ok": True, "status": "not_run"})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok", True) else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
