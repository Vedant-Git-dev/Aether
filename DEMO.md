# Demo walkthrough

Eight scripted demos, in increasing order of "watch it carefully". Each one
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

## 6. Why did you do that? — decision replay

**What it shows**: the encrypted "why" behind every act — ask after the
fact, get the record.

1. Do anything from demos 1–5 (or just chat). Every act — a turn, a
   routine fire, a scheduled action, an approved call — has written a
   decision trace alongside its audit rows.
2. In chat: *"why did you tell me about the invoice?"* The agent calls
   `explain_decision`, which replays the recorded trace: what was seen
   (the event lines), what was proposed, the gate's ruling on each call
   with its audit row, and what came back. The answer comes from the
   record, not from the model remembering.
3. Ask about a held action: *"what was approval #3?"* — the trace of the
   act that proposed it and the trace of it running, both from the
   record.
4. The API serves the same records for the panel: `GET /api/traces`
   (the list) and `GET /api/traces/{id}` (one full trace), token-guarded.
5. **The point**: traces are the why to the audit's what — encrypted at
   rest like all human-readable content, and written so that even a
   failed trace write never breaks the act it records.

## 7. The record proves itself — from your pocket

**What it shows**: the chain badge and the replay delivered where you
already are — deterministic, plain words, no model woken.

1. In any enabled chat app (Telegram, Discord, Slack — or the web chat),
   send `/verify`. The answer is code, not the model — no tokens, no
   ingest, no new trace:
   > 🛡️ decision record: 1,247 decisions, chain intact — every entry
   > still hashes to the one before it, last act 2 minutes ago.

   A natural-language ask works too — *"can anyone tamper with your
   logs?"* — the model has a `verify_integrity` tool for exactly that.
2. Reply **why?** to a message Aether sent you — a routine ping, an
   approval nudge, a carried-out call (Telegram, Discord, or a Slack
   thread). Your reply's platform id links it back to the trace that
   produced the message, and the record answers:
   > 🧵 that message, from the record (trace #57):
   > Your routine 'billing' fired.
   > I went ahead with keeping a note — the gate let it through
   > (builtin:internal).
   >
   > The full recorded detail — the exact call, the parameters, the
   > gate's ruling — is trace #57 in the panel.

   The plain-words rule holds in chat: tool names and raw errors stay
   in the panel; the message reads like Aether explaining itself.
3. Break the chain on purpose — demo 3's tamper, this time watched from
   chat (run the SQL yourself, against your live DB):
   ```sql
   SELECT outcome FROM audit_log WHERE seq = 1;   -- remember it
   UPDATE audit_log SET outcome = 'haha' WHERE seq = 1;
   ```
4. `/verify` again:
   > ⚠️ decision record: BROKEN at entry #1 — seq 1: stored hash does
   > not match the entry contents. Everything from there on can't be
   > trusted.
5. Put the row back (`UPDATE audit_log SET outcome = '<the original
   text>' WHERE seq = 1;`) and `/verify` once more — green again,
   because the restored entry hashes to exactly what it hashed to
   before.

**The point**: the trust features are not panel-only curiosities. The
badge and the replay are reads from the record — deterministic, plain
words, no LLM turn — which is itself the claim: the record can answer
for Aether without Aether improvising.

## 8. The whole config, from your pocket

**What it shows**: every `config.yaml` setting readable and changeable
from chat — through the same gate as every other action, with the change
persisted in Postgres, and a restart nobody performs by hand.

1. In chat, send `/config`. The answer is code, not the model — no tokens,
   no ingest, no new trace:
   > ⚙️ my configuration — config.yaml plus whatever you've changed from
   > chat:
   > llm: anthropic · claude-opus-5 · max 16000 tokens
   > agent: tick 30s · up to 12 tool calls a turn · daily cap 20 · quiet
   > hours off (urgent at 8)
   > …
   > Nothing changed from config.yaml yet.
   > /config show <section> for one section's settings · /config show
   > <path> for one value

   `/config show agent` for one section, `/config show agent.tick_seconds`
   for a single value, and a near miss like `/config agent.ticksecond`
   gets a did-you-mean instead of a shrug.
2. Change a personal tuning value — no approval card, because tuning is
   yours:
   ```
   /config set agent.tick_seconds 10
   ```
   > agent.tick_seconds set to 10 (config.yaml says 30). — applies from the
   > next tick

   The **audit** pane shows the row (`allow · builtin:config-tune`), and
   the panel's read-only config feed (`GET /api/config`) serves the merged
   value with the override listed. `/config reset agent.tick_seconds`
   puts config.yaml's value back and deletes the row.
3. Now a security-shaped one:
   ```
   /config set messaging.discord.enabled true
   ```
   It parks for your one tap — the same card as any risky action:
   > 🔒 that one's security-shaped — held for your one-tap approval (#14).
   > Tap approve and it's done.

   Approve it and the change carries out:
   > messaging.discord.enabled set to true (config.yaml says false).
   > — discord is starting up

   …and Discord is alive without a deploy. (No `DISCORD_BOT_TOKEN` in
   .env? The reply says so honestly — the toggle persists, the connector
   starts the moment the token exists.)
4. The allowlist, the same way: `/config set contacts.mode enforce`, then
   `/config add contacts.allowlist telegram @mom` — both park, both need
   the tap. Approved, the next message from @mom is seen while a
   stranger's is dropped at ingest, content never stored:
   > contacts.allowlist updated — 1 entries now (config.yaml has 0).
   > — applies to the next message I see
5. Change the model itself:
   ```
   /config set llm.model claude-sonnet-5
   ```
   > llm.model set to claude-sonnet-5 (config.yaml says claude-opus-5).
   > — restarting myself to load it, back in a few seconds

   The reply lands first — the delay is the current turn's chance to finish
   speaking — then the process drains its requests and serves a fresh app:
   new lifespan, fresh boot merge, the new model. Nobody restarts anything
   by hand. And on Render's ephemeral disk, this is the only way a config
   change survives at all: it lives in Postgres, not the filesystem.

**The point**: the config is not a file you edit on the server — it's a
conversation. Reads are deterministic, writes pass the same gate and land
in the same audit chain as every other act, and the change outlives the
container.

## What all eight have in common

Every path ends in the same four artifacts, which is the whole pitch:

- the **events** pane — what Aether noticed (deduplicated, scored);
- the **approvals** pane — what waited for a human, with parameters;
- the **audit** pane — every decision in order, with a chain that
  verifies;
- the **decision traces** — the why behind each act, replayable on
  request.
