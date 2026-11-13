---
title: "Authorization and policy"
summary: "How the deterministic gate classifies every tool call, the rule grammar, and how approvals work."
read_when:
  - You are writing authz rules
  - A tool keeps asking for approval
  - You want to know what policy does and does not do
---

# Authorization and policy

Aether's authorization layer is deterministic. The model proposes a tool call; code
classifies it; the result is `allow`, `require_approval`, or `deny`. No language model
is consulted in the decision path.

The implementation is `src/aether/authz/policy.py`. The classifier is a pure function
of the tool name, the call parameters, the call origin, and the effect a provider
declares.

## Order of evaluation

First match wins.

1. **Your rules.** Each rule matches `tool_pattern` with `re.search`, optionally
   requires `param_pattern` to match the canonical JSON of the parameters, and returns
   its configured decision.
2. **`set_config`.** A settings change made through chat or the agent.
   - A connect the user drove in the `/apps` walk (`origin: walk`) on `mcp_servers` or
     `messaging` is allowed, because the user's pick was the approval.
   - Tuning roots `agent`, `salience`, `llm`, `proactive`, `workbench`, and `providers`
     are allowed.
   - Security roots `contacts`, `authz`, `messaging`, and `mcp_servers`, plus any
     unknown or missing path, require approval. `contacts` and `authz` park even from a
     walk.
3. **Provider effects.** Direct app tools declare `read`, `write`, or `destructive`.
   Reads run. Writes and destructive actions park. A missing or unknown effect falls
   through to the fail-safe.
4. **Internal tools.** Tools that touch only Aether's own state are allowed:
   `memory_*`, `note_entity`, `schedule_action`, `request_screen_capture`,
   `look_at_media`, `share_media`, `edit_video`, `get_pending_approvals`,
   `send_chat_message`, the alert tools, `explain_decision`, `verify_integrity`,
   `get_config`, and `workspace_*`.
5. **Web research.** `web_fetch` and `web_search` are allowed. They only read.
6. **Image generation.** `generate_image` is allowed. The prompt reaches the image
   provider, and the result is a stored file. Sending it anywhere is a separate call
   that parks.
7. **Risky verbs.** A name containing `send`, `reply`, `forward`, `post`, `publish`,
   `delete`, `remove`, `cancel`, `pay`, `transfer`, `invite`, `book`, or `create` parks.
   The verb must start a word inside the name, so `resend` and `facebook` do not match.
8. **Read-only verbs.** A name starting a word with `list`, `search`, `get`, `read`,
   `fetch`, `find`, or `query` is allowed. Risky verbs are checked first, so a compound
   name cannot sneak a write past a read.
9. **Fail-safe.** Anything else requires approval.

The fail-safe direction is deliberate: an unknown tool can never run silently. The
worst case is an extra tap, never an unreviewed action.

## Rule grammar

```yaml
authz:
  rules:
    - tool_pattern: "regex"
      param_pattern: "optional regex over canonical JSON"
      decision: allow | approve | deny
      note: "shown on the approval card and in the panel"
```

Both patterns use Python `re.search`, not a glob and not a full match. `param_pattern`
runs against compact JSON with sorted keys and no spaces, for example
`{"repo":"owner/name","title":"..."}`. String values are JSON-escaped.

A rule with `decision: deny` refuses the call outright. It does not park, and it cannot
be approved later.

## Naming

| Source | Shape | Example |
| --- | --- | --- |
| Direct app provider | `app__tool` | `gmail__send_email`, `github__create_issue` |
| MCP server | `server__tool` | `weather__get_forecast` |
| Native Aether tool | its own name | `memory_search`, `schedule_action` |

The prefix is the app key or the MCP server name from `config.yaml`.

## Examples

Treat reading as free and sending as one tap:

```yaml
authz:
  rules:
    - tool_pattern: "^gmail__(list_messages|get_message)$"
      decision: allow

    - tool_pattern: "^gmail__(send_email|send_draft)$"
      decision: approve
      note: "Mail leaves Aether"
```

Never post publicly on LinkedIn:

```yaml
    - tool_pattern: "^linkedin__create_post$"
      param_pattern: '"visibility":"PUBLIC"'
      decision: deny
      note: "Public posts need a separate review"
```

Let a specific repository through without a tap:

```yaml
    - tool_pattern: "^github__create_issue$"
      param_pattern: '"repo":"your-org/your-repo"'
      decision: allow
      note: "Issues in the tracker are routine"
```

Silence one noisy MCP tool:

```yaml
    - tool_pattern: "^news__list_headlines$"
      decision: allow
```

## Approvals

A `require_approval` ruling never blocks the agent loop. Aether:

1. Encrypts the call parameters and stores them in `pending_approvals`, bound to the
   row id.
2. Writes an audit row recording the hold, the matched rule, and the parameter digest.
3. Shows the call on every enabled surface: the panel's Attention page, and Telegram
   or Discord with Approve and Deny buttons.

Approving executes exactly that stored call. It does not grant future trust; standing
trust belongs in a rule. Denying writes a denial and runs nothing. Requests expire
after `authz.approval_ttl_hours` (24 by default); expiry is audited as well.

Approval is not a blanket override. A parked call is executed through the same tool
registry, but it is not re-classified at execution time. That is why the park-time
gate is the only gate a security write passes, and why an approved call cannot be
silently re-shaped.

## Background tasks

A background task's result can carry a staged action. Approving the task first asks
the skill to revalidate live state: does the source event still exist, is the
opportunity still open. If not, the approval is retired, the task is marked stale, and
nothing fires. Only a fresh task is carried out.

During detection and planning, a skill receives a read-only executor. A write proposed
in those phases is refused before the policy is asked. This is a code gate, not a
prompt instruction.

## Contact allowlists

Contact filtering is separate from tool authorization. It decides whose messages and
mail the agent may see at all.

```yaml
contacts:
  mode: enforce
  allowlist:
    - {platform: "*", handle: "friend@example.com"}
  gmail:
    mode: enforce
    allowlist:
      - {platform: email, handle: "friend@example.com"}
  telegram:
    mode: enforce
    allowlist:
      - {platform: telegram, handle: "@friend"}
```

- `mode: enforce` drops non-allowlisted senders at ingestion, before storage.
- `mode: off` does not filter.
- An app slice's `mode: null` follows the section default.
- Gmail's list also filters bulk inbox reads. Mail from someone not on the list never
  reaches the model, even when the agent asks for the inbox.
- `platform: "*"` entries count on every platform. `platform: email` matches Gmail.

## What policy does not do

- It does not judge intent or content. It matches names and parameters.
- It does not prevent a connected app from behaving differently than its tool
  description claims.
- It does not replace scoping OAuth grants and bot permissions to the minimum you
  need.
