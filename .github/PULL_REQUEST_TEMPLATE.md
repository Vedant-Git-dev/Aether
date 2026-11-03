# Pull request

## What changed

A short description of the change and why it is needed.

## Trust and safety

- [ ] No new outbound action, or it goes through the existing policy gate
- [ ] No change to authorization, approvals, or audit without tests
- [ ] No secret, token, database URL, or personal data added to the repository
- [ ] No emoji added to user-facing strings, logs, documentation, or the panel

## Verification

Paste the commands you ran and their results:

- [ ] `ruff check src tests`
- [ ] `ruff format --check .`
- [ ] `pytest -m "not integration and not e2e"`
- [ ] Scratch-database integration suite, if relevant
- [ ] `AETHER_TEST_E2E=1 pytest -m e2e`, if the panel changed

## Documentation

- [ ] README or `docs/` updated for new settings, commands, or behavior
