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
        "daemon_pid": <int>                      # for human debugging only
    }

Older entries are overwritten; the file always holds one revision (the
ledger has the full history).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def write_state(
    *,
    state_file_path: Path,
    latest_commit_ingested: str,
    latest_code_revision_id: str,
    daemon_pid: Optional[int] = None,
) -> None:
    """Atomically replace ``state_file_path`` with a fresh state object.

    Atomic order (amendment decision #12): write-to-tempfile in the same
    directory, ``fsync``, ``rename``. A crash mid-write leaves either
    the prior good file or no file — never a half-written one.
    """
    state_file_path = Path(state_file_path)
    state_file_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "latest_commit_ingested": latest_commit_ingested.lower(),
        "latest_code_revision_id": latest_code_revision_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "daemon_pid": daemon_pid if daemon_pid is not None else os.getpid(),
    }
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
