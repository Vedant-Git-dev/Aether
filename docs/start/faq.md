---
title: "FAQ"
summary: "Short answers about hosting, data, restarts, cost, and what Aether does with your accounts."
read_when:
  - You want a quick answer before reading a full guide
---

# FAQ

## Does it run on my laptop?

Yes. You need Python 3.12+, Node 22 to build the panel, and Postgres (local or free
Neon). Screen capture needs Wayland and `grim`; everything else is platform-independent.
See [Getting started](../quickstart.md).

## Is there a hosted version?

No. There is no hosted instance and no SQLite mode. Everything runs on your machine or in
your own cloud account. The process is one asyncio app and Postgres.

## Which data goes to which provider?

Your LLM sees prompts, context, tool results, and files in a perception call. Embeddings
go to Gemini, searches to Tavily, image prompts to Pollinations or Gemini, and voice notes
to Groq when no audio model is set. Sandboxes receive only their task files. OAuth tokens
stay in the encrypted store. [Models](../models/index.md) covers the slots.

## What happens if the server restarts?

Encrypted rows persist. The scheduler re-arms, a running background task returns to
pending, and approvals keep their expiry clock. An in-flight model turn is lost and not
retried automatically. Chat and panel settings continue to override `config.yaml`.

## Can a model decide to do something on its own?

It can propose anything, but code decides. Deterministic policy classifies every call,
and an unrecognized tool parks for a tap rather than running. See
[Policy](../concepts/policy.md).

## How do I stop Aether from seeing some senders?

Contact filtering is separate from tool authorization and drops non-allowlisted senders
at ingestion, before storage or the model. Configure it in `contacts.*` or on the panel's
Contacts page; [Channels](../channels/index.md) has the shape.

## What does it cost on free tiers?

Neon has a free Postgres tier, Render has a free web tier with 750 instance-hours and
spin-down, Tavily has free search credits, and Modal and Vercel have free starting
credits. Model tokens are billed by your provider. GitHub's free plan includes 2,000
Actions minutes for the batch queue. Terms change, so check current pricing.

## Where do I report a vulnerability?

Through [SECURITY.md](../../SECURITY.md), never a public issue.
