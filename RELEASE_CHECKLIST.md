# Release checklist

- [ ] Confirm the repository was created from the clean staging tree, not a production directory.
- [ ] Confirm author name/email are appropriate for a public commit.
- [ ] Run `python3 -m unittest discover -s tests -v`.
- [ ] Run `npm test`.
- [ ] Run `npm audit --audit-level=high`.
- [ ] Run `python3 scripts/secret_scan.py --tracked`.
- [ ] Run `gitleaks dir --redact --no-banner .`.
- [ ] Run `gitleaks git --redact --no-banner .` after the first commit.
- [ ] Inspect `git ls-files` for configuration, state, logs, archives and databases.
- [ ] Validate the example configuration in a disposable OpenClaw environment.
- [ ] Run `compatibility_check.py` against the target OpenClaw, Keeper and session-store files; review every finding without copying private output into the repository or PR.
- [ ] For native hosted-Codex sessions, verify no global deterministic-provider override is selected and the rollover headroom meets the documented margin.
- [ ] Verify `memoryFlush.enabled=false` and `qualityGuard.enabled=false` unless both are explicitly routed to a compatible model.
- [ ] Verify emergency recovery mutates through `sessions.compact --max-lines`, not direct transcript/store writes.
- [ ] Recheck the OpenClaw compatibility matrix.
- [ ] Create a signed tag only after CI and secret scanning pass.
