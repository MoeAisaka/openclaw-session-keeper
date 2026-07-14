# Release checklist

- [ ] Confirm the repository was created from the clean staging tree, not a production directory.
- [ ] Confirm author name/email are appropriate for a public commit.
- [ ] Run `python3 -m unittest discover -s tests -v`.
- [ ] Run `python3 scripts/secret_scan.py --tracked`.
- [ ] Run `gitleaks dir --redact --no-banner .`.
- [ ] Run `gitleaks git --redact --no-banner .` after the first commit.
- [ ] Inspect `git ls-files` for configuration, state, logs, archives and databases.
- [ ] Validate the example configuration in a disposable OpenClaw environment.
- [ ] Recheck the OpenClaw compatibility matrix.
- [ ] Create a signed tag only after CI and secret scanning pass.
