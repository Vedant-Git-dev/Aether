---
title: "The evaluation: what the numbers say"
summary: "The golden set, the ablation arms, and the decisions they justify for embeddings and enrichment."
read_when:
  - You are changing retrieval, embeddings, or write-time enrichment
  - You want the measured numbers behind a default
---

# The evaluation: what the numbers say

Plan.md section 11 asks for an evaluation that decides what ships: whether the
optional embedding path stays, whether write-time enrichment earns its
per-event model cost, and whether the routed query pipeline holds up. This
document records the corpus, the measurements and the decisions. The
machine-checked half lives in `tests/test_eval_ablation.py` and
`tests/test_eval_enrichment.py`; a change that breaks a decision fails the
build.

## The corpus

`tests/eval_golden.py` is the golden set: 19 cases, each the smallest world
that poses one of the questions plan.md names. Every case carries a `subset`
tag, so the metrics report per question shape instead of one blended number:

| subset | cases | what it asks |
| --- | --- | --- |
| attention | 3 | "is anything urgent?", "kuch urgent hai?", "anything serious?" |
| reference | 5 | "Assignment?", "send then", "send the assignment then", "send it", and several photos |
| recall | 4 | a second-address sender, the owner's own second address, a typo, an entity timeline |
| deadline | 2 | "what's due today?", and a relative phrase resolved to an absolute instant |
| doc | 1 | "what does slide 3 say?" |
| paraphrase | 1 | "where is the bill?" against a chunk that says "invoice" |
| commitment | 2 | "what did I promise Rahul?", with and without a commitments store |
| abstain | 1 | a question nothing in memory answers |

The set runs offline against the real router, lexical index, fusion, gate and
packer, under a fixed clock (`2026-11-03 06:16 UTC`), with a fake embedder
where a genuine paraphrase needs the vector signal.
`tests/eval_ablation.py` runs the same cases as three arms:

- **lexical** — the vector path off (`vectors=False`);
- **vector** — the vector path on for every case (`vectors=True`);
- **seeded** — vectors on exactly where a case seeds a hit; this is the
  canonical gate the golden set has always meant.

## Embedding: lexical + structured versus + vector

Measured (`memory.eval_repeats` runs per case, one representative run):

| arm | recall@5 | MRR | route | abstain (rec/prec) | claim support | p50 | p95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| lexical | 0.909 | 0.909 | 1.000 | 1.000 / 0.909 | 0.909 | 0.086 ms | 0.333 ms |
| vector | 1.000 | 1.000 | 1.000 | 1.000 / 1.000 | 1.000 | 0.077 ms | 0.199 ms |
| seeded | 1.000 | 1.000 | 1.000 | 1.000 / 1.000 | 1.000 | 0.069 ms | 0.181 ms |

The paraphrase subset is the whole difference: lexical recall@5 there is
0.000 (no shared term between "bill" and "invoice"), the vector arm is 1.000,
so the measured paraphrase gain is **1.000** against a configured margin of
`memory.embedding_min_paraphrase_gain = 0.10`. Every other subset is
identical across the arms — vectors neither help nor hurt them. Peak traced
memory while building every case's index is about **63 KB**, and p95 is
**under 0.4 ms** against a `memory.retrieve_latency_p95_ms` budget of 300 ms.

**Decision: keep the embedding path, behind `llm.embedding_model`.** It
clears the paraphrase margin and the latency budget, and the encryption cost
is already paid. Removal is a follow-up only on a clear negative, and the
chunk store would stay either way because `read_document` depends on it.

**What this does not measure.** The offline paraphrase arm uses a canned hit,
not a live embedding. The CI gate proves the *rule* holds — the vector list
reaches the pack and swings the paraphrase case — and that nothing else
regresses. The real semantic quality of `gemini-embedding-001` on your own
mail is not measured here; run the arms against a real embedder to judge that.

## Enrichment: off versus on

`tests/test_eval_enrichment.py` ingests one small world twice through the real
`EventStore` — once with `memory.enrichment` off, once on — and answers the
attention, deadline and commitment questions from each arm's views:

| arm | attention | deadline | commitment | classifier calls |
| --- | --- | --- | --- | --- |
| off (floor only) | yes | no | no | 0 |
| on | yes | yes | yes | 1 per event |

The gain is **0.667** over the floor on this world, at exactly one classifier
call per event, and the floor arm never loses an item its own words mark
urgent. The rule is `memory.enrichment_min_answer_gain = 0.15`.

**Decision: the default is `true`.** The measured gain (0.667) clears the
configured margin (`memory.enrichment_min_answer_gain = 0.15`) at exactly one
classifier call per event, and the floor arm never loses an item its own words
mark urgent, so the owner asked for it to ship on. **Honest caveat:** the
offline arm uses a deterministic stand-in classifier, not the model, so this
number measures that the wiring turns text into structured fields and reaches
the views, and it counts the cost — it is not a measurement of the real
classifier's quality. A live run against your own mail is what would confirm
that; the rule and the assertion that guard it are in place, and a regression
fails the build.

## The routed query pipeline

Phase 2 shipped the pipeline behind `memory.query_pipeline`, off by default,
"until the golden-set evaluation blesses it". The seeded arm above is that
evaluation: recall@5 1.000, MRR 1.000, route accuracy 1.000, abstain
precision and recall 1.000, claim support 1.000. **Decision: `true` by
default** — the routed pipeline is now the retrieval path, and setting the
flag `false` falls back to the older vector-only recall.

## What is not guaranteed

- Offline latency is in-process and not a production number; the gate is
  deliberately loose and only catches a real regression.
- The paraphrase arm is canned; see above.
- `tests/test_eval_enrichment.py` is an integration test: it needs
  `AETHER_TEST_DATABASE_URL` and runs in the integration job, not the unit job.
- The metrics cover the golden set, not a broad sample of real mail; a new
  question shape is a new case.
