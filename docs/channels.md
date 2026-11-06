# Channels

Aether has one web surface and two native chat connectors. All three carry
approvals. The web panel is always available, even when no other surface is
configured.

## Web panel and chat

The panel is a React app built into `src/aether/web/` and served by the same process
on `/`. It includes:

| Page | What it shows |
| --- | --- |
| Live Activity | Recent events Aether has seen across connected apps |
| Tasks | Approvals waiting for you, scheduled actions, and background task results |
| Memory & Context | Resolved people and their relationship notes |
| Audit Log | Every authorization decision and the chain badge |
| Decision Traces | What triggered an action, each tool call, and each ruling |
| Authorization & Policies | Your rules and the built-in ladder |
| Apps & Permissions | Connected providers and MCP servers |
| Contacts | The cross-app and per-app allowlists |
| Settings | Models, behavior, credentials, profile, personality, access, workbench, media |

The chat is a WebSocket at `/ws`. The token rides in the connection query string
because WebSocket clients cannot set arbitrary headers in every browser. Every REST
call uses the `x-aether-token` header instead. Either way, use TLS in front of the
process when it is reachable from the internet.

The panel token is stored in the browser's local storage. It is not a cookie and is not
sent to third parties.

## Telegram

1. Create a bot with [BotFather](https://t.me/BotFather) and copy the token.
2. Put it in `.env`:

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:your-token
   ```

3. Enable it in `config.yaml`:

   ```yaml
   messaging:
     telegram:
       enabled: true
   ```

4. Restart Aether.
5. Open a direct chat with the bot and send any message.

The step 5 message matters: the connector learns which chat to answer from the first
inbound message. Approvals and results that arrive before you have ever messaged the
bot are still visible in the web panel, but they have nowhere to land in Telegram.

Telegram supports:

- plain conversation,
- Approve and Deny buttons on approvals,
- Read the full text on a parked call,
- Approve, Revise, and Discard on background results,
- file, photo, video, and voice-note attachments,
- reply-to context, so "send it back" resolves to the file you replied to.

A prompt answer to the latest message is sent as a plain message, never a quoted reply.
A quote is used only when it helps: you replied to a specific message, the answer is
about an earlier message or a newer one arrived while it was being prepared, the answer
took longer than about ninety seconds, several pending questions were answered together,
or an approval outcome answers the card you tapped. Proactive alerts never quote you.

Only one process may call `getUpdates` for a bot token at a time. A second instance
logs the conflict and does not take the surface down; the first one keeps it.

## Discord

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Add a bot and copy its token.
3. Enable the **Message Content** privileged intent for the application.
4. Put the token in `.env`:

   ```dotenv
   DISCORD_BOT_TOKEN=your-token
   ```

5. Enable it in `config.yaml`:

   ```yaml
   messaging:
     discord:
       enabled: true
   ```

6. Restart Aether, then send the bot a direct message.

Discord only answers direct messages. It ignores guild channels and other bots. As with
Telegram, the first DM gives the connector a channel to reply to. Until then,
approvals stay on the web panel.

Discord supports the same approval and result buttons, plus file attachments.

## Contact filtering

`contacts.*` decides whose messages and mail Aether may see. It is independent of the
approval policy.

```yaml
contacts:
  mode: enforce
  allowlist:
    - {platform: "*", handle: "friend@example.com"}
  telegram:
    mode: enforce
    allowlist:
      - {platform: telegram, handle: "@friend"}
  discord:
    mode: enforce
    allowlist:
      - {platform: discord, handle: "friend"}
  gmail:
    mode: enforce
    allowlist:
      - {platform: email, handle: "friend@example.com"}
```

With `enforce`, a non-allowlisted sender is dropped at ingestion: their message is not
stored, not shown to the model, and does not wake a turn. Gmail's list also filters bulk
inbox reads.

The Contacts page in the panel edits the same values.

## Screen capture

There is no action API for the screen. The companion captures an image, Aether describes
it with the image model, and stores the description as memory. Any later action
proposed from what it saw goes through the same policy gate as everything else.

Requirements:

- Wayland
- `grim` installed
- an image model configured (`llm.image_model`)

One-shot capture:

```bash
python companion/aether_snap.py --url http://localhost:8000 --token "$AETHER_TOKEN"
```

Daemon mode, which fulfills the agent's capture requests:

```bash
python companion/aether_snap.py --url http://localhost:8000 --token "$AETHER_TOKEN" --poll
```

A note can ride along:

```bash
python companion/aether_snap.py --token "$AETHER_TOKEN" --note "the deployment dashboard"
```

X11, macOS, and Windows capture are not implemented.

## Choosing a surface

- Use the web panel for setup, audit review, and traces.
- Use Telegram or Discord for approvals when you are away from the desk.
- Enable both if you want redundancy. Approving on one surface settles the request
  everywhere; a second tap is told the request was already decided.
