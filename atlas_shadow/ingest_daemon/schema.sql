-- atlas-shadow continuous-ingest daemon: SQLite schema (D2).
--
-- Two tables drive the daemon:
--
--   * ingest_queue — FIFO of commit SHAs awaiting ingest. A row's `status`
--     transitions queued → running → (succeeded|failed). Terminal rows
--     stay in the table for dedup (re-enqueue of an already-succeeded SHA
--     is a no-op) and audit. The worker dequeues the oldest `queued` row
--     each tick.
--
--   * ingest_ledger — append-only attempt history. One row per ingest
--     attempt (NOT per SHA — a SHA that fails twice and succeeds on the
--     third try has three ledger rows). The latest succeeded row is the
--     authoritative "we have data for X" record; everything else is
--     forensic.
--
-- Schema notes:
--   - All timestamps are ISO-8601 strings (`YYYY-MM-DDTHH:MM:SS+00:00`)
--     so they're human-readable in sqlite3 CLI. The worker generates
--     them with `datetime.now(timezone.utc).isoformat()`.
--   - `commit_sha` is the GitHub commit SHA as a 40-char lowercase hex
--     string. The daemon never trims to short SHAs.
--   - `attempt_count` on the queue row tracks retries; it's compared
--     against `max_attempts_per_commit` from shadow-config.yaml's
--     `ingest_daemon:` section.
--   - The schema is intentionally NOT versioned with a `schema_version`
--     table — the daemon DB is local-only, the daemon owns it 1:1, and
--     the runbook documents `rm -f ~/.atlas-shadow/ingest.db && make
--     ingest-bootstrap` for major schema changes.

CREATE TABLE IF NOT EXISTS ingest_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha      TEXT    NOT NULL UNIQUE,
    enqueued_at     TEXT    NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    source          TEXT    NOT NULL DEFAULT 'webhook'
                            CHECK (source IN ('webhook', 'replay', 'startup-recover'))
);

CREATE INDEX IF NOT EXISTS idx_ingest_queue_status_id
    ON ingest_queue (status, id);

CREATE INDEX IF NOT EXISTS idx_ingest_queue_finished_at
    ON ingest_queue (finished_at);

CREATE TABLE IF NOT EXISTS ingest_ledger (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha              TEXT    NOT NULL,
    started_at              TEXT    NOT NULL,
    finished_at             TEXT,
    status                  TEXT    NOT NULL
                                    CHECK (status IN ('succeeded', 'failed')),
    code_revision_id        TEXT,
    scip_path               TEXT,
    source_root             TEXT,
    scip_size_bytes         INTEGER,
    chunker_stats_total     INTEGER,
    counts_json             TEXT,
    latency_ms              INTEGER,
    error_message           TEXT,
    attempt_number          INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_ingest_ledger_commit_sha
    ON ingest_ledger (commit_sha);

CREATE INDEX IF NOT EXISTS idx_ingest_ledger_finished_at
    ON ingest_ledger (finished_at);
