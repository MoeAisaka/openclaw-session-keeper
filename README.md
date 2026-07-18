# OpenClaw Session Keeper

Safe physical-session rollover behind stable OpenClaw project session keys.

Long-running agent sessions eventually become expensive, brittle, or difficult to recover. Session Keeper creates a verified handoff, rotates the physical session only while it is idle, and keeps the stable project entry intact.

Version 0.3 can defer normal physical rollover until the next user message: the scanner prepares a verified handoff at the threshold, then an awaited pre-dispatch hook rotates the physical session before that message is sent to the agent. The completed answer therefore remains visible for review. Emergency rollover is still immediate. Version 0.2 also provides an auth-independent deterministic compaction provider for compatible embedded runtimes and keeps emergency recovery inside OpenClaw's Gateway lifecycle lock. Native hosted-Codex sessions require the compatibility policy below because OpenClaw `2026.7.1` owns their compaction lifecycle.

## Measured cost model

The repository includes a provider-configurable estimator for repeated context
tokens and long-context price tiers. In the documented 40-turn reference
scenario, Keeper reduces processed tokens by **36.4%** and estimated GPT-5.6
Sol Codex credits by **36.0%**, including the uncached cold-start cost after
rollover. This is a reproducible scenario, not a universal savings claim.

```bash
python3 cost_estimator.py
python3 cost_estimator.py --json
```

See [Token and tiered-pricing impact](docs/COST_MODEL.md) for assumptions,
formulas, caveats, and official pricing sources.

## What it preserves

- Stable `sessionKey` and project label
- A bounded, checksummed handoff package
- Recent visible user/assistant context without tool payloads
- Active workflow references and optional nmem recall
- User-selected provider/model overrides
- Thinking preferences and, when explicitly enabled, the user's current Standard/Fast choice
- An idempotent, operator-visible rollover notice
- Optional deferred normal rollover that activates before the next inbound task is dispatched

## Safety model

- Running sessions are never reset.
- Missing transcripts, failed backups, failed nmem checks, failed Gateway verification, or preference drift stop the rollover.
- State and handoff files are written atomically with owner-only permissions.
- Real configuration, transcripts, logs, databases, credentials and archives are denied by `.gitignore` and the repository secret gate.
- The optional Codex binding check opens the configured SQLite database read-only.
- Deterministic compaction uses bounded one-pass retention instead of copying the full normalized transcript.
- Emergency recovery backs up first, then calls the official `sessions.compact --max-lines` Gateway RPC. It never rewrites an active transcript or `sessions.json` directly.
- Deferred activation is awaited and fail closed: if reset verification fails, the inbound task is not executed and can be retried safely.
- The deferred hook never logs or persists the inbound prompt.

## Requirements

- Python 3.10+
- OpenClaw with `gateway call sessions.reset`, `sessions.patch`, `chat.history`, and `chat.inject`
- macOS or Linux with `fcntl`
- Optional: nmem for semantic recall
- Optional: Codex app-server state inspection

Tested against the stable OpenClaw `2026.7.1` release. Internal Gateway APIs can change; review the compatibility notes before upgrading OpenClaw.

## Hosted-Codex OAuth compatibility

Install the local plugin from a clean checkout:

```bash
openclaw plugins install .
```

OpenClaw `2026.7.1` native hosted-Codex sessions ignore custom compaction provider overrides. Do not select this plugin globally for those sessions. Let Codex own manual compaction, disable auxiliary model calls, and keep periodic physical rollover far enough below the automatic compaction budget:

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

For each managed hosted-Codex session, keep at least 50,000 tokens between the automatic prompt budget and Keeper's rollover threshold:

```text
contextTokens - max(reserveTokens, reserveTokensFloor) - rolloverTokens >= 50000
```

The margin allows the once-per-minute scanner to rotate the physical session before a new prompt enters the incompatible local fallback path. `memoryFlush` and the safeguard quality guard can make auxiliary LLM calls, so disable both unless they are explicitly routed to a compatible model. If nmem or another memory plugin already handles durable memory, disabling OpenClaw's model-based memory flush also avoids duplicate writes.

Validate the installed plugin and configuration before restarting the Gateway:

```bash
openclaw config validate
openclaw plugins list --json
python3 compatibility_check.py \
  --openclaw-config "$HOME/.openclaw/openclaw.json" \
  --keeper-config "$HOME/.config/openclaw-session-keeper/config.json" \
  --json
```

The deterministic provider remains available for non-native or embedded runtimes that actually honor `registerCompactionProvider`; validate that path with a disposable session before enabling it. See [OAuth-safe compaction compatibility](docs/OAUTH_SAFE_COMPACTION.md) for the failure model, production rollout and rollback procedure.

## Quick start

```bash
cp config.example.json "$HOME/.config/openclaw-session-keeper/config.json"
chmod 600 "$HOME/.config/openclaw-session-keeper/config.json"
python3 session_rollover.py --config "$HOME/.config/openclaw-session-keeper/config.json" scan --dry-run
```

Edit the copied configuration and replace only the example session entry. Never put provider keys in this file; the tool does not need them.

`allowManualFastMode` is opt-in per managed session. The configured `fastMode`
value remains the default; when enabled, Keeper preserves the current boolean
session value across scans and physical rollover instead of forcing the default
back onto an explicit user choice. Keep the option disabled for unattended or
role-agent sessions that must always run in Standard mode.

`rolloverTiming.deferUntilNextUserMessage` is also opt-in. When enabled, the
scanner arms normal threshold rollovers without changing the current physical
session. The plugin's awaited `before_dispatch` hook activates the pending
rollover and then lets the original inbound task continue in the new session.
Emergency thresholds and retired Codex-binding recovery remain immediate. The
hook must point to this repository's manager script, production configuration
and state file through the plugin's `deferredRollover` settings.

Run a real scan only after the dry run is clean:

```bash
python3 session_rollover.py scan
python3 session_rollover.py activate-pending --session-key agent:main:project-example
python3 session_rollover.py status
```

## Commands

```bash
python3 session_rollover.py scan --dry-run
python3 session_rollover.py scan
python3 session_rollover.py rollover --session-key agent:main:project-example --dry-run
python3 session_rollover.py activate-pending --session-key agent:main:project-example --dry-run
python3 session_rollover.py repair-visibility --session-key agent:main:project-example --dry-run
python3 session_rollover.py status

# Preview emergency recovery; no live files are modified.
python3 emergency_recovery.py --config "$HOME/.config/openclaw-session-keeper/config.json" \
  --session-key agent:main:project-example --retain-records 50

# After review, back up and ask the Gateway to trim deterministically.
python3 emergency_recovery.py --config "$HOME/.config/openclaw-session-keeper/config.json" \
  --session-key agent:main:project-example --retain-records 50 --execute
```

## Before every commit or push

Install `gitleaks`, initialize Git, and install the local hooks:

```bash
git init -b main
./scripts/install_git_hooks.sh
./scripts/preflight.sh
```

The hooks fail closed when gitleaks is missing. Findings print only file, line and rule names; potential secret values are never echoed.

## Documentation

- [Chinese README](README.zh-CN.md)
- [Token and tiered-pricing impact](docs/COST_MODEL.md)
- [中文 Token 与阶梯计费说明](docs/COST_MODEL.zh-CN.md)
- [OAuth-safe deterministic compaction](docs/OAUTH_SAFE_COMPACTION.md)
- [OAuth 安全的确定性压缩](docs/OAUTH_SAFE_COMPACTION.zh-CN.md)
- [Security policy](SECURITY.md)
- [Release checklist](RELEASE_CHECKLIST.md)
- [Changelog](CHANGELOG.md)

## License

Apache-2.0.
