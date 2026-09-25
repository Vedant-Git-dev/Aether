# Security policy

## Threat model (what Aether protects, and what it doesn't)

- **Protected:** the stored history — messages, files, derived memory — is
  encrypted with AES-256-GCM before it is written to Postgres. A
  database-level compromise (leaked backup, breached storage provider) yields
  ciphertext, not your data.
- **Not protected:** compromise of the server process itself. The decryption
  key lives in the server's environment (`AETHER_ENCRYPTION_KEY`), so an
  attacker who owns the process can read the store. Moving the key to a
  dedicated secrets-management service is on the roadmap.
- **Not protected:** inference-time exposure at the configured LLM provider.
  Choose your provider and its data-retention posture accordingly.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
private vulnerability reporting on this repository, or contact a maintainer
directly. Include reproduction steps and affected components; you'll hear
back within a few days.
