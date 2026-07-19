# Changelog

All notable changes to this project will be documented in this file. The format follows Keep a Changelog and the project uses Semantic Versioning.

## [Unreleased]

## [0.3.1] - 2026-07-19

### Fixed

- Preserve a bounded copy of the previous assistant outcome in the verified handoff and operator-visible rollover notice.
- Derive an explicit continuation decision from Workflow Ledger state and visible turn order, so a completed turn is not silently rerun.
- Treat a busy visibility notice after a committed reset as auxiliary instead of failing the triggering user dispatch.

### Added

- Track the first new-generation Agent run through `before_agent_run` and `agent_end` without persisting prompts, replies, or raw errors.
- Record idempotent `first_dispatch_started`, `first_dispatch_completed`, and `first_dispatch_failed` lifecycle evidence.
- Reconcile a failed or never-started first dispatch by injecting the preserved outcome after the session becomes idle.

## [0.3.0] - 2026-07-18

### Added

- Add an opt-in deferred rollover state machine: the periodic scanner prepares a checksummed handoff at the normal threshold, while the awaited `before_dispatch` hook activates the rollover before the next user task is dispatched.
- Add the idempotent `activate-pending` operator command and deterministic coverage for arming, activation, retry and emergency paths.

### Changed

- Preserve the just-finished assistant answer in the current physical session until the next user message arrives.
- Keep emergency thresholds and retired Codex-binding recovery immediate and fail closed.

### Security

- Never log or persist the inbound prompt in the deferred rollover hook.
- Fail closed before agent execution when a pending rollover cannot be verified or activated, so retrying cannot duplicate a partially executed task.

## [0.2.2] - 2026-07-16

### Fixed

- Preserve an explicitly enabled user's current Standard/Fast choice during periodic preference repair and physical session rollover.
- Restore the pre-reset speed choice after `sessions.reset` instead of reapplying the configured default.

### Security

- Keep manual speed preservation opt-in per session so unattended and role-agent sessions can remain pinned to Standard mode.

## [0.2.1] - 2026-07-15

### Fixed

- Correct the OpenClaw `2026.7.1` hosted-Codex compatibility guidance: native app-server sessions ignore custom compaction provider overrides and own manual compaction.
- Prevent automatic CLI compaction from racing the periodic physical rollover by documenting and checking a minimum prompt-budget headroom.

### Added

- Add a read-only compatibility checker that detects ignored provider overrides and unsafe compaction/rollover headroom without printing auth-profile values.

## [0.2.0] - 2026-07-15

### Added

- Add an auth-independent deterministic OpenClaw compaction provider with bounded one-pass memory use.
- Add fail-closed emergency recovery that backs up first and delegates transcript trimming to the Gateway-owned `sessions.compact` lifecycle API.
- Add transcript-byte checkpoint, rollover and emergency thresholds alongside token thresholds.
- Add secret redaction for summaries, operator focus text and fallback summaries.

### Security

- Document the required `memoryFlush.enabled=false` and `qualityGuard.enabled=false` settings so no auxiliary LLM call bypasses deterministic compaction.
- Stop rewriting active transcripts or `sessions.json` directly during emergency recovery.
- Pin the Node.js CI toolchain and cover the plugin with Node test execution.

### Documentation

- Record compatibility validation against the stable OpenClaw `2026.7.1` release.

## [0.1.1] - 2026-07-15

### Fixed

- Fix live `repair-visibility` so it runs under the process lock and returns the repair event.
- Enforce owner-only permissions on Keeper state directories and lock files.
- Explicitly close read-only SQLite connections after Codex binding checks.

### Added

- Clean public-source baseline for verified OpenClaw physical-session rollover.
- Stable session-key continuity with bounded, checksummed handoff state.
- Preservation and verification of user model, thinking and non-fast preferences.
- Fail-closed pre-commit, pre-push and CI secret scanning.
- Provider-configurable token and tiered-pricing estimator with documented OpenAI reference scenario.

## [0.1.0] - 2026-07-14

- Initial release candidate.
