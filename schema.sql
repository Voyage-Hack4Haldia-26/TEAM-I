-- PR Summarizer Bot - database schema (Supabase PostgreSQL)
-- Run once: paste into Supabase -> SQL Editor -> Run, or execute `python db.py init`.
-- Safe to re-run (everything uses IF NOT EXISTS).
--
-- Design: ONE row per pull request. A new webhook for the same PR (synchronize,
-- reopened) updates that row, so the dashboard always shows the latest analysis.

CREATE TABLE IF NOT EXISTS pr_summaries (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_full_name  TEXT        NOT NULL,                 -- 'owner/name'
    pr_number             INTEGER     NOT NULL CHECK (pr_number > 0),
    pr_title              TEXT        NOT NULL DEFAULT '',
    pr_url                TEXT        NOT NULL DEFAULT '',
    pr_author             TEXT        NOT NULL DEFAULT '',
    base_branch           TEXT        NOT NULL DEFAULT '',
    head_branch           TEXT        NOT NULL DEFAULT '',
    head_sha              TEXT,                                  -- commit analysed (detects stale jobs)

    summary               TEXT,
    key_changes           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    risk_level            TEXT        CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH')),
    risk_reasons          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    testing_suggestions   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    suggested_labels      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    files_changed         JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- [{filename,status,additions,deletions,changes,...}]
    files_analyzed        JSONB       NOT NULL DEFAULT '[]'::jsonb,

    analysis_status       TEXT        NOT NULL DEFAULT 'pending'
                          CHECK (analysis_status IN ('pending', 'processing', 'success', 'skipped', 'failed')),
    analysis_scope        TEXT        CHECK (analysis_scope IN ('full', 'partial')),
    analysis_note         TEXT,
    model_name            TEXT,
    bot_actions           JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- PR comment / labels outcome
    last_delivery_id      TEXT,
    analyzed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pr_summaries_repo_pr_key UNIQUE (repository_full_name, pr_number),
    -- a 'success' row must really contain AI output; fake successes are impossible
    CONSTRAINT pr_summaries_success_has_ai CHECK (
        analysis_status <> 'success' OR (summary IS NOT NULL AND risk_level IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_pr_summaries_updated_at ON pr_summaries (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pr_summaries_risk_level ON pr_summaries (risk_level);
CREATE INDEX IF NOT EXISTS idx_pr_summaries_status     ON pr_summaries (analysis_status);

-- One row per GitHub delivery GUID (X-GitHub-Delivery). The primary key makes
-- duplicate deliveries impossible to process twice.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id           TEXT        PRIMARY KEY,
    event                 TEXT        NOT NULL,
    repository_full_name  TEXT        NOT NULL,
    received_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Supabase exposes the public schema through its REST API. Enabling RLS with no
-- policies blocks that route completely; this app connects directly as the
-- database owner (which bypasses RLS), so it keeps working.
ALTER TABLE pr_summaries       ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY;
