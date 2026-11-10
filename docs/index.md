---
title: "Aether"
summary: "Aether is a self-hosted personal agent that checks every action against a policy you wrote and records the decision in a tamper-evident log."
read_when:
  - You are new to Aether and want the map of the documentation
  - You want to know where to start
---

# Aether

**A personal agent you can give write access to, because it shows its work.**

Aether connects to your apps, remembers what is happening across all of them, and takes
actions on your behalf, through native integrations and any MCP server. Deterministic
code, not the model, decides what is allowed to run: reads go ahead, anything that
sends, changes, or deletes waits for one tap in chat, and every decision lands in a
hash-chained audit log you can verify.

It is one asyncio process and a Postgres database. There is no hosted instance and no
vendor in the middle; everything runs on your machine or in your own cloud account.

## Start here

| Goal | Start here |
| --- | --- |
| Install Aether and send the first message | [Getting started](quickstart.md) |
| Look at the panel before connecting anything | [Try the panel with no database](quickstart.md#path-1-try-the-panel-with-no-database) |
| Run the setup wizard or deploy to Render | [Setup wizard](start/setup-wizard.md) |
| Understand the trust case and the limits | [Why Aether](start/why-aether.md) |
| Answer a common question | [FAQ](start/faq.md) |
| Fix a boot or channel problem | [Troubleshooting](help/troubleshooting.md) |

## Browse the docs

| Area | What it covers |
| --- | --- |
| [Getting started](quickstart.md) | Requirements, both install paths, first connectors, optional services. |
| [Why Aether](start/why-aether.md) | The policy gate, approvals, audit, and what Aether does not claim. |
| [Setup wizard](start/setup-wizard.md) | The keyboard-driven wizard, the answers file, the deploy engine, Render and Neon. |
| [Architecture](concepts/architecture.md) | Boot order, components, data flow, persistence, encryption, failure behavior. |
| [Policy](concepts/policy.md) | The deterministic classifier, the rule grammar, approvals. |
| [Sealed storage](concepts/sealed-storage.md) | What is encrypted at rest, the plaintext allowlist, key rotation. |
| [Channels](channels/index.md) | The web panel, Telegram, Discord, contact filtering, screen capture. |
| [Models](models/index.md) | Providers, model names, and the reasoning, image, audio, video, salience, and embedding slots. |
| [Workbench](tools/workbench.md) | Where sandbox jobs run and the provider ladder. |
| [Batch queue](tools/batch-queue.md) | Running background jobs in your own GitHub Actions queue. |
| [Configuration](reference/configuration.md) | Every environment variable and `config.yaml` field. |
| [Evaluation](reference/evaluation.md) | The golden set and the decisions it justifies. |
| [Troubleshooting](help/troubleshooting.md) | Boot failures, panel errors, channel problems, resetting. |

## How it fits together

- The **agent loop** is the turn engine: it reasons, calls a model, and proposes tool
  calls.
- The **policy gate** classifies every proposal in code and parks the risky ones.
- **Approvals** land on the web panel and on Telegram or Discord with the exact call.
- **Memory** stores deduplicated, encrypted events you can search, inspect, and forget.
- **Connectors** reach your apps, MCP servers, and sandboxes.

A full walkthrough is in [Architecture](concepts/architecture.md).
