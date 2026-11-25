-- The serving task must demonstrate that it has the keys needed to look up
-- any retained historical tags. These values are HMACs over public challenges.
CREATE TABLE IF NOT EXISTS historical_lookup_requirements (
    domain TEXT PRIMARY KEY,
    proofs BYTEA[] NOT NULL CHECK (cardinality(proofs) > 0)
);
