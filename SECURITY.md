# Security Policy

## Supported versions

Security fixes are applied to the latest release line.

## Reporting a vulnerability

Do not open a public issue containing credentials, transcripts, session identifiers, private paths, or exploit details. Use GitHub private vulnerability reporting after the repository is published.

## Credential policy

This project does not require model-provider credentials. Never add credentials to source, examples, tests, issues, logs, or handoff fixtures.

If a credential is ever committed:

1. Revoke or rotate it immediately. Removing the file is not sufficient.
2. Stop publishing until the complete Git history has been rescanned.
3. Rebuild the public history from a verified clean tree if exposure is uncertain.

Every commit and push should pass `scripts/preflight.sh`. Local hooks and CI are defense in depth; they do not replace key rotation after an exposure.
