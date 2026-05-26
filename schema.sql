-- Schema for the Source of Truth (SOT) app.
-- GitHub holds the canonical content (markdown files in a repo); Postgres
-- only holds in-flight LLM extraction state and dedup-judge cache.

-- In-flight LLM extractions awaiting human review.
CREATE TABLE IF NOT EXISTS sot_pending_updates (
    id                SERIAL PRIMARY KEY,
    source_type       TEXT NOT NULL,             -- 'url' | 'text' | 'file' | 'urls'
    source_ref        TEXT,                       -- short label / URL / filename
    source_content    TEXT,                       -- raw content or JSON of URLs
    status            TEXT NOT NULL DEFAULT 'pending', -- pending | extracting | done | error | applied
    extracted_facts   JSONB,                      -- summary counts per bucket
    proposed_changes  JSONB,                      -- per-card LLM proposals + dedup verdicts
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sot_pending_updates_status_idx
    ON sot_pending_updates (status, created_at DESC);

-- Cached dedup-judge verdicts (overlap-prefilter + LLM judge).
-- The application calls ensure_schema() at import, so this table will be
-- created automatically on first run as well.
CREATE TABLE IF NOT EXISTS sot_dup_judgments (
    pair_hash      TEXT PRIMARY KEY,
    bucket         TEXT NOT NULL,
    verdict        TEXT NOT NULL,                -- 'same' | 'different' | 'partial'
    confidence     REAL NOT NULL,
    rationale      TEXT,
    left_summary   TEXT,
    right_summary  TEXT,
    created_at     TIMESTAMP DEFAULT NOW()
);
