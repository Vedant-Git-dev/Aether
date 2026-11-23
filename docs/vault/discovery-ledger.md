# Discovery ledger

This discovery record describes existing storage, data flows, and authorization
checks and records the selected vault design.

Method: the final schema was materialised from the tracked migrations into a
throwaway database and compared with
`memory/allowlist.py`. Tracked-source searches covered `Cipher`, `TokenKeyring`,
`encrypt_`, `decrypt_`, `blind_token`, `dedup_mac`, `ENCRYPTION_KEY`,
`asyncpg|fetchrow|.execute(|.fetch(`, upload/temp paths, route auth, network
sinks and serializers. No live database, `.env` or `config.yaml` was read.

## 1. Key families and every caller

| Family | Where it lives today | Who holds it | Callers |
|---|---|---|---|
| Content key (active + previous) | `AETHER_ENCRYPTION_KEY[_PREVIOUS]`, ids `k1`/custom | serving environment; setup/deploy scripts generate and copy it | `main.py:_build_cipher`; every store taking `Cipher` |
| Provider-token key (active + previous) | `AETHER_TOKEN_ENCRYPTION_KEY[_PREVIOUS]` | serving environment; setup/deploy scripts | `providers/crypto.py:TokenKeyring`; `providers/store.py`; `providers/rotate_tokens.py` |
| Dev throwaway content key | generated in process | nobody (lost on restart) | `main.py:_build_cipher` when `AETHER_DEV_EPHEMERAL_KEY=1` |
| Panel bearer | `AETHER_TOKEN` env | serving environment; browser `localStorage` | `api/routes.py:_authorized`, `chat/ws.py:_authorized` |
| Provider/LLM credentials | `.env`, `app_secrets`, `provider_connections` | serving environment + content/token keys | `secret_env.EnvResolver`, `providers/runtime.py`, `llm/*` |

Facts that matter for the vault:

- The content key covers general storage. Provider tokens and PKCE use a
  separate token key. A database copy and both configured key families can
  open the corresponding records.
- `Cipher` derives per-purpose subkeys with HKDF-SHA256 from the master, binds
  `table:column:record` (and sometimes a name/path/provider) into AAD, and
  writes `AE1\0` envelopes. It still reads legacy bare-nonce blobs. The vault
  uses the `AE2` envelope.
- `Cipher.blind_token` is HMAC-SHA256 under an HKDF subkey salted with the
  active key id and bound to a purpose string. Tokens are therefore
  key-versioned and must be retokenised on rotation.
- `TokenKeyring` adds the key id to the AAD (`prefix:key_id`) and stores
  `key_id` beside token ciphertext. `oauth_tokens` (migration 005) has no
  current writer but is still in the registry and must be migrated.
- No key material is currently in the database, which is the one property the
  vault design preserves: only wraps and public pins may live in the database.

## 2. Storage registry (final schema, 38 application tables + migration bookkeeping + 1 view)

Classes are `plaintext` (allowlisted opaque value), `sealed` (ciphertext,
`bytea`), or `token` (keyed blind index, allowlisted because it is opaque).
The authoritative machine-readable list is `memory/allowlist.py`; this table is
the human reconciliation. Every sealed column is written through a store that
holds a `Cipher` today; after 1d the handle comes from a `KeyScope`.

Categories follow the approved category table.

### memory

| Table | Primary key | Plaintext (why) | Sealed | Keyed tokens / dedup | AAD |
|---|---|---|---|---|---|
| `events` | `id` | `id`, `key_version`, `indexed_at`, `created_at` | `payload_enc`, `meta_enc` | `source_token` (source), `dedup_mac` (content) | `events:<col>:<id>` |
| `identities` | `id` | `id`, `created_at`, `merged_into_id`, `key_version` | `profile_enc` | — | `identities:<col>:<id>` |
| `identity_handles` | `id` | `id`, `identity_id`, `created_at`, `key_version` | `handle_enc` | `platform_token` (platform), `normalized_token` (handle) | `identity_handles:<col>:<id>` |
| `entity_notes` | `id` | `id`, `identity_id`, `created_at`, `key_version` | `payload_enc` | — | `entity_notes:<col>:<id>` |
| `chat_messages` | `id` | `id`, `direction`, `created_at`, `key_version` | `payload_enc` | `surface_token` (surface) | `chat_messages:<col>:<id>` |
| `memory_chunks` | `id` | `id`, `event_id`, `key_version`, `dims`, `ordinal`, `created_at` | `text_enc`, `embedding_enc`, `meta_enc` | `embed_model_token` (embed model) | `chunks:text_enc:<id>` / `chunks:embedding_enc:<id>`; metadata uses `memory_chunks:meta_enc:<id>` |
| `facts` | `id` | `id`, `subject_identity_id`, `status`, `created_at`, `key_version` | `body_enc` | `key_token` (fact key) | `facts:<col>:<id>` |
| `summaries` | `id` | `id`, `kind`, `created_at`, `key_version` | `body_enc` | `period_token` (period) | `summaries:<col>:<id>` |
| `commitments` | `id` | `id`, `kind`, `status`, `counterparty_identity_id`, `source_event_id`, `created_at`, `key_version` | `body_enc` | — | `commitments:<col>:<id>` |
| `commitment_reminders` | `id` | `id`, `commitment_id`, `lead_minutes`, `sent_at` | — | — | — |
| `conclusions` | `key_token` | `created_at`, `key_version` | `value_enc` | `key_token` (conclusion key) | `conclusions:value_enc:<key_token.hex()>` |
| `focus` | `chat_ref_token` | `updated_at`, `key_version` | `state_enc` | `chat_ref_token` (chat ref) | `focus:state_enc:<token.hex()>` |
| `forgotten_events` | `dedup_mac` | `forgotten_at` | — | `dedup_mac`, `source_token` | — |
| `held_notes` | `id` | `id`, `created_at`, `key_version` | `body_enc` | — | `held_notes:<col>:<id>` |

`commitment_reminders` stores scheduling metadata. Its linkage and timing require a target classification in 1d.
`forgotten_events` is the one place a tombstone must survive with no
reconstructible plaintext; rotation must preserve suppression (a risk noted
in the approved design).

### workspace

| Table | Primary key | Plaintext | Sealed | Keyed tokens | AAD |
|---|---|---|---|---|---|
| `workspace_files` | `id` (unique `name_token`) | `id`, `updated_at`, `key_version` | `name_enc`, `content_enc` | `name_token` (file name) | `workspace_files:<col>:<id>` |

The old disk path is `workspace.path` (`aether-workspace/`). `Workspace.
import_from_disk` reads plaintext or `aether-workspace\x01` files, writes sealed
rows, then removes each file after the row is written. `workspace/transfer.py`
uses an independent passphrase (`scrypt` + AES-256-GCM, `AETHWSP1`), not the
content key. Export bundles are user files and must be treated as sensitive.

### operations

| Table | Primary key | Plaintext | Sealed | Keyed tokens | AAD |
|---|---|---|---|---|---|
| `pending_approvals` | `id` | `id`, `tool_name`, `status`, `created_at`, `decided_at`, `expires_at`, `decided_by`, `media_id`, `key_version` | `params_enc`, `rules_matched_enc`, `note_enc` | — | `pending_approvals:<col>:<id>` |
| `scheduled_actions` | `id` | `id`, `status`, `result_digest`, `created_at`, `key_version` | `payload_enc`, `meta_enc` | — | `scheduled_actions:<col>:<id>` |
| `routines_legacy` | `id` | `id`, `enabled`, `cooldown_seconds`, `fire_count`, `last_fired_at`, `created_at`, `key_version` | `label_enc`, `trigger_enc`, `action_enc` | — | `routines:<col>:<id>` |
| `alerts` | `id` | `id`, `channel`, `status`, `converted_from`, `last_event_id`, `last_run_at`, `last_notified_at`, `created_at`, `key_version` | `spec_enc`, `origin_enc` | `scope_token` (scope) | `alerts:<col>:<id>` |
| `alert_notifications` | `id` | `id`, `alert_id`, `event_id`, `status`, `style`, `reason`, `urgent`, `attempts`, `next_try_at`, `created_at`, `updated_at`, `key_version` | `event_enc` | `key_token` (dedup key) | `alert_notifications:<col>:<id>` |
| `decision_traces` | `id` | `id`, `kind`, `created_at`, `key_version` | `label_enc`, `trace_enc` | — | `decision_traces:<col>:<id>` |
| `deliveries` | `id` | `id`, `status`, `created_at`, `key_version` | `body_enc`, `message_id_enc`, `error_enc` | `surface_token` (surface) | `deliveries:<col>:<id>` |
| `proactive_tasks` | `id` | `id`, `skill`, `status`, `feedback`, `provider`, `created_at`, `updated_at`, `media_id`, `key_version` | `label_enc`, `meta_enc`, `opportunity_enc`, `plan_enc`, `result_enc`, `feedback_note_enc` | `day_token` (day), `opportunity_token` (opportunity) | `proactive_tasks:<col>:<id>` |
| `inbound_messages` | `id` | `id`, `surface`, `status`, `checked`, `created_at`, `updated_at`, `key_version` | `reason_enc`, `outcome_enc` | `chat_token` (chat), `message_token` (message), `key_token` (dedup key) | `inbound_messages:<col>:<id>` |
| `processed_updates` | `id` (unique `source,update_id`) | all columns | — | — | — |
| `connector_cursors` | `source` | `source`, `key_version` | `last_seen_enc` | — | `connector_cursors:last_seen_enc:<source>` |
| `workbench_transfers` | `id` (unique `token_hash`) | `id`, `token_hash`, `kind`, `used`, `expires_at`, `created_at`, `key_version` | `payload_enc` | `token_hash` is a one-way hash of a capability token | `workbench:transfer:<token_hash.hex()>` |
| `workbench_minutes` | `month` | all columns | — | — | — |
| `daily_counters` | `day` | all columns | — | — | — |
| `agent_settings` | `id` | `id`, `updated_at`, `key_version` | `personality_enc` | — | `agent_settings:<col>:<id>` |
| `config_overrides` | `path` | `path`, `updated_at`, `key_version` | `value_enc` | — | `config_overrides:value_enc:<path>` |
| `audit_log` | `seq` | `seq`, `actor`, `tool_name`, `decision`, `rules_matched`, `params_digest`, `outcome`, `prev_hash`, `entry_hash`, `created_at` | — (1e adds a sealed canonical payload) | — | chain hash |

`daily_counters` and `workbench_minutes` are aggregate usage records. `processed_updates.update_id` is a source-provided ID and requires protection under the approved design. `audit_log` accepts caller-provided strings: for example, approval outcomes interpolate `decided_by`. Its unkeyed parameter digest allows offline guesses. Task 1e must seal original canonical entries and anchor the legacy head.

### media

| Table | Primary key | Plaintext | Sealed | Keyed tokens | AAD |
|---|---|---|---|---|---|
| `media_blobs` | `id` | `id`, `size`, `created_at`, `last_used_at`, `key_version` | `bytes_enc`, `meta_enc` | `origin_token` (origin), `dedup_mac` (bytes) | `media:bytes_enc:<id>`; metadata uses `media_blobs:meta_enc:<id>` |

Metadata (filename, media type, dimensions, duration) is inside `meta_enc`;
`size` is unavoidable ciphertext length. Uploads arrive through
`api/routes.py` multipart (`UploadFile`) and may spool before endpoint code.

### credentials

| Table | Primary key | Plaintext | Sealed | Keyed tokens | AAD |
|---|---|---|---|---|---|
| `provider_connections` | `provider` | `provider`, `auth_mode`, `token_type`, `scopes`, `key_id`, `expires_at`, `refresh_expires_at`, `status`, `last_refreshed_at`, `created_at`, `updated_at`, `key_version` | `access_enc`, `refresh_enc`, `account_label_enc`, `account_id_enc`, `last_error_enc` | — | `provider_connections:<col>:<provider>` (token fields add `:key_id`) |
| `provider_oauth_states` | `state_hash` | `state_hash`, `provider`, `app`, `scopes`, `key_id`, `redirect_uri`, `created_at`, `expires_at`, `consumed_at` | `code_verifier_enc` | `state_hash` is SHA-256 of a random state | `provider_oauth_states:code_verifier_enc:<hash>` (`:key_id`) |
| `provider_sync_state` | `provider,app,key` | `provider`, `app`, `key`, `updated_at`, `key_version` | `value_enc` | — | `provider_sync_state:value_enc:<p>\x1f<a>\x1f<k>` |
| `oauth_tokens` | `provider` | `provider`, `scopes`, `expires_at`, `updated_at`, `key_version` | `access_enc`, `refresh_enc` | — | `oauth_tokens:<col>:<provider>` |
| `app_secrets` | `name` | `name`, `updated_at`, `key_version` | `value_enc` | — | `app_secrets:value:<name>` |

`provider_connections.scopes`, `provider_oauth_states.scopes` and
`app_secrets.name` are configuration vocabulary, not user content. Provider
scopes can still reveal which services are connected; the residual-metadata
disclosure must name them.

### bookkeeping (no category lease)

`schema_migrations` (public schema version) and the new `vault_*` tables
(public pins, signed manifests, hashed tickets/challenges/sessions, opaque
queue ciphertext) carry no user content and no private key. `vault_*` is
introduced by 1c.

## 3. Keyed-lookup and join inventory

Equality joins that must keep working after sealing:

| Lookup | Token domain | Writers | Readers |
|---|---|---|---|
| event source and content dedup | `source`, dedup bytes | `memory/events.py` | `events`, `memory/control.py`, `memory/views.py` |
| identity handle | `platform`, `handle` | `memory/entities.py` | `entities`, `memory/views.py`, mail filter |
| chat surface | `source`, raw surface | `chat/ws.py` | chat history; no equality join to `delivery` token |
| fact key | `fact_key`, identity ID or 0 + unit separator + collapsed lowercase subject + unit separator + normalized fact key | `memory/facts.py` | facts, context builder |
| summary period | `summary_period`, kind + unit separator + period | `memory/summaries.py` | summaries, retrieval |
| focus chat ref | `focus` | `memory/focus.py` | focus |
| conclusion key | `conclusion` | `memory/conclusions.py` | conclusions |
| delivery surface | `delivery` | `memory/deliveries.py` | deliveries, fanout |
| workspace name | `workspace` | `workspace/store.py`, `memory/storage.py` | workspace |
| embedding model | `embed_model` | `memory/vectors.py` | vector recall |
| media origin and dedup | `source`, dedup bytes | `media/store.py` | media, attachments |
| proactive day/opportunity | `day`, `opportunity` | `proactive/store.py` | proactive worker |
| alert scope | `alert_scope` | `alerts/store.py` | alert engine |
| inbound chat/message/dedup key | `inbound-chat`, `inbound-message`, `inbound-key` | `memory/inbound.py` | inbound states |
| alert notification key | `alert_notification` | `alerts/notifications.py` | notifications |

Rotation rule: a token is bound to the active key version, so every listed
column must be retokenised before equality lookups move to the new version.
If rotation cannot preserve suppression for `forgotten_events`, obtain an owner
decision. The original token inputs cannot be reconstructed.

## 4. Reads, sends and trusted principals

Route auth today is one shared bearer (`x-aether-token`, also accepted as
`?token=` by `api/routes.py:_authorized` and `chat/ws.py`). Every private route
resolves `request.app.state` stores directly. That is the cross-scope risk the
vault design removes; the matrix below is the target principal per surface.

| Surface | Current path | Data touched | Target principal / operation |
|---|---|---|---|
| Panel reads (`/api/events`, `/memory*`, `/workspace*`, `/tasks`, `/alerts`, `/traces`, `/audit`, `/profile`) | `api/routes.py` | memory, workspace, operations | `panel_read` after session validation, typed projection |
| Panel mutations (`/approvals/*/decide`, `/memory/forget`, `/workspace/{file}`, `/contacts*`, `/settings*`, `/alerts/{id}`, `/proactive/*/decide`) | `api/routes.py` | operations, workspace, config | `panel_write` (credentials via `credential_write`) |
| Chat WS | `chat/ws.py` | chat messages, memory | `panel_read`/`panel_write` equivalent, one-use WS ticket (4a) |
| Screen capture / media upload | `api/screen-capture`, `/api/media`, `/api/workbench/*` | media | `panel_write` with media; multipart must not spool (2d) |
| Media download | `/api/media/{id}` | media | `panel_read` for one concrete item |
| Agent turn | `agent/loop.py` | memory, workspace, operations | `turn`, bound to conversation and origin |
| Approved tool dispatch | `agent/tools.py`, `authz/policy.py`, `outbound.py`, `tools/grants.py` | operations + credentials + media | `approved_dispatch`, exact sink and payload |
| Scheduler | `scheduler/jobs.py` | operations, memory, workspace | `scheduled` |
| Proactive worker | `proactive/worker.py` | operations, memory, workspace | `scheduled` (egress separately) |
| Connector ingest | `connectors/` polling, gateway, and attachment handlers | ingress queue, memory, media | `drain` (ingress + destination) |
| Provider refresh | `providers/runtime.py`, `oauth.py` | credentials | `approved_dispatch` / `credential_write` |
| Workspace export/import | `/api/workspace/export`, `/import` | workspace | `panel_read`/`panel_write`, explicit owner gesture |
| Workbench transfer | `/api/workbench/{kind}/{token}` | media, workspace | capability token only; must stay distinct from panel auth |
| OAuth callback | `/api/oauth/{provider}/callback` | credentials | state-bound, distinct from panel auth |
| Boot health | `/healthz`, `/health`, `/v1/models`, `/` | none | public coarse status only |

Network sinks that must be authorised by destination and payload, not by
model intent: `web.py` (fetch/search), `llm/*` providers, `outbound.py`
(messengers), `connectors/mcp_host.py`, `workbench/` sandbox adapters, `media/generate.py`, `providers/*`. A model with private
context must not be able to choose an arbitrary destination.

## 5. Boot-time plaintext residency

`main.py` lifespan, in order: validation -> `_build_cipher` -> token keyring ->
pool -> migrations -> schema audit -> `SecretStore` + `EnvResolver` ->
`settings_with_resolved_keys` -> `ConfigManager.bootstrap` (decrypts every
`config_overrides` row) -> policy -> counters/web/workbench -> vector cache load
(`memory_chunks` embeddings) -> `MemoryIndex.rebuild` (decrypts event payloads
and metadata) -> `LexicalIndex.rebuild` (decrypts chunk text) -> provider
registration -> media store -> providers/LLM -> entities/salience/facts/
commitments/alerts/summaries/views/focus/conclusions/deliveries/inbound ->
workspace import/scaffold -> chat history -> agent -> alerts refresh ->
proactive refresh -> native tools -> proactive worker -> connectors start.

Consequences for 2c: a cold boot currently needs the content key before it can
answer anything. The vault design splits public boot (health, schema, pairing,
ingress) from admitted keyed work. Indexes hold decrypted state in process
memory and must be dropped on lease expiry and rebuilt on reacquire. The
`VectorMemory` cache and both BM25/event indexes are the largest plaintext
residencies.

## 6. Workspace, uploads and temporary files

| Path | Writer | Sensitivity |
|---|---|---|
| `aether-workspace/*.md`, `aether-workspace/memory/*.md` | `Workspace.import_from_disk` (legacy), operator edits | user content |
| `webapp/` build output `src/aether/web/` | `npm run build` | public assets; must contain no keys or fixtures |
| multipart `UploadFile` spool | Starlette/FastAPI before endpoint code | attachment bytes; must move to streamed ingress (2d) |
| workspace export bundle | `workspace/transfer.py` | passphrase-encrypted user content |
| workbench transfer payload | `workbench/transfers.py` | sealed in DB, single-use URL |
| `.env`, `deploy-secrets*`, setup artifacts | setup/deploy scripts | **live keys today**; removed from the server path in 8b |

## 7. Connector acknowledgement and catch-up

| Connector | Current ack point | Durable record | Gap |
|---|---|---|---|
| Long-poll messaging | SDK long-poll offset is in memory; `ProcessedUpdates.record` runs before ingestion completes | `processed_updates` | a crash after `record` and before ingest loses the update; no attachment spool/replay |
| Gateway messaging | gateway event handled in-process | no durable receipt marker. The forwarded event omits its original message ID | stable replay identity and catch-up remain unproven |
| Mail/provider polling | `provider_sync_state` cursor advanced after processing (`providers/runtime.py`) | `provider_sync_state`, `processed_updates` | lookback sweep exists; attachment URL expiry and partial pages not proven |
| MCP connected apps | tool call result | none | no receipt semantics; not a messenger source |
| Panel/WS | request lifetime | `chat_messages` | in-process only; no ingress queue |
| Messaging source without an adapter | no dedicated adapter in `src/aether/connectors/` | — | parity must not be promised until inventoried |

The target is `vault_ingress_queue` + parts, write-before-ack, stable source
identity inside the envelope, and an outbox for the lock notice (2d/2e).

## 8. Key provisioning, backups and bootstrap exceptions

Current provisioning paths that generate or preserve decryption material:

- `setup/artifacts.py` generates `AETHER_ENCRYPTION_KEY` and
  `AETHER_TOKEN_ENCRYPTION_KEY` and emits them into deployment artifacts.
- `setup/plan.py`, `setup/wizard.py`, `setup/requirements.py` require and carry
  the keys; `.env.example` and `config.example.yaml` document them.
- `scripts/deploy.sh` and `scripts/deploy.bat` generate keys, push them to the
  serving host, write `deploy-secrets` files and print backup instructions.
- the deployment blueprint materialises base64 config at build time.
- `providers/rotate_tokens.py` performs offline token-key rotation.

Old backup owners (all retain pre-cutover decryption ability until retired):

| Owner | Material | Action required |
|---|---|---|
| Deployment account owner | service env vars, build artifacts, logs | remove content/token keys after cutover; retain only public bootstrap config |
| Database account owner | database backups and point-in-time recovery | old backups remain decryptable with old keys; apply the signed-off cutover policy in section 12 |
| Operator workstation | `.env`, `deploy-secrets*`, shell history | rotate and delete after cutover |
| Password manager | backed-up keys | apply section 12 at signed-off cutover |
| Setup wizard artifacts | base64 config bundles | stop generating private config |

Bootstrap exceptions that may stay unauthenticated (each must be re-scoped in
4a/2f): `/healthz`, `/health`, `/v1/models`, `/` panel shell,
`/api/workbench/{kind}/{token}` (single-use capability), and
`/api/oauth/{provider}/callback` (state-bound). Everything else is currently
behind the shared bearer and must become a scoped session.

## 9. Decision check (D1-D9)

| Decision | Finding | Verdict |
|---|---|---|
| D1 direct shells | No `mobile/` tree exists; panel is a Vite build served by FastAPI. Direct Kotlin WebView is compatible. | proceed |
| D2 automatic delivery | The existing messaging handlers run in-process. Background push and foreground-service handling are separate vault components. | wake and fetch observed on a physical handset; complete unattended grant remains a product integration check |
| D3 HPKE + ES256 | Feasibility selected HPKE with associated data and JWS with pinned algorithms. | Python vectors and native synthetic grant opening passed. See the platform assurance record. |
| D4 custody | No native code yet. Recovery wraps restore the data keys under a fresh non-exportable device master. | TEE custody observed on a physical handset |
| D5 scopes | Existing code has no lease; stores hold a long-lived `Cipher`. The proposed categories map to the inventory. Verify required lookups and task boundaries before converting each family. | proceed with scoped discovery |
| D6 pairing | Only the shared `AETHER_TOKEN` exists; no enrollment, challenge, session or panel consent. | proceed (4a) |
| D7 recovery | No phrase or recovery kit. BIP39 provides phrase encoding and checksum. | proceed (6a) |
| D8 catch-up | Connector markers are written before ingestion; no queue or independent receiver. | blocked per source (2e.1) |
| D9 assurance | Hardware and background behavior verified on one phone; more devices would broaden coverage. | proceed; Android first |

## 10. Ownership and test IDs

Every registry group has one owner module and at least one existing test that
exercises its read/write path. New tests added by later work inherit the same
IDs. An entry with no test is marked `pending` and blocks its conversion work.

| Domain | Owner (writer/reader) | Test IDs | Status |
|---|---|---|---|
| content cipher | `memory/crypto.py` | `test_crypto.py`, `test_canary.py` | covered |
| token keyring | `providers/crypto.py`, `providers/store.py` | `test_provider_crypto.py`, `test_provider_store.py` | covered |
| memory: events/chunks/index | `memory/events.py`, `memory/vectors.py`, `memory/indexes.py`, `memory/lexical.py` | `test_memory_units.py`, `test_memory_index.py`, `test_lexical_integration.py`, `test_views.py` | covered |
| memory: identities/entities | `memory/entities.py` | `test_memory_units.py`, `test_enrich.py` | covered |
| memory: facts/summaries/commitments/conclusions/focus | `memory/{facts,summaries,commitments,conclusions,focus}.py` | `test_facts.py`, `test_summaries.py`, `test_commitments.py`, `test_conclusions.py`, `test_focus.py` | covered |
| memory: held notes | `memory/held_notes.py` | `test_held_notes.py` | covered |
| workspace | `workspace/store.py`, `memory/storage.py` | `test_workspace.py`, `test_canary.py` | covered |
| operations: approvals/traces/audit | `authz/{approvals,audit}.py`, `agent/traces.py` | `test_authz_approvals.py`, `test_authz_audit.py`, `test_traces.py` | covered |
| operations: scheduler/alerts/deliveries | `scheduler/jobs.py`, `alerts/*`, `memory/deliveries.py` | `test_scheduler.py`, `test_alerts.py`, `test_deliveries.py` | covered |
| operations: proactive/inbound/processed | `proactive/*`, `memory/inbound.py`, `connectors/processed.py` | `test_proactive_store.py`, `test_processed_updates_db.py` | covered |
| operations: config/settings/counters/workbench | `config_store.py`, `agent/settings.py`, `counters.py`, `workbench/*` | `test_config_store.py`, `test_agent_settings.py`, `test_counters.py`, `test_workbench_transfers.py` | covered |
| media | `media/store.py` | `test_media_store.py`, `test_media_*` | covered |
| credentials: provider tokens | `providers/store.py`, `providers/oauth.py` | `test_provider_store.py`, `test_provider_oauth.py` | covered |
| credentials: app secrets | `secret_store.py` | `test_secret_store.py` | covered |
| credentials: PKCE state | `providers/store.py` | `test_provider_store.py`, `test_provider_oauth.py` | covered |
| ingress queue | none yet | `pending` (2d) | blocked |
| routes/WS auth | `api/routes.py`, `chat/ws.py` | `test_api_routes.py`, `test_chat_ws.py`, `tests/e2e/test_plain_language.py` | covered |
| connectors/ack | `connectors/` polling, gateway, and receipt handlers | `test_polling.py`, `test_connectors_registry.py` | partial (pre-ingest marker) |
| boot/index residency | `main.py` | `test_main_app.py`, `test_required_config.py` | covered |
| schema guard | `memory/allowlist.py`, `memory/storage.py` | `test_storage_guard.py`, `scripts/check_storage_guard.py --strict` | covered |
| canary/leak scan | `agent/leaks.py`, `logging_setup.py` | `test_canary.py`, `test_logging_setup.py` | covered |
| setup/deploy key provisioning | `setup/*`, `scripts/deploy.*`, the deployment blueprint | `test_setup_wizard.py`, `test_setup_requirements.py` | covered (to be removed in 8b) |

## 11. Unclassified values, risks and conclusion

No persisted column is unclassified: the scratch schema audit matches
`PLAINTEXT_ALLOWLIST` and `SEALED_COLUMNS` for 38 application tables plus `schema_migrations`; `watches` is
a compatibility view. The following are *candidate residual metadata* for owner review. Existing schema allowlisting does not authorize retention in the vault:

- ciphertext lengths, row counts, table names, key versions, coarse statuses;
- `size` on `media_blobs`, `dims`/`ordinal` on `memory_chunks`;
- provider names, auth modes, scopes and token expiry times;
- aggregate `daily_counters`; protect source-provided update IDs;
- audit sequence numbers, reviewed fixed vocabulary and timing; seal dynamic actors, outcomes and digests;
- origin host and port in the pairing pin.

This reconciliation is provisional. The present-day schema is recorded here.
Before a store family enters 1d or a route/sink enters 2f, record its explicit
target classifications and source-level lookup, authorization and test
evidence. The owner accepted the public metadata list above.
In particular, classify `config_overrides.path`, `app_secrets.name`,
`pending_approvals.decided_by`, `scheduled_actions.result_digest`, provider
scope/redirect/sync keys and connector source IDs using their actual producers.
Seal arbitrary or user-derived values; use scoped keyed lookups only where
equality is needed.

## 12. Owner decisions

| Decision | Choice |
|---|---|
| Residual metadata | Accepted as listed in section 11; removing an item later becomes a 1d/1e task with a test. |
| Legacy backup retention | Delete old decryption material at cutover. Serving environment keys, database backups, `.env`/`deploy-secrets`, password-manager copies and setup artifacts are destroyed when the cutover is signed off. Pre-cutover copies remain exposed until then. |
| Android before iOS | Android first; production only after the agreed Android scope passes. |
| Ambiguous field classification, route/sink matrix, per-source acknowledgement contract, UI/egress inventory | Owner accepted the proposed approach; these are ledger work items, not open decisions. |


## 13. Route, sink and source matrix

### Route principals (target for 2f)

Generated from the `@router` decorators in `src/aether/api/routes.py` plus the
chat WebSocket. Today all of these share one bearer; the target column is the
scoped operation each route must map to. `panel_read`/`panel_write` require a
validated owner session; `credential_write` is a separate explicit operation.

| Method | Path | Target operation/principal |
|---|---|---|
| `POST` | `/api/screen-capture` | panel_write |
| `GET` | `/api/screen-request` | panel_read |
| `GET` | `/api/media/{media_id}` | panel_read |
| `POST` | `/api/media` | panel_write |
| `GET` | `/api/approvals` | panel_read |
| `POST` | `/api/approvals/{approval_id}/decide` | panel_write |
| `GET` | `/api/events` | panel_read |
| `GET` | `/api/policy` | panel_read |
| `GET` | `/api/config` | panel_read |
| `GET` | `/api/contacts` | panel_read |
| `POST` | `/api/contacts/allowlist` | panel_write |
| `DELETE` | `/api/contacts/allowlist` | panel_write |
| `PUT` | `/api/contacts/mode` | panel_write |
| `PUT` | `/api/contacts/apps/{key}/mode` | panel_write |
| `POST` | `/api/contacts/apps/{key}/allowlist` | panel_write |
| `DELETE` | `/api/contacts/apps/{key}/allowlist` | panel_write |
| `GET` | `/api/settings/fields` | panel_read |
| `PUT` | `/api/settings/fields/{path}` | panel_write |
| `DELETE` | `/api/settings/fields/{path}` | panel_write |
| `GET` | `/api/settings/models` | panel_read |
| `PUT` | `/api/settings/credentials/{name}` | credential_write |
| `GET` | `/api/settings/personality` | panel_read |
| `PUT` | `/api/settings/personality` | panel_write |
| `GET` | `/api/settings/apps` | panel_read |
| `GET` | `/api/audit` | panel_read |
| `GET` | `/api/traces` | panel_read |
| `GET` | `/api/traces/{trace_id}` | panel_read |
| `GET` | `/api/memory` | panel_read |
| `GET` | `/api/memory/overview` | panel_read |
| `GET` | `/api/memory/items` | panel_read |
| `GET` | `/api/memory/claims` | panel_read |
| `POST` | `/api/memory/forget` | panel_write |
| `GET` | `/api/tasks` | panel_read |
| `GET` | `/api/alerts` | panel_read |
| `PATCH` | `/api/alerts/{alert_id}` | panel_write |
| `DELETE` | `/api/alerts/{alert_id}` | panel_write |
| `GET` | `/api/proactive/tasks` | panel_read |
| `POST` | `/api/proactive/tasks/{task_id}/decide` | panel_write |
| `GET` | `/api/profile` | panel_read |
| `POST` | `/api/profile/confirm` | panel_write |
| `GET` | `/api/workspace` | panel_read |
| `PUT` | `/api/workspace/{file}` | panel_write |
| `GET` | `/api/workspace/daily` | panel_read |
| `GET` | `/api/workspace/search` | panel_read |
| `POST` | `/api/workspace/export` | panel_read with explicit owner gesture |
| `POST` | `/api/workspace/import` | panel_write |
| `GET` | `/api/workbench/{kind}/{token}` | scoped single-use workbench capability |
| `POST` | `/api/workbench/{kind}/{token}` | scoped single-use workbench capability |
| `GET` | `/api/providers` | panel_read |
| `GET` | `/api/providers/{provider}/setup` | panel_read |
| `PUT` | `/api/providers/{provider}/client` | credential_write |
| `GET` | `/api/providers/{provider}/picker` | panel_read |
| `PUT` | `/api/providers/{provider}/picker` | panel_write |
| `POST` | `/api/providers/{app}/picked` | panel_write |
| `POST` | `/api/providers/{app}/connect` | panel_write |
| `POST` | `/api/providers/{app}/token` | credential_write |
| `POST` | `/api/providers/{app}/disconnect` | panel_write |
| `GET` | `/api/oauth/{provider}/callback` | oauth callback (state-bound) |
| WS | `/ws (chat)` | one-use WS ticket bound to an owner panel session |

`/api/media/{id}` and binary downloads must resolve one concrete item under
`panel_read` and never return a generic decrypt handle. `/api/workspace/export`
is an explicit owner gesture. `/api/workbench/{kind}/{token}` keeps the
single-use capability token distinct from panel auth.

### Network sinks

Every sink is authorised by destination and payload, not by model intent.

| Sink | Where | Target operation/principal |
|---|---|---|
| Web fetch/search | `web.py` | `approved_dispatch`, allowlisted destination, redacted URL |
| LLM providers | `llm/*` | `turn`/`scheduled` with the turn's whole provenance label |
| Messengers | `outbound.py`, connectors | `approved_dispatch` to the owner surface only |
| MCP servers | `connectors/mcp_host.py` | `approved_dispatch`, configured server only |
| Workbench sandboxes | `workbench/` sandbox adapters | `approved_dispatch`, job payload version pinned |
| Image service | `media/generate.py` | `approved_dispatch`, prompt only, no private context |
| Provider APIs | `providers/*` | `approved_dispatch`/`credential_write` per declared app |

### Per-source acknowledgement contract

| Source | Ack point today | Durable record | Gap and target |
|---|---|---|---|
| Long-poll messaging | `ProcessedUpdates.record` runs before ingestion completes; SDK offset is in memory | `processed_updates` | Crash between mark and ingest loses the update. Target: write the sealed queue record before ack (2d), then advance the processed marker in the drain transaction (2e). |
| Gateway messaging | Gateway event handled in-process | no durable gateway receipt | Preserve the original source message ID. Target: the same queue-then-write order; record the gateway replay window. |
| Mail/provider polling | Cursor advanced after processing in `providers/runtime.py`; a lookback sweep exists | `provider_sync_state`, `processed_updates` | Attachment URL expiry and partial pages are unproven. Target: page checkpoint after every event and attachment has a durable encrypted receipt, previous checkpoint kept on failure (2e.1). |
| Messaging source without an adapter | No adapter in `src/aether/connectors/` | none | Unsupported until inventoried; do not claim messenger parity. |

### UI and egress inventory

| Path | Behaviour | Target |
|---|---|---|
| `webapp/src/lib/api.js` | sends `x-aether-token` from `localStorage` | one-use WS ticket plus scoped session; no token in web storage (3a/4a) |
| `webapp/src/pages/Settings.jsx` | persists the token | remove persistence; memory-only session |
| `webapp/src/store.jsx` | keeps prior results, ETags and page state | drop data and inflight responses on lock/logout/background (3a) |
| Cloud file picker | loads remote scripts | replace with a scoped backend list/select using granted scopes (4c) |
| media object URLs | blob URLs held by the page | revoke on lock and navigation (3a) |
| `webapp/src/pages/*` (Tasks, Alerts, Audit, Memory) | wide desktop layouts | mobile cards retaining all actions (3b) |
| `tests/e2e/test_plain_language.py` | enforces the no-raw-JSON contract | extend for locked/offline/loading states (3a) |

### Ambiguous field classification

Decision for each field the ledger flagged. "Plaintext" means opaque or
configuration vocabulary; "token" means a keyed blind index; "sealed" means
ciphertext.

| Field | Producer | Decision | Why |
|---|---|---|---|
| `config_overrides.path` | `config_store.py` | plaintext | fixed settings-catalog path, not user content |
| `app_secrets.name` | `secret_store.py` | sealed name plus keyed name lookup | SecretStore accepts caller-supplied names and supports listing them. |
| `pending_approvals.decided_by` | `authz/approvals.py` | plaintext only for validated fixed vocabulary; seal other values | The writer accepts a string. |
| `scheduled_actions.result_digest` | `scheduler/jobs.py` | sealed | Truncated, unkeyed SHA-256 of the result or exception allows offline guessing. |
| `provider_connections.scopes` | `providers/store.py` | plaintext | provider scope vocabulary; disclosed as metadata |
| `provider_oauth_states.scopes` | `providers/store.py` | plaintext | same |
| `provider_oauth_states.redirect_uri` | `providers/store.py` | plaintext | registered app redirect, not user data |
| `provider_sync_state.key` | `providers/store.py` | plaintext | provider-defined cursor name |
| `connector_cursors.source` | connector stores | plaintext | source vocabulary |
| `processed_updates.source`, `update_id` | `connectors/processed.py` | allowlisted source vocabulary and keyed source-scoped update ID | Preserve unique deduplication without exposing the source event ID. |
| `inbound_messages.surface` | `memory/inbound.py` | plaintext | surface vocabulary |

All other user-derived values are already keyed tokens or sealed columns per
`memory/allowlist.py`. Register and migrate the secret-name and source-update lookup domains in 1d/2e.
