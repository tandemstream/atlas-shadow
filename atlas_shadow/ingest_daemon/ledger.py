"""ledger — append-only attempt history (``ingest_ledger`` table).

One row per ingest attempt. The worker writes a ``running`` row at attempt
start (status column not used for that; the queue tracks running state).
Actually, simpler design: ledger rows are written ONLY on terminal
outcomes (status in {'succeeded', 'failed'}). For in-flight attempts the
``ingest_queue`` row is the source of truth.

Public surface:
- :func:`insert_terminal_attempt` — write one row at end-of-attempt.
- :func:`latest_succeeded` — most-recent succeeded ledger row (drives
  the ``/status`` endpoint and the state-file content).
- :func:`get_by_commit_sha` — list all attempts for a SHA, newest first
  (used by ``GET /status?commit=<sha>`` if added in a follow-on).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import queue as queue_mod


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_terminal_attempt(
    db_path: Path,
    *,
    commit_sha: str,
    status: str,
    started_at: str,
    attempt_number: int,
    code_revision_id: Optional[str] = None,
    scip_path: Optional[str] = None,
    source_root: Optional[str] = None,
    scip_size_bytes: Optional[int] = None,
    chunker_stats_total: Optional[int] = None,
    counts: Optional[dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error_message: Optional[str] = None,
) -> int:
    """Insert a terminal-attempt row; return its id."""
    if status not in ("succeeded", "failed"):
        raise ValueError(f"status must be 'succeeded' or 'failed', got {status!r}")
    finished_at = _now_iso()
    counts_json = json.dumps(counts) if counts is not None else None
    with queue_mod.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO ingest_ledger (
                commit_sha, started_at, finished_at, status,
                code_revision_id, scip_path, source_root, scip_size_bytes,
                chunker_stats_total, counts_json, latency_ms, error_message,
                attempt_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                commit_sha.lower(),
                started_at,
                finished_at,
                status,
                code_revision_id,
                scip_path,
                source_root,
                scip_size_bytes,
                chunker_stats_total,
                counts_json,
                latency_ms,
                error_message,
                attempt_number,
            ),
        )
        return int(cur.lastrowid)


def latest_succeeded(db_path: Path) -> Optional[dict[str, Any]]:
    """Most-recent succeeded row, or None."""
    with queue_mod.connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT * FROM ingest_ledger
             WHERE status='succeeded'
             ORDER BY id DESC
             LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row)


def latest_attempt(db_path: Path) -> Optional[dict[str, Any]]:
    """Most-recent row regardless of status (used for ``last_error`` on
    ``/status``)."""
    with queue_mod.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM ingest_ledger ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row)


def get_by_commit_sha(db_path: Path, commit_sha: str) -> list[dict[str, Any]]:
    """All attempts for a SHA, newest first."""
    with queue_mod.connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT * FROM ingest_ledger
             WHERE commit_sha=?
             ORDER BY id DESC
            """,
            (commit_sha.lower(),),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def _row_to_dict(row) -> dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    if d.get("counts_json"):
        try:
            d["counts"] = json.loads(d["counts_json"])
        except (TypeError, json.JSONDecodeError):
            d["counts"] = None
    else:
        d["counts"] = None
    return d
