"""worker — single-thread queue drainer that runs ingest jobs.

The worker is invoked on a tight loop by ``entrypoint.run_forever``:

  1. Reset any stale ``running`` rows (startup-only).
  2. ``queue.claim_next()`` — pop the oldest queued row or sleep.
  3. Build SCIP (``scip_builder.build_scip``).
  4. Shell out to the dogfood ingest CLI
     (``scip_builder.run_dogfood_ingest``) — single subprocess that does
     both ``ingest_scip_upload`` AND ``chunk_code_revision``.
  5. On success (exit 0):
     a. Write a succeeded ledger row.
     b. Atomically rewrite ``.daemon-state.json``.
     c. Mark the queue row succeeded.
  6. On failure: write a failed ledger row; mark the queue row failed.
     Re-enqueue handled by the receiver/replay layer next time the SHA
     comes through.

Per amendment decision #3: NO ROLLBACK. Atlas's ``ingest_scip_upload``
is idempotent on ``(org_id, repo_url, commit_sha, indexer_version)`` —
a re-enqueued SHA will short-circuit at the cache-hit path. Per
amendment decision #11: backlog catch-up is operator-driven via
``make ingest-replay``; the worker doesn't auto-backfill.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import cache as cache_mod
from . import ledger as ledger_mod
from . import queue as queue_mod
from . import scip_builder as scip_mod
from . import state_file as state_mod
from .config import DaemonConfig


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_doc_ingest(
    *,
    core_repo_path: Path,
    org_id: str,
    commit_sha: str,
    repo_url: str,
    timeout_seconds: int,
    _subprocess_run: Callable = subprocess.run,
) -> dict[str, Any]:
    """Shell out to ``scripts.shadow_ingest_docs`` for the new SHA.

    The doc-side counterpart to ``scip_builder.run_dogfood_ingest``.
    Reads doc-flavored files (``.md``/``.yaml``/etc., minus ``docs/work/``
    packet ground truth) via ``git show <commit_sha>:<path>`` and ingests
    via ``core.ingest.pipeline.ingest`` into the same atlas DB the SCIP
    path writes to.

    Atlas's file-content-hash memoization ensures repeat runs are cheap:
    files whose content hasn't changed between commits skip re-embedding.
    Cold runs (first time atlas has seen these doc bodies) take ~10-15 min
    per 1000 files; subsequent commits are typically 1-3 min.

    Returns a dict::

        {
            "status": "succeeded" | "failed" | "timeout" | "unparseable",
            "files_seen": int | None,
            "files_ingested": int | None,
            "artifact_count": int | None,
            "chunk_count": int | None,
            "latency_ms": int | None,
            "error": str | None,
        }

    Never raises — every failure mode collapses to a dict the caller can
    log + degrade past. SCIP ingest already succeeded by the time we get
    here; doc-ingest failure must not invalidate that.
    """
    atlas_leaf = core_repo_path / "products" / "tandem" / "packages" / "python" / "atlas"
    venv_py = atlas_leaf / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return {
            "status": "failed",
            "files_seen": None,
            "files_ingested": None,
            "artifact_count": None,
            "chunk_count": None,
            "latency_ms": None,
            "error": f"atlas leaf venv python not found at {venv_py}",
        }
    cmd = [
        str(venv_py), "-m", "scripts.shadow_ingest_docs",
        "--org-id", org_id,
        "--commit-sha", commit_sha,
        "--repo-path", str(core_repo_path),
        "--repo-url", repo_url,
        "--quiet",
    ]
    started = time.perf_counter()
    try:
        proc = _subprocess_run(
            cmd,
            cwd=str(atlas_leaf),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "files_seen": None,
            "files_ingested": None,
            "artifact_count": None,
            "chunk_count": None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"doc ingest timed out after {exc.timeout}s",
        }
    except (OSError, ValueError) as exc:
        return {
            "status": "failed",
            "files_seen": None,
            "files_ingested": None,
            "artifact_count": None,
            "chunk_count": None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }

    elapsed = int((time.perf_counter() - started) * 1000)
    # shadow_ingest_docs.py with --quiet emits only the final manifest
    # JSON to stdout. Exit 0 = full success; exit 2 = soft-pass (partial,
    # e.g. some artifacts zero-chunked); exit 1 = fatal setup error.
    if proc.returncode not in (0, 2):
        return {
            "status": "failed",
            "files_seen": None,
            "files_ingested": None,
            "artifact_count": None,
            "chunk_count": None,
            "latency_ms": elapsed,
            "error": (
                f"doc ingest exit={proc.returncode}; "
                f"stderr={proc.stderr[:500] if proc.stderr else ''!r}"
            ),
        }
    try:
        manifest = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "unparseable",
            "files_seen": None,
            "files_ingested": None,
            "artifact_count": None,
            "chunk_count": None,
            "latency_ms": elapsed,
            "error": f"could not parse doc-ingest manifest: {exc}",
        }
    counts = manifest.get("counts") or {}
    return {
        "status": "succeeded" if proc.returncode == 0 else "partial",
        "files_seen": manifest.get("files_seen"),
        "files_ingested": manifest.get("files_ingested"),
        "artifact_count": counts.get("artifact_count"),
        "chunk_count": counts.get("chunk_count"),
        "latency_ms": manifest.get("latency_ms") or elapsed,
        "error": None,
    }


def process_one(
    cfg: DaemonConfig,
    claim: dict[str, Any],
    *,
    _build_scip: Callable = scip_mod.build_scip,
    _run_ingest: Callable = scip_mod.run_dogfood_ingest,
    _run_doc_ingest: Callable = run_doc_ingest,
    _ensure_clone: Callable = cache_mod.ensure_core_clone,
    _checkout_worktree: Callable = cache_mod.checkout_worktree_at_commit,
    _write_state: Callable = state_mod.write_state,
    _read_state: Callable = state_mod.read_state,
) -> dict[str, Any]:
    """Process one claimed queue row end-to-end.

    Returns a dict::

        {"status": "succeeded"|"failed", "ledger_id": int, "error": str|None,
         "code_revision_id": str|None, "latency_ms": int}

    Never raises — exceptions are caught, recorded to the ledger, and
    the queue row is marked failed. The caller (entrypoint) just loops.

    Sequence per amendment decision #12:
      1. cache clone+fetch+worktree
      2. build SCIP
      3. shell out to dogfood ingest CLI
      4. (a) ledger row succeeded
         (b) state file written via atomic-rename
         (c) queue row marked succeeded
      Failure of (4a) or (4b) after a successful (3) is logged to stderr
      but doesn't trigger a retry — Atlas has the data.
    """
    commit_sha = claim["commit_sha"]
    queue_id = claim["queue_id"]
    attempt_number = claim["attempt_number"]
    started_at = claim["started_at"]
    perf_start = time.perf_counter()

    try:
        core_clone = _ensure_clone(
            cache_dir=cfg.cache_dir,
            core_repo_url=cfg.core_repo_url,
        )
        source_root = _checkout_worktree(
            cache_dir=cfg.cache_dir,
            commit_sha=commit_sha,
            core_clone=core_clone,
        )
        scip_path = _build_scip(
            source_root=source_root,
            commit_sha=commit_sha,
            cache_dir=cfg.cache_dir,
            indexer_version=cfg.scip_indexer_version,
            timeout_seconds=cfg.scip_build_timeout_seconds,
        )
        scip_size = scip_path.stat().st_size if scip_path.exists() else None
        # T2 (P1, packet 2026-05-14-atlas-shadow-substrate-enablers-v1):
        # read the prior ingest's code_revision_id from the daemon state
        # file and thread it through to the dogfood CLI as
        # --parent-code-revision-id. Atlas's ingest_scip_upload then
        # dispatches to file_memoization.ingest_with_carry_forward for
        # unchanged files. Cold start (no state file) → parent stays
        # None → daemon's first ingest is a full ingest (byte-identical
        # to pre-P1 behavior).
        prior_state = _read_state(cfg.state_file)
        parent_code_revision_id: Optional[str] = None
        if isinstance(prior_state, dict):
            parent_code_revision_id = prior_state.get("latest_code_revision_id")
        ingest_payload = _run_ingest(
            core_repo_path=cfg.core_repo_path,
            org_id=cfg.continuous_shadow_org_id,
            scip_path=scip_path,
            source_root=source_root,
            commit_sha=commit_sha,
            repo_url=cfg.repo_url,
            parent_code_revision_id=parent_code_revision_id,
            timeout_seconds=cfg.ingest_shell_out_timeout_seconds,
        )
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        latency_ms = int((time.perf_counter() - perf_start) * 1000)
        ledger_id = ledger_mod.insert_terminal_attempt(
            cfg.db_path,
            commit_sha=commit_sha,
            status="failed",
            started_at=started_at,
            attempt_number=attempt_number,
            latency_ms=latency_ms,
            error_message=err_text[:4000],
        )
        queue_mod.mark_terminal(
            cfg.db_path,
            queue_id,
            status="failed",
            error=str(exc)[:1000],
        )
        return {
            "status": "failed",
            "ledger_id": ledger_id,
            "error": str(exc),
            "code_revision_id": None,
            "latency_ms": latency_ms,
        }

    code_revision_id: Optional[str] = ingest_payload.get("code_revision_id")
    chunk_stats: Optional[dict[str, Any]] = ingest_payload.get("chunk_stats")
    counts: Optional[dict[str, Any]] = ingest_payload.get("counts")
    chunker_total: Optional[int] = None
    if isinstance(chunk_stats, dict):
        chunker_total = (
            chunk_stats.get("total_chunks")
            or chunk_stats.get("chunk_count")
            or chunk_stats.get("chunk_refs")
        )

    # P3 follow-up: run the doc-side ingest after SCIP succeeds. Keeps
    # the doc corpus current with code so doc-anchored receipts resolve
    # via the precise db_commit_scoped tier instead of falling through
    # to git_receipt_snapshot. Disabled via shadow-config.yaml
    # ``ingest_daemon.doc_ingest_enabled: false`` for code-only mode.
    #
    # Doc-ingest failure does NOT fail the whole commit — SCIP already
    # succeeded and atlas has the code data. The failure is logged to
    # stderr + recorded in counts_json so operators can audit drift.
    doc_outcome: Optional[dict[str, Any]] = None
    if cfg.doc_ingest_enabled:
        try:
            doc_outcome = _run_doc_ingest(
                core_repo_path=cfg.core_repo_path,
                org_id=cfg.continuous_shadow_org_id,
                commit_sha=commit_sha,
                repo_url=cfg.repo_url,
                timeout_seconds=cfg.doc_ingest_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — never let doc raise
            doc_outcome = {
                "status": "error",
                "files_seen": None,
                "files_ingested": None,
                "artifact_count": None,
                "chunk_count": None,
                "latency_ms": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if doc_outcome.get("status") not in ("succeeded", "partial"):
            print(
                f"[ingest-daemon] WARN: doc ingest "
                f"status={doc_outcome.get('status')} for {commit_sha} "
                f"(SCIP succeeded; doc corpus may be stale): "
                f"{doc_outcome.get('error')}",
                file=sys.stderr,
            )
        elif doc_outcome.get("status") == "partial":
            # Soft-pass: shadow_ingest_docs returned exit 2 (some
            # artifacts have zero chunks, or some files errored). Still
            # useful — log so operator can investigate the gaps.
            print(
                f"[ingest-daemon] WARN: doc ingest partial for {commit_sha}: "
                f"files_ingested={doc_outcome.get('files_ingested')} / "
                f"files_seen={doc_outcome.get('files_seen')}",
                file=sys.stderr,
            )

    # Stash doc-ingest outcome under counts so the ledger row records
    # both code + doc stats without a schema migration.
    if doc_outcome is not None:
        if counts is None:
            counts = {}
        counts = {**counts, "doc_ingest": doc_outcome}

    latency_ms = int((time.perf_counter() - perf_start) * 1000)

    # (a) ledger row
    ledger_id = ledger_mod.insert_terminal_attempt(
        cfg.db_path,
        commit_sha=commit_sha,
        status="succeeded",
        started_at=started_at,
        attempt_number=attempt_number,
        code_revision_id=str(code_revision_id) if code_revision_id else None,
        scip_path=str(scip_path),
        source_root=str(source_root),
        scip_size_bytes=scip_size,
        chunker_stats_total=int(chunker_total) if chunker_total is not None else None,
        counts=counts,
        latency_ms=latency_ms,
    )

    # (b) state file — log+continue on failure (amendment decision #12)
    if code_revision_id:
        try:
            _write_state(
                state_file_path=cfg.state_file,
                latest_commit_ingested=commit_sha,
                latest_code_revision_id=str(code_revision_id),
            )
        except Exception as exc:
            print(
                f"[ingest-daemon] WARN: state file write failed after successful "
                f"ingest of {commit_sha} (code_revision_id={code_revision_id}): "
                f"{type(exc).__name__}: {exc}. Atlas has the data; run "
                f"`make ingest-status --refresh` to re-derive state from the ledger.",
                file=sys.stderr,
            )

    # (c) queue row succeeded
    queue_mod.mark_terminal(cfg.db_path, queue_id, status="succeeded")

    # T3 (P1, packet 2026-05-14-atlas-shadow-substrate-enablers-v1):
    # SCIP-blob janitor — delete the on-disk blob after a successful
    # ingest to keep disk usage bounded. ~48 MB/commit; without this,
    # the daemon's cache_dir/scip/ grows linearly with commit count.
    # Atlas has the data; the blob is only useful for debugging an
    # ingest failure (and we don't reach this branch on failure — the
    # failure branch above returns early and skips this cleanup, so
    # the blob is naturally retained for inspection per D-P1-4).
    try:
        scip_path.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"[ingest-daemon] WARN: SCIP blob delete failed for {scip_path} "
            f"after successful ingest of {commit_sha} "
            f"(code_revision_id={code_revision_id}): "
            f"{type(exc).__name__}: {exc}. Continuing — Atlas has the data.",
            file=sys.stderr,
        )

    return {
        "status": "succeeded",
        "ledger_id": ledger_id,
        "error": None,
        "code_revision_id": str(code_revision_id) if code_revision_id else None,
        "latency_ms": latency_ms,
    }


def drain_once(
    cfg: DaemonConfig,
    *,
    _claim_next: Callable = queue_mod.claim_next,
    _process_one: Callable = process_one,
) -> Optional[dict[str, Any]]:
    """Claim and process one queue row; return its outcome or None.

    Returns None when the queue is empty (caller sleeps).
    """
    claim = _claim_next(cfg.db_path)
    if claim is None:
        return None
    return _process_one(cfg, claim)


def warn_if_backlog(
    cfg: DaemonConfig,
    *,
    _subprocess_run: Callable = None,
) -> Optional[int]:
    """Per amendment decision #11: on startup, if state-file SHA is N
    commits behind origin/main, emit ONE stderr warning.

    Returns the integer N (number of commits behind) when a warning is
    emitted; returns None when state is fresh / unknown / no clone yet.

    Does NOT auto-backfill — operator decides via ``make ingest-replay
    --from=<state.json:latest_commit_ingested>``.
    """
    import subprocess as _subprocess
    import sys

    if _subprocess_run is None:
        _subprocess_run = _subprocess.run

    state = state_mod.read_state(cfg.state_file)
    latest = state.get("latest_commit_ingested") if state else None
    if not latest:
        return None
    core_clone = cfg.cache_dir / "core"
    if not (core_clone / ".git").exists():
        return None
    # Refresh remote first. §13 subprocess discipline: timeouts on every
    # subprocess call — pre-existing D5 calls in this function gained
    # explicit timeouts as part of P2's acceptance gates (§7 audit).
    _subprocess_run(
        ["git", "fetch", "--quiet", "origin", "main"],
        cwd=str(core_clone),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    proc = _subprocess_run(
        ["git", "rev-list", "--count", f"{latest}..origin/main"],
        cwd=str(core_clone),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        n = int(proc.stdout.strip())
    except (TypeError, ValueError):
        return None
    if n > 0:
        print(
            f"[ingest-daemon] WARN: state file's latest_commit_ingested={latest} "
            f"is {n} commits behind origin/main. Operator may run "
            f"`make ingest-replay FROM={latest}` to backfill, or wait for "
            f"webhooks to catch up. Daemon does NOT auto-backfill.",
            file=sys.stderr,
        )
    return n
