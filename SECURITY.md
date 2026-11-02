# Security policy

## Supported versions

Only the latest commit on `main` is supported. There are no maintained release branches
and no backports.

## Threat model

**Protected:** stored history (messages, files, derived memory) is encrypted with
AES-256-GCM before it is written to Postgres. A database-level compromise, such as a
leaked backup or a breached storage provider, yields ciphertext rather than content.
Encrypted values are bound to their table, column, and row id, so moving a ciphertext to
another row fails to decrypt.

**Not protected:** compromise of the server process itself. The content key lives in the
server's environment (`AETHER_ENCRYPTION_KEY`), so an attacker who owns the process can
read the store. Moving the key to a dedicated secrets manager is planned.

**Not protected:** inference-time exposure at the configured model provider. Prompts,
recent context, tool results, and files included in a perception call reach that
provider. Choose a provider and retention policy you accept.

**Not protected:** a compromised connected app or a stolen OAuth token. Scope grants and
bot permissions to the minimum you need, and disconnect apps you no longer use.

**Not protected:** multi-tenant isolation. `AETHER_TOKEN` is a single shared secret for
the API, web chat, and companion. Aether is designed for one owner.

## Reporting a vulnerability

Do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting on this repository: [open a private advisory](https://github.com/Vedant-Git-dev/Aether/security/advisories/new).

Include reproduction steps, the affected component, and the version or commit you tested.
You will hear back within a few days. Please give a reasonable window to fix the issue
before publishing details.

## Operational guidance

- Use a long random `AETHER_TOKEN` and serve Aether behind TLS.
- Keep `AETHER_ENCRYPTION_KEY` and `AETHER_TOKEN_ENCRYPTION_KEY` out of the repository
  and out of database backups.
- Back up the encryption key separately from the database. A backup without the key is
  unreadable.
- Rotate provider token keys with `python -m aether.providers.rotate_tokens --check`.
- Review the audit chain with `/verify` after restoring a backup.
- Run integration tests only against a scratch database. The suite truncates tables.
