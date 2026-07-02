-- ══════════════════════════════════════════════════════════════════════════════
--  AI Code Review Agent — Postgres Schema
--  Source: database.md §2 (PostgreSQL) and §3 (TimescaleDB)
--
--  This script is idempotent (uses IF NOT EXISTS / CREATE OR REPLACE).
--  It is run automatically on first container start via docker-entrypoint-initdb.d.
--  Alembic is the ongoing migration tool; this file is the canonical reference.
-- ══════════════════════════════════════════════════════════════════════════════

-- Enable TimescaleDB extension (must run before hypertable creation)
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── Repositories being monitored ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS repos (
    id              SERIAL PRIMARY KEY,
    github_repo_id  BIGINT UNIQUE NOT NULL,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    installed_at    TIMESTAMPTZ DEFAULT now(),
    is_active       BOOLEAN DEFAULT true
);

-- ── Pull requests processed ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pull_requests (
    id              SERIAL PRIMARY KEY,
    repo_id         INTEGER REFERENCES repos(id),
    pr_number       INTEGER NOT NULL,
    commit_sha      TEXT NOT NULL,
    author          TEXT,
    title           TEXT,
    status          TEXT CHECK (status IN ('queued','processing','completed','failed')) DEFAULT 'queued',
    created_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    UNIQUE(repo_id, pr_number, commit_sha)
);

-- ── Individual review runs ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reviews (
    id              SERIAL PRIMARY KEY,
    pull_request_id INTEGER REFERENCES pull_requests(id),
    total_findings  INTEGER DEFAULT 0,
    blocker_count   INTEGER DEFAULT 0,
    warning_count   INTEGER DEFAULT 0,
    nit_count       INTEGER DEFAULT 0,
    model_cost_usd  NUMERIC(10,4) DEFAULT 0,
    duration_ms     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ── Individual findings ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS findings (
    id              SERIAL PRIMARY KEY,
    review_id       INTEGER REFERENCES reviews(id),
    agent           TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    line_number     INTEGER,
    severity        TEXT CHECK (severity IN ('blocker','warning','nit')),
    category        TEXT,
    message         TEXT NOT NULL,
    confidence      NUMERIC(3,2),
    escalated_to_claude BOOLEAN DEFAULT false,
    suggested_fix   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ── GitHub App installations ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS installations (
    id                     SERIAL PRIMARY KEY,
    github_installation_id BIGINT UNIQUE NOT NULL,
    account_login          TEXT,
    installed_at           TIMESTAMPTZ DEFAULT now()
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_pr_repo ON pull_requests(repo_id);
CREATE INDEX IF NOT EXISTS idx_findings_review ON findings(review_id);
CREATE INDEX IF NOT EXISTS idx_findings_file ON findings(file_path);

-- ── TimescaleDB: Technical Debt Hypertable ────────────────────────────────────
CREATE TABLE IF NOT EXISTS debt_scores (
    time        TIMESTAMPTZ NOT NULL DEFAULT now(),
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    file_path   TEXT NOT NULL,
    score       DOUBLE PRECISION NOT NULL,
    delta       DOUBLE PRECISION NOT NULL,
    pr_number   INTEGER
);

-- create_hypertable is idempotent when if_not_exists => true
SELECT create_hypertable(
    'debt_scores',
    'time',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_debt_repo_file ON debt_scores(repo_id, file_path, time DESC);

-- ── Verification queries (safe to run, just return data) ──────────────────────
-- \dt                                                      → list tables
-- SELECT * FROM timescaledb_information.hypertables;       → confirm debt_scores is a hypertable
