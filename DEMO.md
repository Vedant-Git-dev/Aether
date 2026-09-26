# Demo walkthrough

Five scripted demos, in increasing order of "watch it carefully". Each one
runs against a live Aether (local or deployed — see
[DEPLOYMENT.md](DEPLOYMENT.md)) with at least one LLM provider key set.
Open the web panel (`/`) with your token before starting; the sections
below reference its panes.

## Setup used throughout

```yaml
# config.yaml — the shape behind demos 1 and 3
mcp_servers:
  - name: mail
    transport: {type: stdio, command: npx, args: ["-y", "mcp-mail-server"]}
    poll_tools: [{tool: list_unread, every_minutes: 5}]
authz:
  rules: []
  approval_ttl_hours: 24
```

(Any MCP mail server works; anything read-only is enough for demo 1.)

## 1. Low-stakes actions happen on their own — and leave a trail

**What it shows**: continuous monitoring, deduplicated memory, and that
routine read-only actions need no permission slip.

1. Boot Aether with the mail server configured. Within one poll interval
   the **events** pane shows `mail/poll:list_unread` entries.
2. Send the same "you have 3 unread messages" twice? You won't see it
   twice — dedup is content-hash based; the second poll is a no-op.
3. Ask in chat: *"what's unread in my mail?"* — the agent calls
   `mail__list_unread` (or `memory_search`), classified **read-only →
   allow**, and answers. No approval card appears.
4. Open the **audit** pane: every poll and every tool call is there with
   its decision (`allow · builtin:read-only`) and the chain badge reads
   `chain ok (N)`.

## 2. Screen-vision on an app with no API

**What it shows**: the perception-only fallback — Aether reads a
screenshot as memory and nothing more.

1. Start the companion on your machine (Wayland; needs `grim`):
   ```bash
   python companion/aether_snap.py --token <AETHER_TOKEN> --poll
   ```
2. In chat: *"take a look at my screen and tell me what's pending."*
   The agent calls `request_screen_capture` (internal → allow). Within one
   poll interval the companion captures and uploads.
3. A `screen_capture` event appears, described by the vision model —
   app, summary, any people or commitments. The agent tells you what it
   saw.
4. Ask an hour later: *"what was on my screen earlier?"* — `memory_search`
   finds it.
5. **The point**: no action tools exist for the screen. If the agent
   proposes acting on something it saw, that proposal goes through the
   same gate as everything else — see demo 3.

## 3. A risky action is held for one tap

**What it shows**: the deterministic gate and the one-tap approval — the
core of the design.

1. In chat: *"reply to Alice's last email and tell her the demo moved to
   Friday."*
2. The agent drafts and proposes `mail__send_message`. The classifier
   matches `send` → **REQUIRE_APPROVAL**: the call does **not** run. An
   approval card appears on the web panel with the exact parameters, and
   on every enabled chat surface as Approve / Deny buttons.
3. Press **Deny** anywhere. The reply is "Not run — mail__send_message
   was denied." The audit pane shows the `approve` (parked) row followed
   by the denial outcome; the tool never executed.
4. Propose it again — the agent tells you it's parked and doesn't
   re-propose; the prompt design and the held-call result both say so.
5. Now approve one: the held call executes, the approval is marked
   executed, and the audit trail shows the run — `✅ ran …` lands in
   chat.
6. Tamper check (optional, two minutes): `UPDATE audit_log SET
   outcome='haha' WHERE seq=1;` in psql, then reload the audit pane —
   the badge flips to `BROKEN @1`. Fix it back to watch it go green.

## 4. A schedule survives a kill -9

**What it shows**: actions are persisted at creation, not held in
memory — the reason "schedule" isn't a timer variable.

1. In chat: *"in two minutes, ask the mail server for my unread
   messages and tell me what's there."* The agent calls
   `schedule_action`; the row exists in Postgres the moment it returns.
2. Kill the process hard: `kill -9 <pid>` (or Restart in the Render
   dashboard) **before** the two minutes are up.
3. Start it again. The boot log shows the re-arm line —
   `re-arming 1 overdue scheduled action(s)`.
4. At fire time the action runs — **through the same gate**: an allowed
   tool executes, a risky one parks for approval. Scheduling is not a
   way around review.

## 5. Teach a routine — and watch it still ask

**What it shows**: standing reactions with deterministic triggers, and
that teaching one is never a way around review.

1. In chat: *"whenever a mail poll mentions an invoice, note it on my
   billing contact."* The agent calls `create_routine` and confirms:
   *Routine 1 'billing' armed … It still passes the authorization gate
   every time it fires.*
2. Wait for the next mail poll to bring an invoice in (or send yourself
   one). The routine fires **before any LLM is involved** — matching is
   source/sender/keyword, not a model judgment — and you get the
   🧭 ping: *routine 'billing' fired → note_entity: …*
3. The **audit** pane now carries two rows per fire: the gate's own
   decision (`allow · builtin:internal`) and a provenance row —
   `routine · info · routine:1` — pointing back at what fired and on
   which event. Ask *"list my routines"* for the roster: label, trigger,
   fired count, cooldown.
4. Now teach a risky one: *"when a mail poll contains 'server is down',
   message me on Telegram."* When it fires, nothing sends — the
   **approvals** pane lights up with `telegram__send_message`, held for
   your one tap. Teaching is not a way around review, either.
5. Done with one? *"pause the billing routine"* / *"delete the billing
   routine"* — `set_routine_enabled` and `delete_routine` answer, and a
   paused routine stops matching while its row stays in Postgres for
   when you re-arm it.

## What all five have in common

Every path ends in the same three artifacts, which is the whole pitch:

- the **events** pane — what Aether noticed (deduplicated, scored);
- the **approvals** pane — what waited for a human, with parameters;
- the **audit** pane — every decision in order, with a chain that
  verifies.
