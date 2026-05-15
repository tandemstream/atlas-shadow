"""state_file — atomic JSON write of ``<atlas-shadow>/.daemon-state.json``.

Per amendment decision #1 + #12: the daemon writes a state file the
runner reads (read-then-fallback to ``shadow-config.yaml`` keys when
absent). The write order documented in decision #12 is:

    open(tempfile, 'w').write(...)
    os.fsync(...)
    os.rename(tempfile, state_file)

So a partial write never replaces the prior good state.

Schema (top-level fields):

    {
        "latest_commit_ingested": "<sha>",       # 40-char lowercase hex
        "latest_code_revision_id": "<uuid>",     # Atlas's id for that ingest
        "updated_at": "<iso8601>",               # when the daemon wrote this
        "daemon_pid": <int>,                     # for human debugging only
        "pinned_revisions": {                    # T6 (P2): code_revision_ids
            "<pr_number>": "<code_revision_id>", # held while grading is in
            ...                                  # flight. Prevents GC.
        }
    }

Older entries are overwritten; the file always holds one revision of the
``latest_*`` fields (the ledger has the full history). ``pinned_revisions``
is preserved across ingest writes — T6's pin-lifecycle helpers
(:func:`acquire_pin` / :func:`release_pin`) own that key, and the worker's
``write_state`` call merges over the live pin map rather than wiping it.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _atomic_write_json(state_file_path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` atomically to ``state_file_path``.

    Used by both :func:`write_state` and :func:`acquire_pin` /
    :func:`release_pin` so the write order is identical everywhere
    (amendment decision #12: tempfile -> fsync -> rename).
    """
    state_file_path = Path(state_file_path)
    state_file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=state_file_path.name + ".",
        suffix=".tmp",
        dir=str(state_file_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, sort_keys=True)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, state_file_path)
    except Exception:
        # Clean up tempfile on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_state(
    *,
    state_file_path: Path,
    latest_commit_ingested: str,
    latest_code_revision_id: str,
    daemon_pid: Optional[int] = None,
) -> None:
    """Atomically merge a fresh ``latest_*`` update into ``state_file_path``.

    Preserves the live ``pinned_revisions`` map (T6) by reading the
    current file before writing — daemon ingest cycles must not wipe pins
    held by in-flight PR-grading runs.

    Atomic order (amendment decision #12): write-to-tempfile in the same
    directory, ``fsync``, ``rename``. A crash mid-write leaves either
    the prior good file or no file — never a half-written one.
    """
    existing = read_state(state_file_path) or {}
    pins = existing.get("pinned_revisions") if isinstance(existing, dict) else None
    payload: dict[str, Any] = {
        "latest_commit_ingested": latest_commit_ingested.lower(),
        "latest_code_revision_id": latest_code_revision_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "daemon_pid": daemon_pid if daemon_pid is not None else os.getpid(),
    }
    if isinstance(pins, dict) and pins:
        payload["pinned_revisions"] = pins
    _atomic_write_json(Path(state_file_path), payload)


def read_state(state_file_path: Path) -> Optional[dict[str, Any]]:
    """Read the state file; return None when absent or unparseable.

    The runner reads this file at the start of every invocation; absence
    or corruption gracefully degrades to the config-file fallback (per
    amendment decision #1).
    """
    p = Path(state_file_path)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# T6 — Pin lifecycle (P2 packet 2026-05-14-atlas-shadow-pre-merge-grading-gate-v1)
# ---------------------------------------------------------------------------


def acquire_pin(
    state_file_path: Path,
    *,
    pr_number: int,
    code_revision_id: str,
) -> None:
    """Mark ``code_revision_id`` as pinned for PR #``pr_number``.

    The pin is a soft GC hint: while the entry is present, downstream
    cleanup tasks (out of scope for v1; planned for P3) must NOT delete
    the referenced code_revision row. The daemon's ingest writes
    (:func:`write_state`) preserve the live pin map across overwrites.

    Read-modify-write under the daemon's single-worker model (no
    cross-process locking; the daemon is the only writer).
    """
    existing = read_state(state_file_path) or {}
    pins_raw = existing.get("pinned_revisions")
    pins: dict[str, Any] = dict(pins_raw) if isinstance(pins_raw, dict) else {}
    pins[str(pr_number)] = code_revision_id
    payload = dict(existing)
    payload["pinned_revisions"] = pins
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(Path(state_file_path), payload)


def release_pin(
    state_file_path: Path,
    *,
    pr_number: int,
) -> None:
    """Remove the pin entry for PR #``pr_number``. No-op when absent.

    Called from the orchestrator's ``finally:`` block so the pin is
    always released — even if grading fails partway. P2 v1 ships
    without a daemon-restart pin-recovery sweep; if the daemon dies
    mid-grade, the pin survives until manual cleanup. (P3 adds the
    sweep.)
    """
    existing = read_state(state_file_path) or {}
    pins_raw = existing.get("pinned_revisions")
    pins: dict[str, Any] = dict(pins_raw) if isinstance(pins_raw, dict) else {}
    pins.pop(str(pr_number), None)
    payload = dict(existing)
    payload["pinned_revisions"] = pins
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(Path(state_file_path), payload)


def get_pinned_revision(
    state_file_path: Path,
    *,
    pr_number: int,
) -> Optional[str]:
    """Return the pinned code_revision_id for ``pr_number`` (or None)."""
    state = read_state(state_file_path) or {}
    pins = state.get("pinned_revisions")
    if not isinstance(pins, dict):
        return None
    val = pins.get(str(pr_number))
    return str(val) if val else None
