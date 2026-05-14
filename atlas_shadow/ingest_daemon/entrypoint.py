"""entrypoint — daemon CLI (``python -m atlas_shadow.ingest_daemon``).

Subcommands:
  * ``serve`` — start the FastAPI receiver + background worker. The
    receiver runs in the main thread (uvicorn), the worker runs in a
    background daemon thread that drains the queue.
  * ``replay`` — enqueue commit(s) from a SHA range or single SHA
    (mirrors ``make ingest-replay``).
  * ``bootstrap`` — apply the schema to a fresh DB.
  * ``status`` — pretty-print the ``/status`` payload from disk (no
    HTTP — useful when the daemon is down).

This module is the only one that imports uvicorn; everything else is
import-safe even without FastAPI installed.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from . import queue as queue_mod
from . import receiver as receiver_mod
from . import worker as worker_mod
from .config import DaemonConfig, load_config


def _worker_loop(cfg: DaemonConfig, stop_event: threading.Event) -> None:
    """Background-thread worker loop. Drains the queue until ``stop_event``."""
    # Recover any crashed-row state.
    n_recovered = queue_mod.recover_running_on_startup(cfg.db_path)
    if n_recovered:
        print(
            f"[ingest-daemon] recovered {n_recovered} running rows on startup",
            file=sys.stderr,
        )
    # Best-effort backlog warning (amendment decision #11).
    try:
        worker_mod.warn_if_backlog(cfg)
    except Exception as exc:
        print(
            f"[ingest-daemon] WARN: backlog check failed (non-fatal): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    while not stop_event.is_set():
        try:
            outcome = worker_mod.drain_once(cfg)
        except Exception as exc:
            # Outer guardrail — drain_once should already be exception-safe,
            # but if something escapes (e.g., SQLite locked), log+sleep.
            print(
                f"[ingest-daemon] WARN: drain_once raised "
                f"{type(exc).__name__}: {exc}; sleeping {cfg.worker_idle_sleep_seconds}s",
                file=sys.stderr,
            )
            outcome = None
        if outcome is None:
            stop_event.wait(timeout=cfg.worker_idle_sleep_seconds)
        else:
            print(
                f"[ingest-daemon] drained ledger_id={outcome['ledger_id']} "
                f"status={outcome['status']} code_revision_id={outcome['code_revision_id']} "
                f"latency_ms={outcome['latency_ms']}",
                file=sys.stderr,
            )


def cmd_serve(cfg: DaemonConfig) -> int:
    """Run the daemon in the foreground."""
    queue_mod.init_db(cfg.db_path)
    stop_event = threading.Event()
    worker_thread = threading.Thread(
        target=_worker_loop,
        args=(cfg, stop_event),
        name="ingest-daemon-worker",
        daemon=True,
    )
    worker_thread.start()
    try:
        import uvicorn  # type: ignore
    except ImportError as exc:
        print(
            f"[ingest-daemon] FATAL: uvicorn not installed: {exc}. "
            f"Run `make setup` from the atlas-shadow root.",
            file=sys.stderr,
        )
        stop_event.set()
        return 2
    app = receiver_mod.create_app(cfg)
    print(
        f"[ingest-daemon] serving on http://{cfg.host}:{cfg.port} "
        f"(org_id={cfg.continuous_shadow_org_id}, db_path={cfg.db_path}, "
        f"state_file={cfg.state_file})",
        file=sys.stderr,
    )
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    finally:
        stop_event.set()
        worker_thread.join(timeout=10)
    return 0


def cmd_replay(cfg: DaemonConfig, *, commit: str = None, from_sha: str = None) -> int:
    """Enqueue commits.

    - ``--commit <sha>``: enqueue exactly one SHA.
    - ``--from <sha>``: enqueue every commit in ``<sha>..origin/main`` of
      the local core clone (oldest first, FIFO order).
    """
    queue_mod.init_db(cfg.db_path)
    shas: list[str] = []
    if commit:
        shas.append(commit.strip().lower())
    elif from_sha:
        # Need the cache clone.
        from . import cache as cache_mod

        core_clone = cache_mod.ensure_core_clone(
            cache_dir=cfg.cache_dir, core_repo_url=cfg.core_repo_url
        )
        import subprocess

        proc = subprocess.run(
            ["git", "rev-list", "--reverse", f"{from_sha}..origin/main"],
            cwd=str(core_clone),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            print(
                f"[ingest-daemon] ERROR: git rev-list failed: {proc.stderr}",
                file=sys.stderr,
            )
            return 1
        shas = [
            line.strip().lower()
            for line in proc.stdout.splitlines()
            if line.strip()
        ]
    else:
        print(
            "[ingest-daemon] ERROR: replay requires --commit <sha> or --from <sha>",
            file=sys.stderr,
        )
        return 1
    results = []
    for sha in shas:
        res = queue_mod.enqueue(
            cfg.db_path,
            sha,
            source="replay",
            max_attempts=cfg.max_attempts_per_commit,
        )
        res["commit_sha"] = sha
        results.append(res)
    print(json.dumps(results, indent=2))
    return 0


def cmd_bootstrap(cfg: DaemonConfig) -> int:
    """Apply schema to a fresh DB."""
    queue_mod.init_db(cfg.db_path)
    print(f"[ingest-daemon] bootstrap: schema applied at {cfg.db_path}")
    return 0


def cmd_status(cfg: DaemonConfig) -> int:
    """Pretty-print the ``/status`` payload (no HTTP)."""
    queue_mod.init_db(cfg.db_path)
    payload = receiver_mod.compute_status(cfg)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atlas-shadow-ingest-daemon",
        description="Continuous-ingest daemon for atlas-shadow (D5).",
    )
    parser.add_argument(
        "--config",
        default="shadow-config.yaml",
        help="Path to shadow-config.yaml (default: ./shadow-config.yaml)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Run receiver + worker in the foreground.")
    rp = sub.add_parser("replay", help="Enqueue a commit or commit range.")
    rp.add_argument("--commit", help="Single SHA to enqueue.")
    rp.add_argument("--from", dest="from_sha", help="Enqueue range <sha>..origin/main.")
    sub.add_parser("bootstrap", help="Apply DB schema (idempotent).")
    sub.add_parser("status", help="Print /status payload (no HTTP needed).")

    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config))
    if args.cmd == "serve":
        return cmd_serve(cfg)
    if args.cmd == "replay":
        return cmd_replay(cfg, commit=args.commit, from_sha=args.from_sha)
    if args.cmd == "bootstrap":
        return cmd_bootstrap(cfg)
    if args.cmd == "status":
        return cmd_status(cfg)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
