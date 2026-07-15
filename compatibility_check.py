#!/usr/bin/env python3
"""Read-only compatibility checks for OpenClaw compaction and Keeper rollover."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


VERSION = "0.2.1"
DETERMINISTIC_PROVIDER = "openclaw-session-keeper-deterministic"
DEFAULT_NATIVE_CODEX_RESERVE_TOKENS = 20_000
DEFAULT_SAFETY_MARGIN_TOKENS = 50_000


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected_object:{path}")
    return value


def nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def positive_integer(value: Any) -> int | None:
    parsed = nonnegative_integer(value)
    return parsed if parsed is not None and parsed > 0 else None


def session_entries(store: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if "sessions" in store:
        raw = store.get("sessions") if isinstance(store.get("sessions"), dict) else {}
    else:
        raw = store
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, dict)}


def is_openai_oauth_session(entry: dict[str, Any]) -> bool:
    provider = str(entry.get("modelProvider") or entry.get("providerOverride") or "").lower()
    return provider == "openai" and bool(str(entry.get("authProfileOverride") or "").strip())


def compaction_settings(openclaw_config: dict[str, Any]) -> dict[str, Any]:
    agents = openclaw_config.get("agents") if isinstance(openclaw_config.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    value = defaults.get("compaction")
    return value if isinstance(value, dict) else {}


def effective_reserve_tokens(compaction: dict[str, Any]) -> int:
    reserve_tokens = nonnegative_integer(compaction.get("reserveTokens"))
    reserve_floor = nonnegative_integer(compaction.get("reserveTokensFloor"))
    if reserve_tokens is not None:
        return max(
            reserve_tokens,
            reserve_floor if reserve_floor is not None else DEFAULT_NATIVE_CODEX_RESERVE_TOKENS,
        )
    return reserve_floor if reserve_floor is not None else DEFAULT_NATIVE_CODEX_RESERVE_TOKENS


def session_rollover_tokens(keeper_config: dict[str, Any], session_key: str) -> int | None:
    global_thresholds = keeper_config.get("thresholds")
    global_thresholds = global_thresholds if isinstance(global_thresholds, dict) else {}
    sessions = keeper_config.get("sessions")
    sessions = sessions if isinstance(sessions, dict) else {}
    spec = sessions.get(session_key)
    spec = spec if isinstance(spec, dict) else {}
    local_thresholds = spec.get("thresholds")
    local_thresholds = local_thresholds if isinstance(local_thresholds, dict) else {}
    return positive_integer(local_thresholds.get("rolloverTokens") or global_thresholds.get("rolloverTokens"))


def analyze_compatibility(
    openclaw_config: dict[str, Any],
    keeper_config: dict[str, Any],
    sessions_store: dict[str, Any],
    *,
    safety_margin_tokens: int = DEFAULT_SAFETY_MARGIN_TOKENS,
) -> dict[str, Any]:
    compaction = compaction_settings(openclaw_config)
    provider = str(compaction.get("provider") or "").strip()
    reserve_tokens = effective_reserve_tokens(compaction)
    entries = session_entries(sessions_store)
    managed = keeper_config.get("sessions")
    managed = managed if isinstance(managed, dict) else {}
    oauth_session_keys = sorted(key for key, entry in entries.items() if is_openai_oauth_session(entry))
    findings: list[dict[str, Any]] = []

    if provider == DETERMINISTIC_PROVIDER and oauth_session_keys:
        findings.append({
            "severity": "error",
            "code": "native_codex_compaction_override_ignored",
            "message": (
                "OpenClaw 2026.7.1 native hosted-Codex sessions ignore custom compaction "
                "providers; remove the global provider override and use native compaction plus "
                "preemptive physical rollover."
            ),
            "affectedSessionCount": len(oauth_session_keys),
        })

    for session_key in sorted(managed):
        entry = entries.get(session_key)
        if not entry:
            findings.append({
                "severity": "warning",
                "code": "managed_session_state_missing",
                "message": (
                    "The managed session is absent from the session store, so hosted-Codex "
                    "compaction headroom cannot be verified."
                ),
                "sessionKey": session_key,
            })
            continue
        if not is_openai_oauth_session(entry):
            continue
        context_tokens = positive_integer(entry.get("contextTokens"))
        rollover_tokens = session_rollover_tokens(keeper_config, session_key)
        missing_fields = []
        if context_tokens is None:
            missing_fields.append("contextTokens")
        if rollover_tokens is None:
            missing_fields.append("rolloverTokens")
        if missing_fields:
            findings.append({
                "severity": "warning",
                "code": "compaction_headroom_input_missing",
                "message": (
                    "Hosted-Codex compaction headroom cannot be verified because required "
                    "numeric inputs are missing or invalid."
                ),
                "sessionKey": session_key,
                "missingFields": missing_fields,
            })
            continue
        prompt_budget = max(1, context_tokens - reserve_tokens)
        headroom = prompt_budget - rollover_tokens
        if headroom < safety_margin_tokens:
            findings.append({
                "severity": "warning",
                "code": "compaction_rollover_race_headroom",
                "message": (
                    "The automatic compaction budget is too close to the physical rollover "
                    "threshold; a new prompt can win the race before the periodic scanner."
                ),
                "sessionKey": session_key,
                "contextTokens": context_tokens,
                "effectiveReserveTokens": reserve_tokens,
                "rolloverTokens": rollover_tokens,
                "headroomTokens": headroom,
                "requiredHeadroomTokens": safety_margin_tokens,
            })

    return {
        "version": VERSION,
        "ok": not findings,
        "findingCount": len(findings),
        "nativeOpenAiOauthSessionCount": len(oauth_session_keys),
        "effectiveReserveTokens": reserve_tokens,
        "findings": findings,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openclaw-config", required=True, type=Path)
    parser.add_argument("--keeper-config", required=True, type=Path)
    parser.add_argument("--sessions-store", type=Path)
    parser.add_argument("--safety-margin-tokens", type=int, default=DEFAULT_SAFETY_MARGIN_TOKENS)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.safety_margin_tokens < 0:
        raise ValueError("safety_margin_tokens_must_be_non_negative")
    keeper_config = load_json(args.keeper_config)
    sessions_path = args.sessions_store
    if sessions_path is None:
        configured_path = keeper_config.get("sessionsStorePath")
        if not isinstance(configured_path, str) or not configured_path.strip():
            raise ValueError("sessions_store_path_missing")
        sessions_path = Path(configured_path).expanduser()
    result = analyze_compatibility(
        load_json(args.openclaw_config),
        keeper_config,
        load_json(sessions_path),
        safety_margin_tokens=args.safety_margin_tokens,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print("compatibility-check: ok")
    else:
        for finding in result["findings"]:
            print(f"{finding['severity']}: {finding['code']}: {finding['message']}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"compatibility-check: {error}", file=sys.stderr)
        raise SystemExit(1)
