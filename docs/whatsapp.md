# WhatsApp — documented stub, not yet wired

Aether ships native Telegram, Discord, and Slack connectors. WhatsApp is
deliberately **docs-only**: there is no free, official, personal-use route
to it today, so instead of shipping something that breaks the first week,
this page documents both realistic routes and exactly where the code
would go.

## Why it's a stub

WhatsApp has no free API for personal automation. The official path —
the **WhatsApp Business Platform (Cloud API)** — is built for businesses
and bills per conversation. What *is* free is Meta's development tier,
which comes with real restrictions; the alternative is an unofficial
bridge, which has none of the restrictions and all of the risk.

## Route A — Meta test number (official, free, capped)

The Cloud API in a Meta **development-mode app** lets you send messages
from a test number for free, but only to **up to 5 verified recipient
numbers**. For a personal agent that watches *your* WhatsApp, that cap
is actually fine: you, and at most a handful of people who agree to be
messaged by your agent.

Setup outline:

1. Create an app at [developers.facebook.com](https://developers.facebook.com) →
   type *Business* → add the *WhatsApp* product.
2. Copy the **test number's** permanent token and phone-number ID
   (dashboard → WhatsApp → API Setup).
3. Verify up to 5 recipient numbers (a code by SMS/voicecall).
4. Configure a **webhook** for inbound messages. Aether already runs on a
   public URL when deployed (`https://<your-app>.onrender.com`), so this
   is the same shape as any other webhook receiver. Verify with Meta's
   challenge handshake.
5. Receive → Aether; send → Graph API `POST /{phone-number-id}/messages`.

Honest limits: 5 recipients; template messages outside a 24-hour
customer-service window; Meta can change dev-tier terms at any time.

## Route B — WAHA or an equivalent QR bridge (unofficial, unlimited, at your own risk)

[WAHA](https://waha.dev) (and similar projects) runs a WhatsApp *web*
session in a sidecar container and exposes a small HTTP API: you scan a
QR code once with your normal WhatsApp account, and it can then send and
receive on your behalf, with **no recipient cap** and no Meta review.

- **What's good about it**: unlimited, personal, free, self-hosted.
- **What's not**: it is against WhatsApp's Terms of Service. Accounts
  doing automation this way are banned regularly, and a ban takes the
  phone number with it. Run it on a burner number, or accept the risk
  on your own.
- Setup outline: run WAHA (`docker run -p 3000:3000/tcp waha`), open its
  UI, scan the QR, then poll its `/api/messages` or subscribe to its
  webhooks for inbound and `POST /api/sendText` for outbound.

**If you use this route, keep it to a number whose ban would not hurt.**

## Where the connector would go

Everything around a connector is already in place and platform-agnostic:

- `MessagingConnector` (`src/aether/connectors/base.py`) — the shared
  base class. A WhatsApp connector subclasses it and provides:
  - `send_to_user(text)` — outbound message to your number;
  - `present_approval(approval_id, tool_name, summary)` — WhatsApp's
    buttons/interactive messages for the one-tap Approve/Deny;
  - `start()` / `stop()` — webhook receiver or WAHA poller;
  - inbound messages → `self._emit_inbound(...)` with
    `surface="whatsapp"` — approval button presses → `self._decide(...)`,
    which already handles fresh-vs-stale decisions for you.
- Wiring happens in `build_messaging_connectors`
  (`src/aether/connectors/__init__.py`): a `whatsapp` toggle in
  `config.yaml` plus `WHATSAPP_*` secrets in `.env`.
- Nothing else needs to change: the **contact allowlist** already
  enforces at ingestion for every surface (a non-allowlisted WhatsApp
  sender is dropped before their message is ever stored), approvals and
  the audit chain are surface-agnostic, and the web panel works
  regardless.

The Meta test tier is inherently recipient-gated (the 5 verified
numbers); the allowlist brings the same control to every other channel.
