-- Decision traces: the encrypted "why" behind every action Aether takes.
--
-- The audit log answers "what was decided" — append-only, hash-chained,
-- parameters kept as digests so the chain proves the decision without
-- storing the call. A trace answers "why": what triggered the act (a
-- message, a feed event, a taught routine, a schedule, an approval),
-- what the model saw and said, how the gate ruled each proposed call,
-- and what came back. One row per act, four kinds:
--   turn      — one LLM turn
--   routine   — one routine fire
--   scheduled — one scheduled action firing
--   carry_out — an approved call being carried out

CREATE TABLE IF NOT EXISTS decision_traces (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT        NOT NULL,
    label       TEXT        NOT NULL DEFAULT '',
    trace_enc   BYTEA       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS decision_traces_recent_idx
    ON decision_traces (id DESC);
