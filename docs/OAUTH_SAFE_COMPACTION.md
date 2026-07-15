# OAuth-safe deterministic compaction

## Problem

An OpenClaw session may authenticate to a hosted Codex runtime with OAuth while a built-in summarizer expects an OpenAI API-key profile. Sending that OAuth profile to an API-key-only compaction path fails before the summary is produced. A very large transcript can then remain mapped to the same session and repeatedly hit the context limit.

There are three possible LLM call sites to account for:

1. the main compaction summary provider;
2. the pre-compaction memory flush;
3. the safeguard summary quality audit.

Replacing only the first call site is insufficient.

## Safe configuration

Use `openclaw-session-keeper-deterministic` as the provider and disable both auxiliary model call sites:

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "provider": "openclaw-session-keeper-deterministic",
        "reserveTokensFloor": 100000,
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

The provider performs a bounded one-pass extraction of recent user goals, assistant outcomes, failures and opaque references. Common credential patterns are redacted before output, but no pattern-based filter is perfect: protect the resulting summary as sensitive session data. The provider does not call a model, read provider credentials or log transcript content.

## Production rollout

1. Back up the active OpenClaw configuration and session index to an owner-only directory.
2. Install from a reviewed clean checkout with `openclaw plugins install .`.
3. Apply the safe configuration and run `openclaw config validate`.
4. Restart the Gateway once and verify its PID remains stable.
5. Confirm the plugin is loaded and the configured provider id resolves.
6. Create a disposable session that retains the real OAuth profile, inject synthetic messages, and run `openclaw sessions compact <key> --json`.
7. Confirm compaction succeeds without adding or switching to an API key.
8. Delete the disposable session and run the repository secret gates.

The release was validated against OpenClaw `2026.7.1` with a disposable hosted-Codex OAuth session: the deterministic provider completed `/compact`, the same stable session key continued on its original model, and no API-key profile or fallback was used.

## Emergency recovery

For an already oversized inactive session, `emergency_recovery.py`:

1. verifies that the Gateway reports no active run;
2. validates the transcript chain;
3. copies the transcript and session entry to an owner-only recovery directory and verifies SHA-256;
4. calls `openclaw sessions compact <key> --max-lines N --json`;
5. reloads the Gateway-owned state and verifies that line count and bytes decreased;
6. writes a metadata-only manifest.

It deliberately does not rewrite the live transcript or `sessions.json`. Direct writes race the Gateway and can corrupt lifecycle state.

The recovery CLI currently targets POSIX systems because it uses an advisory `flock`. Its configuration, session index and transcript must be regular files owned by the current user and must not be group- or world-writable. A stale stored `running` value is reported as `storedStatusStale`; the Gateway runtime status remains authoritative and the tool never patches private OpenClaw state directly.

## Rollback

Restore the previous OpenClaw configuration, disable the plugin entry, restart the Gateway once, and verify health. Recovery backups contain private transcripts and must never be attached to issues or committed to Git.

If this provider is not installed and selected, do not run model-based `/compact` on an OAuth session until the active OpenClaw version is known to route that authentication profile to a compatible summarizer. With the provider selected and both auxiliary call sites disabled, `/compact` follows the deterministic path documented above.
