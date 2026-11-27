# Ingress recovery

Aether runs on one serving host. Provider pollers run under that host’s database leadership, including while the vault is locked. When the phone is offline, the host can still seal authenticated input to the ingress public key. After a host outage, recovery depends on provider replay and attachment availability. Aether reports uncertainty when it cannot establish coverage.

Set `AETHER_VAULT_INGRESS_IDENTITY_KEY` once when enabling Telegram or Discord ingress. Use 32 random bytes encoded as unpadded base64url. Keep this server-side secret stable across process restarts, bot credential rotation, and serving-host handoff.

## Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_INGRESS_OWNERS` in the serving host environment. The owner list is a JSON array of numeric Telegram user IDs. Capture admits direct messages whose sender and private chat ID match a configured owner. The provider-authenticated username is retained for existing contact rules. Receipt and notice identifiers use the installation identity key and are scoped to the bot account. Rotating the same bot’s credential preserves these identifiers.

The poller confirms a Telegram page only after all accepted updates on that page have durable encrypted receipts with their attachment bytes. On restart it requests unconfirmed updates. Telegram retains updates for up to 24 hours and can issue a new file URL through `getFile`. Live photos are captured as motion and still image files when both are supplied. An unsupported structured message, an unknown content field, or an incomplete attachment keeps the poller at the safe offset and is reported as incomplete coverage. Recovery after restart is reported as unconfirmed because an empty replay cannot establish what Telegram retained. [Telegram Bot API](https://core.telegram.org/bots/api#getting-updates) · [Telegram file retrieval](https://core.telegram.org/bots/api#getfile)

After a locked vault receives a durable receipt from a verified owner, Aether can send a catalog lock notice. It records the attempt before dispatch. A lost or ambiguous response leaves the notice unconfirmed for that lock episode and suppresses a repeat attempt. [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage)

## Discord

Set `DISCORD_BOT_TOKEN` and `DISCORD_INGRESS_OWNERS` in the serving host environment. The owner list is a JSON array of numeric Discord user IDs. The provider-authenticated account name is retained for existing contact rules. Each owner’s direct-message discovery and history retry runs independently. On restart Aether finds each conversation’s latest durable receipt and reads later messages oldest first. Rotating the same bot’s credential preserves the receipt lookup and lock-notice history. Each message and its attachments commit before the scan checkpoint advances. Retrying history obtains fresh attachment URLs. Deleted messages or missing attachment bytes can leave a gap, which Aether reports as uncertainty. [Discord message history](https://docs.discord.com/developers/resources/message#get-channel-messages)

Aether reserves a receipt-keyed lock notice before sending and uses Discord's nonce enforcement for provider retries. Discord checks the nonce within a short window. Aether retains an unconfirmed record if the outcome is unknown. [Discord Create Message](https://docs.discord.com/developers/resources/message#create-message)

## Panel chat

Open `/ws` from the pinned origin and send the memory-only panel token in the initial JSON frame as `{"token":"…"}`. Subsequent frames carry `id` and `text`. Use a new random UUID v4 for each message and reuse the same ID and text on retry until the server returns `{"type":"receipt","id":"…"}`. Acknowledge means the encrypted receipt committed. The client still needs its own copy until acknowledgement. Session revocation or expiry closes the socket on the next submission.

## Other sources

The repository has no dedicated Slack messenger adapter. Connected apps run through MCP and require their own receipt contracts. No general incoming messenger webhook is currently registered.

## Private operations

| Entry point | Admission and data | Destination |
| --- | --- | --- |
| Public health | Service availability | Requesting client |
| Panel chat | Pinned origin and active panel session. Validate each submission before storing an encrypted receipt. | Receipt to the submitting socket |
| Panel feeds, workspace, tasks and media | Active owner session and route-specific read lease. Recheck the session before sending the response. | Requesting panel session |
| Panel memory search | Owner query and scoped indexes. The configured Gemini embedding processor receives the query under a separate dispatch lease. | Fixed Gemini HTTPS endpoint for the configured model. The response returns to the requesting panel session. |
| Ingress drain | Verified source and a scoped lease for the encrypted receipt and destination records. | Sealed events, attachment storage and the inbound ledger |
| Scheduled workspace operations | Named due job and the local workspace tool allowlist. | Sealed workspace records |
| Generic model calls, connector tools, messaging fanout, exports and workbench | Private work denies these paths unless a dedicated scoped operation is implemented. | No external delivery |

A chat receipt confirms capture. The serving runtime currently records inbound messages for later processing and does not run a general conversational agent or send replies from those records.
