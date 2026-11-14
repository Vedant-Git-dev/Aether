---
title: "Models and modality slots"
summary: "Providers, model names, and how the reasoning, image, audio, video, salience, and embedding slots route."
read_when:
  - You are choosing a provider or model
  - You want images, audio, video, or semantic memory
  - You are checking what a slot does
---

# Models and modality slots

Aether separates the model that reasons from the model that reads an image, hears
audio, watches video, scores salience, or makes embeddings. One provider key can cover
several slots, or each slot can route somewhere else.

## Providers

| Provider | Key | Notes |
| --- | --- | --- |
| Anthropic | `ANTHROPIC_API_KEY` | Claude models |
| OpenAI | `OPENAI_API_KEY` | GPT models |
| Google Gemini | `GEMINI_API_KEY` | Gemini models, and the embedding model |
| NVIDIA NIM | `NVIDIA_API_KEY` | NVIDIA's hosted model catalog |
| Groq | `GROQ_API_KEY` | Groq's hosted models, same key as voice-note transcription |
| Ollama | none, uses `OLLAMA_BASE_URL` | Local models; defaults to `http://localhost:11434/v1` |

The main model is built from `llm.provider` and `llm.model`. If no matching key is
present, the agent still boots, but chat replies, salience judging, and screen vision
stay off with a plain log line. The panel and audit remain usable.

## Model names

A model value can carry its own provider:

```yaml
llm:
  provider: anthropic
  model: openai/gpt-5.1
```

The first slash is the provider seam. The rest is the provider's own model id, so
`openai/gpt/gpt-5.6` means OpenAI with the id `gpt/gpt-5.6`.

A bare id uses `llm.provider`:

```yaml
llm:
  provider: gemini
  model: gemini-2.5-pro
```

Under `provider: nvidia` or `provider: groq`, a prefix that is not a known provider is
treated as part of the model id, because both carry a vendor slash:

```yaml
llm:
  provider: nvidia
  model: meta/llama-3.1-405b-instruct
```

```yaml
llm:
  provider: groq
  model: meta-llama/llama-4-scout-17b-16e-instruct
```

Groq ids whose first segment is itself a provider name (`openai/gpt-oss-120b`) are the
one exception: a bare id like that reads as that provider. Prefix it with `groq/` — the
form the model dropdown saves — to keep it on Groq.

### Groq and per-minute token limits

Groq admits a request against the model's tokens-per-minute budget using the prompt plus
the requested maximum output, so a large `max_tokens` can be refused before a single
word is generated. Aether's Groq adapter sends no token cap at all, letting the model's
own ceiling apply. Free-tier Groq models still have low per-minute budgets, so a very
long conversation can be refused; the reply says so plainly and retries when the refusal
looks temporary. A Developer-tier key raises the ceiling.

The three sentinel words are meanings, never model ids: `off`, `default`, and the
legacy spelling `base`.

## Slots

| Slot | Key | Empty | `default` | Named model |
| --- | --- | --- | --- | --- |
| Reasoning | `llm.model` | invalid; required | not applicable | yes |
| Images | `llm.image_model` | medium off | main model handles it | route to another model |
| Audio | `llm.audio_model` | medium off | main model handles it | route to another model |
| Video | `llm.video_model` | medium off | main model handles it | route to another model |
| First pass | `llm.salience_model` | main model | main model | cheaper or separate model |
| Embeddings | `llm.embedding_model` | off | not used | Gemini model id |

For the first pass, only `off` is special. An empty value means the main model scores
salience.

The knob is the routing truth. Whether the chosen model actually accepts an image,
audio clip, or video is discovered at call time. A refusal degrades the perception
ladder honestly instead of failing the turn.

## What each slot does

### Images

`llm.image_model` is used to describe:

- images shared in chat,
- screenshots uploaded by the companion,
- frames extracted from a shared video when no video model is set.

Set it to a vision-capable model. If it is empty, shared images are stored but reported
as unseen, and the chat tells you which setting to change.

### Audio

`llm.audio_model` lets the main reasoning stack listen to audio directly. If it is
empty, Aether can still transcribe voice notes through Groq if `GROQ_API_KEY` is set.
Transcription is a separate seam: it produces text, not a model that "hears".

If neither is configured, the audio file is stored and the chat says it could not be
listened to.

### Video

`llm.video_model` lets a model watch video directly. If it is empty and an image model
is configured, Aether extracts evenly spaced frames in a sandbox and sends those to the
image model. If neither path is available, the video is stored and reported as unseen.

Frame extraction consumes the media sandbox budget (`media.max_sandbox_jobs_per_day`).

### First pass

The salience model decides whether an observation is worth mentioning. It is meant for
a cheaper, faster model than the main one.

- Empty: the main model scores.
- `off`: no model scores. The free heuristic gate still runs, the judge reads as
  not-worth-mentioning, and person merging falls back to the main model.
- A named model: that model scores.

### Embeddings

Semantic memory uses Gemini embeddings and is off by default. Set a Gemini model such as
`gemini-embedding-001` (free-tier capable) to turn it on; `off` leaves it off. A bare id
is treated as a Gemini model. A value pointing at another provider logs a warning and
leaves memory search off; the seam does not guess.

Without embeddings, memory search still works through recent events and keyword
matching, but not by meaning.

## Choosing models in the panel

The Settings panel asks the server for the models each configured key can reach. The
catalog is fetched live and cached by key, so a key pasted after boot appears without a
restart.

The chat wire reads provider keys when the LLM layer is built, so pasting a provider key
(or saving a model) restarts Aether. It is back in a few seconds with the new key live.

If a stored model id is not in the catalog, the picker keeps it selected and labels it
`not in the available models — kept as saved`. It never silently changes your value.

## Checking what is live

```text
/status
```

reports the resolved model per role, which settings differ from `config.yaml`, pending
approvals, and queue depth. It is deterministic and costs no tokens.

## Prompt caching and usage

The prompt is assembled in a stable order so a provider can reuse the longest
unchanged prefix: tools, then the system prompt, then the conversation. Nothing
provider-specific is added to the message model.

- **Anthropic** gets explicit cache breakpoints at the end of each layer — the
  last tool definition, the system block, and the last cacheable content block —
  with the default five-minute TTL. A tool loop that runs inside the window pays
  the cached rate for everything before the breakpoint.
- **OpenAI and Gemini** cache automatically; their usage envelopes report the
  cached portion.
- **Groq** caches automatically on the models that support it.
- **Ollama** has no provider-side prompt cache.
- **NVIDIA NIM** documents no prompt cache, and Aether never sends it a
  `prompt_cache_key`; the adapter only carries the OpenAI-compatible usage
  numbers when the server returns them.

Every completion's usage (input, output, cached read, cache write) is summed
across the tool loop and recorded on the turn's decision trace, where the panel
shows the total and how much came from cache. Input plus output tokens also land
in the daily token ledger the background budget reads, so chat and background
spend are one honest total.

None of this changes `llm.max_tokens` or any platform message limit.

## Cost and latency notes

- The first pass can be a small model. Turning it off saves tokens but reduces how much
  Aether notices on its own.
- The main model sees recent context and tool results. Long conversations cost more
  than short ones.
- Perception calls happen once per file, then the result is stored. Asking about the
  same image later reads memory, not the provider.
- `agent.turn_timeout_seconds` is a ceiling, not a target. It does not cap
  `llm.max_tokens`.

## Not implemented

- Automatic fallback from one LLM provider to another when a provider is down.
- Per-conversation model switching.
