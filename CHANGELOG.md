# Changelog

All notable changes to this project will be documented in this file. The format follows Keep a Changelog and the project uses Semantic Versioning.

## [Unreleased]

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
