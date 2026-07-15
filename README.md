# OpenClaw Session Keeper

Safe physical-session rollover behind stable OpenClaw project session keys.

Long-running agent sessions eventually become expensive, brittle, or difficult to recover. Session Keeper creates a verified handoff, rotates the physical session only while it is idle, and keeps the stable project entry intact.

Version 0.2 also provides an auth-independent deterministic compaction provider. It prevents a Codex OAuth session from being routed into an API-key-only summarizer and keeps emergency recovery inside OpenClaw's Gateway lifecycle lock.

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
- Thinking and non-fast execution preferences
- An idempotent, operator-visible rollover notice

## Safety model

- Running sessions are never reset.
- Missing transcripts, failed backups, failed nmem checks, failed Gateway verification, or preference drift stop the rollover.
- State and handoff files are written atomically with owner-only permissions.
- Real configuration, transcripts, logs, databases, credentials and archives are denied by `.gitignore` and the repository secret gate.
- The optional Codex binding check opens the configured SQLite database read-only.
- Deterministic compaction uses bounded one-pass retention instead of copying the full normalized transcript.
- Emergency recovery backs up first, then calls the official `sessions.compact --max-lines` Gateway RPC. It never rewrites an active transcript or `sessions.json` directly.

## Requirements

- Python 3.10+
- OpenClaw with `gateway call sessions.reset`, `sessions.patch`, `chat.history`, and `chat.inject`
- macOS or Linux with `fcntl`
- Optional: nmem for semantic recall
- Optional: Codex app-server state inspection

Tested against the stable OpenClaw `2026.7.1` release. Internal Gateway APIs can change; review the compatibility notes before upgrading OpenClaw.

## OAuth-safe deterministic compaction

Install the local plugin from a clean checkout:

```bash
openclaw plugins install .
```

Then configure OpenClaw so every compaction stage stays model-free:

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

`memoryFlush` and the safeguard quality guard can make auxiliary LLM calls even when the main summary provider is deterministic. Disable both unless they are explicitly routed to a compatible non-OAuth model. If nmem or another memory plugin already handles durable memory, disabling OpenClaw's model-based memory flush avoids duplicate writes as well.

Validate the installed plugin and configuration before restarting the Gateway:

```bash
openclaw config validate
openclaw plugins list --json
```

See [OAuth-safe deterministic compaction](docs/OAUTH_SAFE_COMPACTION.md) for the failure model, production rollout and rollback procedure.

## Quick start

```bash
cp config.example.json "$HOME/.config/openclaw-session-keeper/config.json"
chmod 600 "$HOME/.config/openclaw-session-keeper/config.json"
python3 session_rollover.py --config "$HOME/.config/openclaw-session-keeper/config.json" scan --dry-run
```

Edit the copied configuration and replace only the example session entry. Never put provider keys in this file; the tool does not need them.

Run a real scan only after the dry run is clean:

```bash
python3 session_rollover.py scan
python3 session_rollover.py status
```

## Commands

```bash
python3 session_rollover.py scan --dry-run
python3 session_rollover.py scan
python3 session_rollover.py rollover --session-key agent:main:project-example --dry-run
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
