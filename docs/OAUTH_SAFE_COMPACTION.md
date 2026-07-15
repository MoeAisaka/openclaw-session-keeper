# OAuth-safe compaction compatibility

## Failure model

OpenClaw can authenticate a hosted Codex runtime with OAuth while its local transcript fallback resolves an `openai-responses` summarizer that accepts API-key profiles only. On OpenClaw `2026.7.1`, native Codex app-server sessions own manual compaction and ignore custom compaction provider overrides. During automatic pre-turn compaction the native backend can decline the non-manual request, after which the local fallback resolves authentication before an extension provider can run. An OAuth profile then fails with an API-key requirement.

The failure is an ordering and compatibility issue in the host lifecycle. Selecting `openclaw-session-keeper-deterministic` globally does not fix native hosted-Codex sessions and produces an explicit "compaction overrides ignored" warning.

## Safe hosted-Codex configuration

Do not set `agents.defaults.compaction.provider` globally when native hosted-Codex OAuth sessions are present. Keep auxiliary model calls disabled and leave enough space for physical rollover to win before the automatic pre-turn budget is reached:

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "reserveTokensFloor": 50000,
        "keepRecentTokens": 30000,
        "maxActiveTranscriptBytes": "16mb",
        "truncateAfterCompaction": true,
        "memoryFlush": { "enabled": false },
        "qualityGuard": { "enabled": false }
      }
    }
  }
}
```

For every managed hosted-Codex session:

```text
contextTokens - max(reserveTokens, reserveTokensFloor) - rolloverTokens >= 50000
```

This is a scheduling margin, not additional model capacity. A larger incoming prompt or system prompt can consume it, so review the result after model-window or system-prompt changes.

Run the read-only check before deployment:

```bash
python3 compatibility_check.py \
  --openclaw-config "$HOME/.openclaw/openclaw.json" \
  --keeper-config "$HOME/.config/openclaw-session-keeper/config.json" \
  --json
```

The checker reports counts and thresholds but never returns auth-profile values.

## Deterministic provider boundary

The plugin still registers `openclaw-session-keeper-deterministic`. It performs a bounded one-pass extraction of recent user goals, outcomes, failures and opaque references without calling a model or reading provider credentials. Use it only with an embedded or non-native runtime that honors `registerCompactionProvider`, and validate the exact OpenClaw version with a disposable session before selection.

Pattern-based redaction is best effort. Protect every resulting summary as sensitive session data.

## Production rollout

1. Back up the active OpenClaw configuration, Keeper configuration and session index to an owner-only directory.
2. Run `compatibility_check.py` against the live files and review every finding.
3. Apply the hosted-Codex configuration and safe rollover thresholds.
4. Run `openclaw config validate` and Keeper `scan --dry-run`.
5. Restart the Gateway once; confirm health, plugin loading and stable process state.
6. Verify no new API-key compaction error or ignored-provider warning appears.
7. Run a normal turn in a disposable OAuth session. Manual compaction, if tested, must remain on the native Codex path.
8. Run the repository tests and secret gates.

## Emergency recovery

For an already oversized inactive session, `emergency_recovery.py` verifies idle state, backs up the transcript and session entry, verifies SHA-256, and calls the Gateway-owned `sessions.compact --max-lines` lifecycle API. It never rewrites an active transcript or `sessions.json` directly.

The recovery CLI targets POSIX systems because it uses an advisory `flock`. Its inputs must be regular files owned by the current user and not writable by group or others. A stale stored `running` value is reported as `storedStatusStale`; the Gateway runtime remains authoritative.

## Rollback

Restore the previous OpenClaw and Keeper configurations, restore the previous plugin version if it changed, restart the Gateway once, and verify health. Recovery backups contain private transcripts and must never be attached to issues or committed to Git.
