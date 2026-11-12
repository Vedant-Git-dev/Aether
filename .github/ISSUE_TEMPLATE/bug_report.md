---
name: Bug report
about: Report a reproducible problem
title: ""
labels: bug
assignees: ""
---

## What happened

What went wrong, where you saw it, and what it looks like from the outside.
Lead with the symptom, then the cause if you know it.

## What you expected

What should have happened instead.

## Steps to reproduce

1.
2.
3.

## Environment

- Commit or version:
- Python version:
- Postgres version:
- Operating system:
- Panel built? yes / no
- LLM provider and model (no keys):

## Logs

The relevant runtime or startup lines, plus audit entries from `/api/audit` if
the problem involves policy or approvals. Remove secrets, tokens, database URLs,
personal names and chat ids first — never paste `.env` or `config.yaml` unchanged.
