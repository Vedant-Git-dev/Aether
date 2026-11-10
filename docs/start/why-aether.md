---
title: "Why Aether"
summary: "The case for a personal agent whose authorization is code, whose approvals are one tap, and whose record is tamper-evident, stated with the limits."
read_when:
  - You are deciding whether to give Aether access to your accounts
  - You want the security model and what it does not claim
  - You are comparing Aether with a hosted assistant
---

# Why Aether

Most assistants make you choose: keep the agent read-only and useless, or hand it your
accounts and hope. Aether is built for the part they skip. It asks for a lot of access,
so it is specific about where the guarantees come from and where they end.

## Policy decides, not the model

Authorization is deterministic code. The model proposes a tool call; a classifier
evaluates your rules first and then a built-in ladder, and returns `allow`,
`require_approval`, or `deny`. No language model sits in the decision path. An
unrecognized tool parks rather than running silently, so the worst case is an extra tap,
never an unreviewed action. The rules and the ladder are in
[Policy](../concepts/policy.md).

## One tap for anything that leaves

A parked call never blocks the agent loop. Aether encrypts the call, stores it, writes an
audit row, and shows it on every enabled surface: the panel and Telegram or Discord with
Approve and Deny. Approving runs exactly that stored call. It does not grant future
trust, which belongs in a rule. Requests expire after 24 hours by default, and expiry is
audited too.

## A record you can verify

Every decision appends a row that commits to every row before it with SHA-256. `/verify`
re-walks the chain and reports the first break. The audit log keeps ids, counts, and
digests; it never stores parameter values.

## Your data stays yours

State, memory, and credentials live on your hardware. Content is encrypted at rest with
AES-256-GCM under a key you hold, and each value is bound to its table, column, and row,
so a ciphertext moved to another row fails to decrypt rather than silently decoding.
What is sealed, and what may stay plaintext, is in
[Sealed storage](../concepts/sealed-storage.md).

## The security model, plainly

| Area | What you get |
| --- | --- |
| **Encryption at rest** | AES-256-GCM with a server-held key. Protects stored history against a database-level compromise such as a leaked backup. Does **not** protect against a compromised server process. |
| **Secrets** | The same encrypted store. Provider tokens use a separate keyring so they can be rotated. The audit log stores a parameter digest, never values. |
| **Model providers** | They see prompts, recent context, tool results, and files in a perception call. Choose a provider and retention policy you accept; there is no end-to-end encryption. |
| **Sandboxes** | Receive only the files a job needs, never the database URL, encryption key, or provider tokens. |
| **Web content** | Fetched pages and searches cannot change policy. They reach the profile only as a proposal you confirm. |
| **Screen capture** | Perception only. A screenshot is described, and no action tools are registered for the screen. |
| **Free-tier hosting** | Schedules persist and re-arm on boot, but do not fire while the process is asleep. |
| **Single owner** | `AETHER_TOKEN` is shared by the API, web chat, and companion. Aether is not multi-tenant. |

## What we do not claim

- No independent security audit.
- No end-to-end encryption.
- No protection from a compromised host: whoever can read `AETHER_ENCRYPTION_KEY` and
  query the database can read the same rows the app does.
- No guarantee that a model provider excludes your data from training.
- No hosted service-level agreement.

Report vulnerabilities through [SECURITY.md](../../SECURITY.md), not a public issue.

## Related

- [Architecture](../concepts/architecture.md): how the process is wired.
- [Policy](../concepts/policy.md): the rules and the built-in ladder.
- [Sealed storage](../concepts/sealed-storage.md): what is encrypted at rest.
