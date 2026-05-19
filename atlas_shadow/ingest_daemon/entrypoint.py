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

from . import grader_service as grader_service_mod
from . import queue as queue_mod
from . import receiver as receiver_mod
from . import reconciler as reconciler_mod
from . import visibility_bootstrap as visibility_bootstrap_mod
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
    try:
        result = visibility_bootstrap_mod.ensure_shadow_runner_visibility(
            org_id=cfg.continuous_shadow_org_id,
            principal_id=cfg.default_principal_id,
        )
        print(
            f"[ingest-daemon] visibility bootstrap ok "
            f"(role={result.role_name}, active_grants={result.active_grant_count})",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"[ingest-daemon] WARN: visibility bootstrap failed; "
            f"code retrieval may return empty for the default principal "
            f"({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
    stop_event = threading.Event()
    worker_thread = threading.Thread(
        target=_worker_loop,
        args=(cfg, stop_event),
        name="ingest-daemon-worker",
        daemon=True,
    )
    worker_thread.start()
    reconciler_thread: threading.Thread | None = None
    if cfg.reconciler_enabled:
        reconciler_thread = threading.Thread(
            target=reconciler_mod.run_loop,
            args=(cfg, stop_event),
            name="ingest-daemon-reconciler",
            daemon=True,
        )
        reconciler_thread.start()
        print(
            f"[ingest-daemon] reconciler enabled "
            f"(interval={cfg.reconciler_interval_seconds}s, "
            f"core_repo_url={cfg.core_repo_url})",
            file=sys.stderr,
        )
    else:
        print(
            "[ingest-daemon] reconciler disabled "
            "(set ingest_daemon.reconciler_enabled: true to re-enable)",
            file=sys.stderr,
        )
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
    # T10 (P2 packet 2026-05-14-atlas-shadow-pre-merge-grading-gate-v1):
    # wire the PR-grading orchestrator into the receiver. The receiver's
    # default handler is a stderr-logging stub; in production we pass the
    # real `handle_pr_event` so opened/synchronize/reopened PR events
    # run the full grading pipeline via a FastAPI BackgroundTask.
    app = receiver_mod.create_app(
        cfg, pr_event_handler=grader_service_mod.handle_pr_event
    )
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
        if reconciler_thread is not None:
            reconciler_thread.join(timeout=10)
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
            timeout=120,
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


def cmd_bootstrap_visibility(cfg: DaemonConfig) -> int:
    """Ensure the shadow runner principal can see internal corpus chunks."""
    try:
        result = visibility_bootstrap_mod.ensure_shadow_runner_visibility(
            org_id=cfg.continuous_shadow_org_id,
            principal_id=cfg.default_principal_id,
        )
    except Exception as exc:
        print(
            f"[ingest-daemon] visibility bootstrap failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result.__dict__, indent=2, sort_keys=True))
    return 0


def cmd_status(cfg: DaemonConfig) -> int:
    """Pretty-print the ``/status`` payload (no HTTP)."""
    queue_mod.init_db(cfg.db_path)
    payload = receiver_mod.compute_status(cfg)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_grading_verify(cfg: DaemonConfig) -> int:
    """Verify the pre-merge grading gate's environment is configured.

    Checks (in order, each emits a line of pass / fail / warn):
      1. ``GITHUB_WEBHOOK_SECRET`` env var present (HMAC verify requires it).
      2. ``GITHUB_ATLAS_SHADOW_TOKEN`` env var present (GH check + comment
         API auth).
      3. Atlas DB URL present in env-var chain
         (ATLAS_SHADOW_DOC_RESOLVER_DB_URL -> ATLAS_ADMIN_DB_URL ->
         ATLAS_DB_URL). T4a needs this for the primary lookup path.
      4. ``cfg.shadow_runs_dir`` writable.
      5. ``cfg.core_repo_path`` resolvable (for the T4a git fallback).
      6. ``psycopg2`` importable (T4a direct DB path).

    Returns 0 when all hard requirements (1, 2, 4, 5, 6) pass; 1 when any
    fails. (3 is treated as a warning — T4a degrades to git-receipt-
    snapshot for doc receipts when the DB lookup is skipped.)
    """
    from .config import DEFAULTS  # noqa: F401  (re-export check)

    failures = 0
    warnings = 0

    secret = cfg.webhook_secret
    if secret:
        print("PASS: GITHUB_WEBHOOK_SECRET is set")
    else:
        failures += 1
        print(
            "FAIL: GITHUB_WEBHOOK_SECRET is unset; receiver returns 503 "
            "for all webhook calls. Export it via env or systemd."
        )

    gh_token = (
        __import__("os").environ.get("GITHUB_ATLAS_SHADOW_TOKEN")
        or __import__("os").environ.get("GITHUB_TOKEN")
    )
    if gh_token:
        print("PASS: GITHUB_ATLAS_SHADOW_TOKEN is set")
    else:
        failures += 1
        print(
            "FAIL: GITHUB_ATLAS_SHADOW_TOKEN unset. The grader cannot "
            "post check_runs or PR comments without it."
        )

    db_url = None
    for var in (
        "ATLAS_SHADOW_DOC_RESOLVER_DB_URL",
        "ATLAS_ADMIN_DB_URL",
        "ATLAS_DB_URL",
    ):
        if __import__("os").environ.get(var):
            db_url = var
            break
    if db_url:
        print(f"PASS: doc-resolver DB URL configured via {db_url}")
    else:
        warnings += 1
        print(
            "WARN: no Atlas DB URL in env (tried "
            "ATLAS_SHADOW_DOC_RESOLVER_DB_URL, ATLAS_ADMIN_DB_URL, "
            "ATLAS_DB_URL). T4a primary lookup will be skipped; doc "
            "receipts will fall back to git_receipt_snapshot."
        )

    try:
        cfg.shadow_runs_dir.mkdir(parents=True, exist_ok=True)
        test_file = cfg.shadow_runs_dir / ".grading-verify.tmp"
        test_file.write_text("ok\n", encoding="utf-8")
        test_file.unlink()
        print(f"PASS: shadow_runs_dir writable at {cfg.shadow_runs_dir}")
    except (OSError, RuntimeError) as exc:
        failures += 1
        print(
            f"FAIL: shadow_runs_dir not writable: {exc}"
        )

    if cfg.core_repo_path.exists():
        print(f"PASS: core_repo_path resolvable at {cfg.core_repo_path}")
    else:
        failures += 1
        print(
            f"FAIL: core_repo_path does not exist: {cfg.core_repo_path}. "
            f"T4a's git_receipt_snapshot fallback needs a local clone."
        )

    try:
        import psycopg2  # type: ignore  # noqa: F401

        print("PASS: psycopg2 importable")
    except ImportError as exc:
        failures += 1
        print(
            f"FAIL: psycopg2 not installed ({exc}); T4a primary path "
            f"unavailable. Run `make setup`."
        )

    if failures:
        print(
            f"\n{failures} hard requirement(s) failed, {warnings} warning(s)."
        )
        return 1
    print(f"\nAll hard requirements passed. {warnings} warning(s).")
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
    sub.add_parser(
        "bootstrap-visibility",
        help="Grant the default shadow principal visibility over internal chunks.",
    )
    sub.add_parser("status", help="Print /status payload (no HTTP needed).")
    sub.add_parser(
        "grading-verify",
        help="T10 (P2): check env + paths for the pre-merge grading gate.",
    )

    # Phase 3 batch-grading subcommand.
    from . import grade_batch as grade_batch_mod
    grade_batch_mod.build_subparser(sub)

    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config))
    if args.cmd == "serve":
        return cmd_serve(cfg)
    if args.cmd == "replay":
        return cmd_replay(cfg, commit=args.commit, from_sha=args.from_sha)
    if args.cmd == "bootstrap":
        return cmd_bootstrap(cfg)
    if args.cmd == "bootstrap-visibility":
        return cmd_bootstrap_visibility(cfg)
    if args.cmd == "status":
        return cmd_status(cfg)
    if args.cmd == "grading-verify":
        return cmd_grading_verify(cfg)
    if args.cmd == "grade-packet-batch":
        return grade_batch_mod.cmd_grade_packet_batch(cfg, args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
