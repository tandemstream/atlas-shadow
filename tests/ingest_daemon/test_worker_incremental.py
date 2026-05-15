"""T4.2 — AG3 + AG4: incremental wiring + SCIP-blob janitor (atlas-shadow).

Packet: ``2026-05-14-atlas-shadow-substrate-enablers-v1`` (P1, T4 unchanged
by the 2026-05-15 reframe).

AG3 — `worker.process_one` threads the prior `code_revision_id` from
`state_file.read_state` into `_run_ingest` so the daemon engages
Atlas's `file_memoization.ingest_with_carry_forward` path [qa:q4].

AG4 — On success the worker deletes the SCIP blob from disk
(`scip_path.unlink(missing_ok=True)`). On failure the blob is retained
for debugging — the failure branch returns early and never reaches
the unlink call (per D-P1-4).

The tests reuse the existing `daemon_config`, `db_path`, and
`state_file` fixtures from `tests/ingest_daemon/conftest.py` (same
fixtures used by `tests/ingest_daemon/test_worker.py`). All ingest
side-effects are stubbed via the worker's keyword-injection seams
(`_build_scip`, `_run_ingest`, `_ensure_clone`, `_checkout_worktree`,
`_read_state`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from atlas_shadow.ingest_daemon import ledger as ledger_mod
from atlas_shadow.ingest_daemon import queue as queue_mod
from atlas_shadow.ingest_daemon import state_file as state_mod
from atlas_shadow.ingest_daemon import worker as worker_mod


def _make_claim(sha: str = "abc" + "0" * 37) -> dict:
    return {
        "queue_id": 1,
        "commit_sha": sha,
        "attempt_number": 1,
        "started_at": "2026-05-14T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# AG3 — incremental wiring (state-file read → parent_code_revision_id pass-through)
# ---------------------------------------------------------------------------


def test_incremental_parent_revision_threaded_when_state_exists(
    daemon_config, db_path, state_file,
):
    """AG3 — when the state file has a prior `latest_code_revision_id`,
    the worker reads it and threads it into `_run_ingest` as
    `parent_code_revision_id`. The dogfood CLI then emits
    `--incremental --parent-code-revision-id <uuid>` to the subprocess
    (covered by `test_worker.py::test_dogfood_ingest_argv_with_parent_revision_appends_incremental_flags`).
    """
    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    captured: dict[str, Any] = {}

    def fake_ensure_clone(*, cache_dir, core_repo_url):
        return cache_dir / "core"

    def fake_checkout(*, cache_dir, commit_sha, core_clone):
        return cache_dir / "worktrees" / commit_sha[:12]

    def fake_build_scip(*, source_root, commit_sha, cache_dir, indexer_version, timeout_seconds):
        scip = cache_dir / "scip" / f"core-{commit_sha}.scip"
        scip.parent.mkdir(parents=True, exist_ok=True)
        scip.write_bytes(b"fake-scip")
        return scip

    def fake_run_ingest(*, parent_code_revision_id=None, **kwargs):
        captured["parent_code_revision_id"] = parent_code_revision_id
        return {
            "code_revision_id": "22222222-2222-2222-2222-222222222222",
            "chunk_stats": {"total_chunks": 1},
            "counts": {},
            "latency_ms": 100,
        }

    # Inject a prior state — same shape state_mod.write_state writes.
    fake_prior_uuid = "11111111-1111-1111-1111-111111111111"
    def fake_read_state(state_file_path):
        return {
            "latest_commit_ingested": "deadbeef" + "0" * 32,
            "latest_code_revision_id": fake_prior_uuid,
            "updated_at": "2026-05-14T00:00:00+00:00",
            "daemon_pid": 12345,
        }

    outcome = worker_mod.process_one(
        daemon_config,
        claim,
        _build_scip=fake_build_scip,
        _run_ingest=fake_run_ingest,
        _ensure_clone=fake_ensure_clone,
        _checkout_worktree=fake_checkout,
        _read_state=fake_read_state,
    )

    assert outcome["status"] == "succeeded"
    assert captured["parent_code_revision_id"] == fake_prior_uuid


def test_no_incremental_parent_on_cold_start(daemon_config, db_path, state_file):
    """AG3 — when the state file is absent (cold start), the worker
    passes `parent_code_revision_id=None` so the daemon's first
    ingest is a full ingest (byte-identical to pre-P1 behavior).
    """
    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    captured: dict[str, Any] = {}

    def fake_ensure_clone(*, cache_dir, core_repo_url):
        return cache_dir / "core"

    def fake_checkout(*, cache_dir, commit_sha, core_clone):
        return cache_dir / "worktrees" / commit_sha[:12]

    def fake_build_scip(*, source_root, commit_sha, cache_dir, indexer_version, timeout_seconds):
        scip = cache_dir / "scip" / f"core-{commit_sha}.scip"
        scip.parent.mkdir(parents=True, exist_ok=True)
        scip.write_bytes(b"fake-scip")
        return scip

    def fake_run_ingest(*, parent_code_revision_id=None, **kwargs):
        captured["parent_code_revision_id"] = parent_code_revision_id
        return {
            "code_revision_id": "33333333-3333-3333-3333-333333333333",
            "chunk_stats": {"total_chunks": 1},
            "counts": {},
            "latency_ms": 100,
        }

    # state_file fixture is a path that doesn't exist yet — default
    # state_mod.read_state returns None on missing file. We don't
    # inject _read_state so the default fires.
    outcome = worker_mod.process_one(
        daemon_config,
        claim,
        _build_scip=fake_build_scip,
        _run_ingest=fake_run_ingest,
        _ensure_clone=fake_ensure_clone,
        _checkout_worktree=fake_checkout,
    )

    assert outcome["status"] == "succeeded"
    assert captured["parent_code_revision_id"] is None


def test_malformed_state_file_does_not_propagate_parent(
    daemon_config, db_path, state_file,
):
    """AG3 — defensive — if `read_state` returns a non-dict (corrupt
    file silently parsed differently, future schema change, etc.),
    the worker treats it as cold start and does NOT pass a parent.
    """
    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    captured: dict[str, Any] = {}

    def fake_ensure_clone(*, cache_dir, core_repo_url):
        return cache_dir / "core"

    def fake_checkout(*, cache_dir, commit_sha, core_clone):
        return cache_dir / "worktrees" / commit_sha[:12]

    def fake_build_scip(*, source_root, commit_sha, cache_dir, indexer_version, timeout_seconds):
        scip = cache_dir / "scip" / f"core-{commit_sha}.scip"
        scip.parent.mkdir(parents=True, exist_ok=True)
        scip.write_bytes(b"fake-scip")
        return scip

    def fake_run_ingest(*, parent_code_revision_id=None, **kwargs):
        captured["parent_code_revision_id"] = parent_code_revision_id
        return {"code_revision_id": "rev-Y", "chunk_stats": {}, "counts": {}, "latency_ms": 10}

    def fake_read_state(state_file_path):
        return ["unexpected", "list", "instead", "of", "dict"]  # type: ignore[return-value]

    outcome = worker_mod.process_one(
        daemon_config,
        claim,
        _build_scip=fake_build_scip,
        _run_ingest=fake_run_ingest,
        _ensure_clone=fake_ensure_clone,
        _checkout_worktree=fake_checkout,
        _read_state=fake_read_state,
    )

    assert outcome["status"] == "succeeded"
    assert captured["parent_code_revision_id"] is None


# ---------------------------------------------------------------------------
# AG4 — SCIP-blob janitor
# ---------------------------------------------------------------------------


def test_scip_blob_deleted_on_success(daemon_config, db_path, state_file):
    """AG4 — on a successful ingest the worker deletes the on-disk
    SCIP blob (`scip_path.unlink(missing_ok=True)`) so the daemon's
    cache_dir doesn't grow linearly with commit count (~48 MB / commit).
    Per D-P1-4: success path deletes; failure path retains.
    """
    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    captured: dict[str, Any] = {}

    def fake_ensure_clone(*, cache_dir, core_repo_url):
        return cache_dir / "core"

    def fake_checkout(*, cache_dir, commit_sha, core_clone):
        return cache_dir / "worktrees" / commit_sha[:12]

    def fake_build_scip(*, source_root, commit_sha, cache_dir, indexer_version, timeout_seconds):
        scip = cache_dir / "scip" / f"core-{commit_sha}.scip"
        scip.parent.mkdir(parents=True, exist_ok=True)
        scip.write_bytes(b"fake-scip-bytes")
        captured["scip_path"] = scip
        # Pre-condition: the blob exists immediately after build.
        assert scip.exists()
        return scip

    def fake_run_ingest(*, parent_code_revision_id=None, **kwargs):
        # Mid-ingest: blob must still exist (ingest reads it).
        assert captured["scip_path"].exists()
        return {
            "code_revision_id": "44444444-4444-4444-4444-444444444444",
            "chunk_stats": {"total_chunks": 1},
            "counts": {},
            "latency_ms": 100,
        }

    outcome = worker_mod.process_one(
        daemon_config,
        claim,
        _build_scip=fake_build_scip,
        _run_ingest=fake_run_ingest,
        _ensure_clone=fake_ensure_clone,
        _checkout_worktree=fake_checkout,
    )

    assert outcome["status"] == "succeeded"
    # Post-condition: the blob is gone after a successful process_one.
    assert not captured["scip_path"].exists(), (
        f"SCIP blob still present after successful ingest: {captured['scip_path']!s}. "
        f"The T3 janitor (`scip_path.unlink(missing_ok=True)`) did not fire."
    )


def test_scip_blob_retained_on_run_ingest_failure(daemon_config, db_path, state_file):
    """AG4 — on a failed ingest (run_ingest raises) the worker does
    NOT delete the SCIP blob; it's retained for debugging. The
    failure branch returns early and never reaches the unlink call.
    """
    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    captured: dict[str, Any] = {}

    def fake_ensure_clone(*, cache_dir, core_repo_url):
        return cache_dir / "core"

    def fake_checkout(*, cache_dir, commit_sha, core_clone):
        return cache_dir / "worktrees" / commit_sha[:12]

    def fake_build_scip(*, source_root, commit_sha, cache_dir, indexer_version, timeout_seconds):
        scip = cache_dir / "scip" / f"core-{commit_sha}.scip"
        scip.parent.mkdir(parents=True, exist_ok=True)
        scip.write_bytes(b"fake-scip-bytes")
        captured["scip_path"] = scip
        return scip

    def fake_run_ingest(**kwargs):
        raise RuntimeError("simulated dogfood ingest failure")

    outcome = worker_mod.process_one(
        daemon_config,
        claim,
        _build_scip=fake_build_scip,
        _run_ingest=fake_run_ingest,
        _ensure_clone=fake_ensure_clone,
        _checkout_worktree=fake_checkout,
    )

    assert outcome["status"] == "failed"
    assert "simulated dogfood ingest failure" in outcome["error"]
    # Post-condition: blob retained (per D-P1-4).
    assert captured["scip_path"].exists(), (
        f"SCIP blob missing after failed ingest: {captured['scip_path']!s}. "
        f"The failure branch returned early — janitor must not have fired, "
        f"yet the blob isn't present. Did the janitor run in the failure path?"
    )


def test_scip_blob_janitor_silent_when_already_deleted(daemon_config, db_path, state_file):
    """AG4 (edge case) — `missing_ok=True` ensures the janitor doesn't
    crash when an operator (or a prior cleanup pass) deleted the blob
    between ingest completion and the janitor call. Outcome still
    succeeded; no spurious failure status.
    """
    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    def fake_ensure_clone(*, cache_dir, core_repo_url):
        return cache_dir / "core"

    def fake_checkout(*, cache_dir, commit_sha, core_clone):
        return cache_dir / "worktrees" / commit_sha[:12]

    def fake_build_scip(*, source_root, commit_sha, cache_dir, indexer_version, timeout_seconds):
        # Build it then immediately delete it — simulates the
        # operator-cleanup race that missing_ok=True is meant to
        # cover.
        scip = cache_dir / "scip" / f"core-{commit_sha}.scip"
        scip.parent.mkdir(parents=True, exist_ok=True)
        scip.write_bytes(b"x")
        scip.unlink()
        return scip

    def fake_run_ingest(**kwargs):
        return {
            "code_revision_id": "55555555-5555-5555-5555-555555555555",
            "chunk_stats": {"total_chunks": 1},
            "counts": {},
            "latency_ms": 50,
        }

    outcome = worker_mod.process_one(
        daemon_config,
        claim,
        _build_scip=fake_build_scip,
        _run_ingest=fake_run_ingest,
        _ensure_clone=fake_ensure_clone,
        _checkout_worktree=fake_checkout,
    )

    # No crash, no failure status — the janitor's missing_ok handled
    # the already-deleted file gracefully.
    assert outcome["status"] == "succeeded"
