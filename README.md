# OpenClaw Session Keeper

Safe physical-session rollover behind stable OpenClaw project session keys.

Long-running agent sessions eventually become expensive, brittle, or difficult to recover. Session Keeper creates a verified handoff, rotates the physical session only while it is idle, and keeps the stable project entry intact.

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

## Requirements

- Python 3.10+
- OpenClaw with `gateway call sessions.reset`, `sessions.patch`, `chat.history`, and `chat.inject`
- macOS or Linux with `fcntl`
- Optional: nmem for semantic recall
- Optional: Codex app-server state inspection

Tested against OpenClaw `2026.7.1-beta.6`. Internal Gateway APIs can change; review the compatibility notes before upgrading OpenClaw.

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
- [Security policy](SECURITY.md)
- [Release checklist](RELEASE_CHECKLIST.md)
- [Changelog](CHANGELOG.md)

## License

Apache-2.0.
