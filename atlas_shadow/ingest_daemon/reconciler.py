"""reconciler — periodic poll of ``origin/main`` as a webhook safety net.

The daemon's normal path for picking up new commits is webhook-driven:
``gh webhook forward`` (or a Cloudflare tunnel) delivers a ``push`` event
to ``POST /webhook`` and the receiver enqueues the head SHA. If the
forwarder dies — usually when its parent shell exits without
``ingest-up-detached``-style nohup wrapping — the daemon never hears
about new commits and the corpus silently freezes.

The 2026-05-15 bring-up surfaced this exact failure: the operator's
shell exited after starting the forwarder, the forwarder process went
with it, and the daemon fell 30 commits behind ``origin/main`` over the
course of an afternoon. ``warn_if_backlog`` (worker.py) only fires once
per restart, so a long-running daemon never re-warns; this reconciler
is the always-on counterpart.

What it does
============

Every ``reconciler_interval_seconds`` (default 300s) the reconciler
runs one tick:

  1. Read ``.daemon-state.json`` for ``latest_commit_ingested``.
  2. ``git ls-remote <core_repo_url> refs/heads/main`` to fetch the
     remote head SHA without needing a local clone.
  3. If the remote SHA differs from the state's
     ``latest_commit_ingested`` (or the state file is empty / missing),
     enqueue the remote SHA via ``queue_mod.enqueue(..., source="reconciler")``.
     The queue's existing dedup (``status='succeeded'`` -> no-op,
     ``status='queued'`` -> no-op) keeps a webhook-driven enqueue and a
     reconciler-driven enqueue from racing into duplicate work.

What it does NOT do
===================

* It does NOT walk the gap. If we're 30 commits behind, the reconciler
  enqueues only the *current* remote HEAD — the worker will ingest that
  one SHA and Atlas's file_memoization carries the rest forward. If the
  operator wants every intermediate SHA in the ledger they still run
  ``make ingest-replay FROM=<sha>``.
* It does NOT replay missed ``push`` webhook deliveries. That would
  require knowing the hook id and matching deliveries to the state
  file's commit-cursor; for the "don't fall behind" goal the simpler
  HEAD-poll is sufficient.
* It does NOT race with the worker. Enqueueing is idempotent on
  ``commit_sha`` (see :func:`queue.enqueue`); a reconciler tick that
  fires while the worker is processing the same SHA short-circuits at
  ``status='running'`` -> ``already-running``.

Why ``git ls-remote`` and not ``gh api .../commits/main``
=========================================================

* No GitHub auth required for public repos (the operator's
  ``core_repo_url`` is the same one the cache uses for cloning).
* No dependency on the local clone being up to date — the worker's own
  ``ensure_core_clone`` will ``git fetch`` once the reconciler enqueues
  the new SHA.
* One subprocess per tick, no JSON parsing.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

from . import queue as queue_mod
from . import state_file as state_mod
from .config import DaemonConfig


def _parse_ls_remote_sha(stdout: str) -> Optional[str]:
    """``git ls-remote <url> refs/heads/main`` prints one line:

        ``<40-char-sha>\\trefs/heads/main``

    Return the SHA, or None when the output is empty / malformed.
    """
    for line in (stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].strip() == "refs/heads/main":
            sha = parts[0].strip().lower()
            if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
                return sha
    return None


def fetch_remote_head_sha(
    cfg: DaemonConfig,
    *,
    _subprocess_run: Callable = subprocess.run,
) -> Optional[str]:
    """Return the current ``refs/heads/main`` SHA on ``cfg.core_repo_url``.

    Returns ``None`` when ``git ls-remote`` fails (network down, repo
    unreachable, etc.) so the caller can log+skip rather than crash the
    reconciler loop. Pure read-only — never writes to the local clone.
    """
    try:
        proc = _subprocess_run(
            ["git", "ls-remote", cfg.core_repo_url, "refs/heads/main"],
            capture_output=True,
            text=True,
            timeout=cfg.reconciler_ls_remote_timeout_seconds,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_ls_remote_sha(proc.stdout)


def reconcile_once(
    cfg: DaemonConfig,
    *,
    _fetch_remote: Callable = fetch_remote_head_sha,
    _read_state: Callable = state_mod.read_state,
    _enqueue: Callable = queue_mod.enqueue,
) -> dict[str, Any]:
    """Run one reconciliation tick.

    Returns a dict so the entrypoint can log a meaningful summary::

        {
            "outcome": "in-sync" | "enqueued" | "no-remote" | "no-op"
                       | "skipped-already-queued" | "skipped-already-running"
                       | "skipped-dedup-succeeded",
            "remote_sha": "<sha>" | None,
            "local_sha": "<sha>" | None,
            "queue_id": int | None,
        }

    Never raises — failure modes collapse to outcome strings so the
    caller's loop stays alive.
    """
    state = _read_state(cfg.state_file) or {}
    local_sha: Optional[str] = state.get("latest_commit_ingested") if isinstance(state, dict) else None
    if local_sha:
        local_sha = local_sha.strip().lower() or None

    remote_sha = _fetch_remote(cfg)
    if remote_sha is None:
        return {
            "outcome": "no-remote",
            "remote_sha": None,
            "local_sha": local_sha,
            "queue_id": None,
        }

    if local_sha == remote_sha:
        return {
            "outcome": "in-sync",
            "remote_sha": remote_sha,
            "local_sha": local_sha,
            "queue_id": None,
        }

    try:
        result = _enqueue(
            cfg.db_path,
            remote_sha,
            source="reconciler",
            max_attempts=cfg.max_attempts_per_commit,
        )
    except Exception as exc:  # noqa: BLE001 — never let enqueue crash the loop
        return {
            "outcome": "enqueue-error",
            "remote_sha": remote_sha,
            "local_sha": local_sha,
            "queue_id": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    queue_id = result.get("queue_id") if isinstance(result, dict) else None
    reason = result.get("reason") if isinstance(result, dict) else None
    queued = bool(result.get("queued")) if isinstance(result, dict) else False

    if queued:
        outcome = "enqueued"
    elif reason == "dedup-succeeded":
        # Worker already ingested this SHA via the webhook path between
        # our `_read_state` call and `git ls-remote`. State file write
        # may simply lag; treat as benign in-sync.
        outcome = "skipped-dedup-succeeded"
    elif reason == "already-queued":
        outcome = "skipped-already-queued"
    elif reason == "already-running":
        outcome = "skipped-already-running"
    elif reason == "max-attempts-exceeded":
        outcome = "skipped-max-attempts"
    else:
        outcome = "no-op"

    return {
        "outcome": outcome,
        "remote_sha": remote_sha,
        "local_sha": local_sha,
        "queue_id": queue_id,
    }


def run_loop(
    cfg: DaemonConfig,
    stop_event: threading.Event,
    *,
    _reconcile_once: Callable = reconcile_once,
    _sleep: Callable = None,
) -> None:
    """Background-thread loop: call :func:`reconcile_once` on every tick.

    Sleeps ``cfg.reconciler_interval_seconds`` between ticks (interrupted
    by ``stop_event.set()``). One stderr line per non-trivial outcome
    (anything other than ``in-sync``) so an operator tailing
    ``.ingest-daemon.log`` sees the safety net firing.
    """
    interval = max(1, int(cfg.reconciler_interval_seconds))
    if _sleep is None:
        _sleep = stop_event.wait
    while not stop_event.is_set():
        try:
            outcome = _reconcile_once(cfg)
        except Exception as exc:  # noqa: BLE001 — outer guardrail
            print(
                f"[ingest-daemon] WARN: reconciler tick raised "
                f"{type(exc).__name__}: {exc}; continuing",
                file=sys.stderr,
            )
        else:
            if outcome.get("outcome") not in ("in-sync",):
                remote = outcome.get("remote_sha")
                local = outcome.get("local_sha")
                print(
                    f"[ingest-daemon] reconciler: "
                    f"outcome={outcome.get('outcome')} "
                    f"remote={remote[:12] if remote else None} "
                    f"local={local[:12] if local else None} "
                    f"queue_id={outcome.get('queue_id')}",
                    file=sys.stderr,
                )
        if _sleep(timeout=interval):
            # stop_event was set during the wait
            return
