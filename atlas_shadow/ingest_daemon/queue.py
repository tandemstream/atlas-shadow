"""queue — SQLite-backed durable FIFO with terminal-status dedup.

Tables defined in ``schema.sql``. Public surface:

- :func:`init_db` — apply schema to a fresh DB file; idempotent.
- :func:`enqueue` — insert a row if no row exists for ``commit_sha``; if
  one exists with terminal status ``succeeded``, no-op (dedup); if
  ``failed`` and below retry cap, reset to queued (replay); otherwise
  no-op and return the existing row id.
- :func:`recover_running_on_startup` — reset any row in ``running`` to
  ``queued`` so the worker re-attempts after a crash.
- :func:`claim_next` — pop the oldest queued row, mark it ``running``,
  return ``(id, commit_sha, attempt_count)`` or ``None``.
- :func:`mark_terminal` — close out a queue row with succeeded/failed.
- :func:`queue_depth` — count of rows with ``status='queued'``.

The connection is opened on demand and closed by the caller (we never
hold a long-lived cursor — the worker re-opens between drains, the
receiver re-opens per request). SQLite's default WAL + 5s busy timeout
is sufficient for the daemon's expected load (≤1 webhook/min, single
worker).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with sensible defaults. Caller closes."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level="DEFERRED", timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    """Apply ``schema.sql`` to the DB at ``db_path``. Idempotent (the
    schema uses ``CREATE TABLE IF NOT EXISTS``).

    Also runs forward-only migrations for live DBs that pre-date a
    schema change — currently the one migration is "drop the source
    CHECK constraint" added when ``source='reconciler'`` joined the
    enum.
    """
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(db_path) as conn:
        conn.executescript(schema)
        _migrate_drop_source_check(conn)


def _migrate_drop_source_check(conn: sqlite3.Connection) -> None:
    """If the live ``ingest_queue`` table still carries the old
    ``CHECK (source IN ('webhook', 'replay', 'startup-recover'))``
    clause, rebuild it without that constraint.

    SQLite can't alter a CHECK constraint in place; the documented
    pattern is "create new, copy rows, drop old, rename." Wrapped in
    a transaction so a partial migration leaves the original table
    untouched.
    """
    cur = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ingest_queue'"
    )
    row = cur.fetchone()
    if row is None:
        return
    create_sql = (row["sql"] if isinstance(row, sqlite3.Row) else row[0]) or ""
    if "CHECK (source IN" not in create_sql and "CHECK(source IN" not in create_sql:
        return  # already migrated (or freshly created without the CHECK)
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE ingest_queue__new (
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
        );
        INSERT INTO ingest_queue__new
            (id, commit_sha, enqueued_at, started_at, finished_at,
             status, attempt_count, last_error, source)
        SELECT id, commit_sha, enqueued_at, started_at, finished_at,
               status, attempt_count, last_error, source
          FROM ingest_queue;
        DROP TABLE ingest_queue;
        ALTER TABLE ingest_queue__new RENAME TO ingest_queue;
        CREATE INDEX IF NOT EXISTS idx_ingest_queue_status_id
            ON ingest_queue (status, id);
        CREATE INDEX IF NOT EXISTS idx_ingest_queue_finished_at
            ON ingest_queue (finished_at);
        COMMIT;
        """
    )


def enqueue(
    db_path: Path,
    commit_sha: str,
    *,
    source: str = "webhook",
    max_attempts: int = 3,
) -> dict:
    """Enqueue ``commit_sha`` if not already present in a terminal-success
    or running state.

    Returns a dict::

        {
            "queued": bool,   # True if the call resulted in a queued row
            "reason": str,    # 'new' | 'dedup-succeeded' | 'requeued-after-failure'
                              # | 'already-queued' | 'already-running' | 'max-attempts-exceeded'
            "queue_id": int,  # the row id in ingest_queue
        }
    """
    assert source in ("webhook", "replay", "startup-recover", "reconciler")
    sha = (commit_sha or "").strip().lower()
    if not sha:
        raise ValueError("commit_sha must be a non-empty string")
    now = _now_iso()
    with connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id, status, attempt_count FROM ingest_queue WHERE commit_sha = ?",
            (sha,),
        )
        row = cur.fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO ingest_queue (commit_sha, enqueued_at, status, source)
                VALUES (?, ?, 'queued', ?)
                """,
                (sha, now, source),
            )
            return {"queued": True, "reason": "new", "queue_id": cur.lastrowid}
        existing_id, status, attempt_count = row["id"], row["status"], row["attempt_count"]
        if status == "succeeded":
            return {"queued": False, "reason": "dedup-succeeded", "queue_id": existing_id}
        if status == "queued":
            return {"queued": False, "reason": "already-queued", "queue_id": existing_id}
        if status == "running":
            return {"queued": False, "reason": "already-running", "queue_id": existing_id}
        # status == 'failed'
        if attempt_count >= max_attempts:
            return {
                "queued": False,
                "reason": "max-attempts-exceeded",
                "queue_id": existing_id,
            }
        conn.execute(
            """
            UPDATE ingest_queue
               SET status='queued',
                   enqueued_at=?,
                   started_at=NULL,
                   finished_at=NULL,
                   last_error=NULL,
                   source=?
             WHERE id=?
            """,
            (now, source, existing_id),
        )
        return {"queued": True, "reason": "requeued-after-failure", "queue_id": existing_id}


def recover_running_on_startup(db_path: Path) -> int:
    """Reset any ``running`` rows to ``queued`` on daemon boot.

    Returns the count reset. Logged by the entrypoint.
    """
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE ingest_queue
               SET status='queued',
                   started_at=NULL,
                   source='startup-recover'
             WHERE status='running'
            """
        )
        return cur.rowcount


def claim_next(db_path: Path) -> Optional[dict]:
    """Pop the oldest queued row; mark it running; return its fields.

    Returns ``None`` if the queue is empty. Increments ``attempt_count``
    so the ledger row's ``attempt_number`` matches.
    """
    now = _now_iso()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, commit_sha, attempt_count
              FROM ingest_queue
             WHERE status='queued'
             ORDER BY id ASC
             LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        new_attempt = int(row["attempt_count"]) + 1
        conn.execute(
            """
            UPDATE ingest_queue
               SET status='running',
                   started_at=?,
                   attempt_count=?
             WHERE id=?
            """,
            (now, new_attempt, row["id"]),
        )
        return {
            "queue_id": int(row["id"]),
            "commit_sha": str(row["commit_sha"]),
            "attempt_number": new_attempt,
            "started_at": now,
        }


def mark_terminal(
    db_path: Path,
    queue_id: int,
    *,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Mark ``queue_id`` as ``succeeded`` or ``failed``."""
    if status not in ("succeeded", "failed"):
        raise ValueError(f"status must be 'succeeded' or 'failed', got {status!r}")
    now = _now_iso()
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE ingest_queue
               SET status=?, finished_at=?, last_error=?
             WHERE id=?
            """,
            (status, now, error, queue_id),
        )


def queue_depth(db_path: Path) -> int:
    """Number of rows with ``status='queued'``."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM ingest_queue WHERE status='queued'"
        )
        row = cur.fetchone()
        return int(row["n"] or 0)
