"""P3 follow-up: doc-ingest hook in the worker.

After every successful SCIP code-ingest, the worker shells out to
``scripts.shadow_ingest_docs`` so the doc corpus stays current with
code. This file covers:

- ``run_doc_ingest`` helper — happy path, exit code variants (0 / 2 /
  other), timeout, parse failure, missing venv.
- ``process_one`` integration — disabled flag, doc-ingest failure does
  not fail the commit, outcome recorded in counts.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from atlas_shadow.ingest_daemon import worker as worker_mod
from atlas_shadow.ingest_daemon.config import DaemonConfig


# ───────── run_doc_ingest helper ─────────────────────────────────────


def _ok_manifest() -> str:
    return json.dumps({
        "files_seen": 100,
        "files_ingested": 100,
        "counts": {"artifact_count": 100, "chunk_count": 1200},
        "latency_ms": 5000,
    })


def _make_proc(returncode: int, stdout: str = "", stderr: str = ""):
    """Build a subprocess.CompletedProcess-like mock."""
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_run_doc_ingest_happy_path(tmp_path: Path):
    """Exit 0 + parseable manifest → status=succeeded with counts.

    Codex r1 (PR #10): --repo-path must point at source_root (the
    daemon's checked-out worktree at the target commit), NOT
    core_repo_path (which the daemon never auto-advances and so
    typically doesn't have webhook-driven commits).
    """
    # Materialize the expected atlas leaf + venv so the venv-check passes.
    atlas_leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (atlas_leaf / ".venv" / "bin").mkdir(parents=True)
    (atlas_leaf / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    source_root = tmp_path / "cache" / "worktrees" / "abc0000"
    source_root.mkdir(parents=True)

    captured_cmd = []
    def _fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return _make_proc(0, stdout=_ok_manifest())

    out = worker_mod.run_doc_ingest(
        core_repo_path=tmp_path,
        source_root=source_root,
        org_id="org-1",
        commit_sha="abc0000",
        repo_url="https://github.com/example/repo",
        timeout_seconds=60,
        _subprocess_run=_fake_run,
    )
    assert out["status"] == "succeeded"
    assert out["files_seen"] == 100
    assert out["files_ingested"] == 100
    assert out["artifact_count"] == 100
    assert out["chunk_count"] == 1200
    assert out["latency_ms"] == 5000
    assert out["error"] is None
    # Verify the cmd shape: -m scripts.shadow_ingest_docs with all 4 args + --quiet
    assert "-m" in captured_cmd
    assert "scripts.shadow_ingest_docs" in captured_cmd
    assert "--org-id" in captured_cmd
    assert "org-1" in captured_cmd
    assert "--commit-sha" in captured_cmd
    assert "abc0000" in captured_cmd
    assert "--repo-path" in captured_cmd
    # Critical: --repo-path is source_root (worktree the daemon checked
    # out at the target SHA), not core_repo_path (operator's static
    # checkout). Codex r1 PR #10 bug fix.
    assert str(source_root) in captured_cmd
    assert str(tmp_path) not in [
        captured_cmd[i + 1] for i, v in enumerate(captured_cmd) if v == "--repo-path"
    ]
    assert "--repo-url" in captured_cmd
    assert "https://github.com/example/repo" in captured_cmd
    assert "--quiet" in captured_cmd


def test_run_doc_ingest_exit_2_is_partial_success(tmp_path: Path):
    """shadow_ingest_docs exits 2 on soft-pass (some artifacts zero-chunked
    or some files errored). Treat as partial — not failure."""
    atlas_leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (atlas_leaf / ".venv" / "bin").mkdir(parents=True)
    (atlas_leaf / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    out = worker_mod.run_doc_ingest(
        core_repo_path=tmp_path,
        source_root=tmp_path / "fake-source",
        org_id="org-1",
        commit_sha="abc",
        repo_url="https://example/repo",
        timeout_seconds=60,
        _subprocess_run=lambda cmd, **kw: _make_proc(2, stdout=_ok_manifest()),
    )
    assert out["status"] == "partial"
    assert out["files_ingested"] == 100  # manifest still parseable


def test_run_doc_ingest_exit_1_is_failure(tmp_path: Path):
    """Exit 1 (or anything not 0/2) → failed."""
    atlas_leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (atlas_leaf / ".venv" / "bin").mkdir(parents=True)
    (atlas_leaf / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    out = worker_mod.run_doc_ingest(
        core_repo_path=tmp_path,
        source_root=tmp_path / "fake-source",
        org_id="org-1",
        commit_sha="abc",
        repo_url="https://example/repo",
        timeout_seconds=60,
        _subprocess_run=lambda cmd, **kw: _make_proc(1, stderr="bad args"),
    )
    assert out["status"] == "failed"
    assert out["files_ingested"] is None
    assert "exit=1" in out["error"]
    assert "bad args" in out["error"]


def test_run_doc_ingest_timeout(tmp_path: Path):
    atlas_leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (atlas_leaf / ".venv" / "bin").mkdir(parents=True)
    (atlas_leaf / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=60)

    out = worker_mod.run_doc_ingest(
        core_repo_path=tmp_path,
        source_root=tmp_path / "fake-source",
        org_id="org-1",
        commit_sha="abc",
        repo_url="https://example/repo",
        timeout_seconds=60,
        _subprocess_run=_fake_run,
    )
    assert out["status"] == "timeout"
    assert "timed out after 60s" in out["error"]


def test_run_doc_ingest_unparseable_stdout(tmp_path: Path):
    """Exit 0 but stdout isn't valid JSON → status=unparseable so
    operator can audit."""
    atlas_leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (atlas_leaf / ".venv" / "bin").mkdir(parents=True)
    (atlas_leaf / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    out = worker_mod.run_doc_ingest(
        core_repo_path=tmp_path,
        source_root=tmp_path / "fake-source",
        org_id="org-1",
        commit_sha="abc",
        repo_url="https://example/repo",
        timeout_seconds=60,
        _subprocess_run=lambda cmd, **kw: _make_proc(0, stdout="not json"),
    )
    assert out["status"] == "unparseable"
    assert "could not parse" in out["error"]


def test_run_doc_ingest_missing_venv(tmp_path: Path):
    """Atlas leaf venv python not present → fail fast with clear error,
    don't even attempt the subprocess. Common operator error: forgot to
    bootstrap the venv in the shadow-runtime worktree."""
    # tmp_path has no atlas leaf — the venv path won't exist.
    out = worker_mod.run_doc_ingest(
        core_repo_path=tmp_path,
        source_root=tmp_path / "fake-source",
        org_id="org-1",
        commit_sha="abc",
        repo_url="https://example/repo",
        timeout_seconds=60,
        _subprocess_run=lambda cmd, **kw: pytest.fail("subprocess should not be called"),
    )
    assert out["status"] == "failed"
    assert "venv python not found" in out["error"]


# ───────── process_one integration ───────────────────────────────────


def _scip_ok_payload() -> dict:
    return {
        "code_revision_id": "rev-1",
        "chunk_stats": {"total_chunks": 50},
        "counts": {"artifacts": 25},
    }


def _make_cfg(tmp_path: Path, *, doc_ingest_enabled: bool = True) -> DaemonConfig:
    """Build a minimal DaemonConfig pointing at tmp paths."""
    return DaemonConfig(
        continuous_shadow_org_id="org-1",
        core_repo_path=tmp_path / "core",
        db_path=tmp_path / "ingest.db",
        cache_dir=tmp_path / "cache",
        state_file=tmp_path / "state.json",
        doc_ingest_enabled=doc_ingest_enabled,
        doc_ingest_timeout_seconds=60,
    )


class _FakeScipPath:
    def stat(self):
        return MagicMock(st_size=1024)
    def exists(self):
        return True
    def unlink(self, missing_ok=False):
        return None
    def __str__(self):
        return "/fake/scip.scip"


def _scip_stub_kwargs(monkeypatch, payload=None):
    """Return a dict of injectable callables for ``process_one`` that
    short-circuit cache/scip/state and supply a payload from
    ``run_dogfood_ingest``. Plus monkeypatch ``queue.mark_terminal`` and
    ``state_file`` writes (those go through module attributes, not
    callable injection).
    """
    if payload is None:
        payload = _scip_ok_payload()
    monkeypatch.setattr(worker_mod.queue_mod, "mark_terminal",
                        lambda *a, **kw: None)
    monkeypatch.setattr(worker_mod.state_mod, "write_state",
                        lambda **kw: None)
    return {
        "_build_scip": lambda *a, **kw: _FakeScipPath(),
        "_run_ingest": lambda *a, **kw: payload,
        "_ensure_clone": lambda *a, **kw: Path("/fake/clone"),
        "_checkout_worktree": lambda *a, **kw: Path("/fake/worktree"),
        "_read_state": lambda *a, **kw: None,
        "_write_state": lambda **kw: None,
    }


def _bootstrap_db(cfg: DaemonConfig):
    """Apply schema to the test DB so ledger writes succeed."""
    import sqlite3
    from pathlib import Path as _P
    schema_sql = (_P(worker_mod.__file__).parent / "schema.sql").read_text()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(cfg.db_path)) as conn:
        conn.executescript(schema_sql)


def _make_claim() -> dict:
    return {
        "queue_id": 1,
        "commit_sha": "abc" + "0" * 37,
        "attempt_number": 1,
        "started_at": "2026-05-15T00:00:00Z",
    }


def test_process_one_invokes_doc_ingest_when_enabled(tmp_path: Path, monkeypatch):
    """Default: doc_ingest_enabled=True → _run_doc_ingest fires after
    successful SCIP, outcome stashed under counts.doc_ingest.

    Also asserts (codex r1 PR #10 fix): source_root is threaded into
    _run_doc_ingest as a SEPARATE arg from core_repo_path. The fake
    pipeline's _checkout_worktree returns Path("/fake/worktree"), so
    that's what should appear as source_root in the call.
    """
    cfg = _make_cfg(tmp_path, doc_ingest_enabled=True)
    _bootstrap_db(cfg)
    stubs = _scip_stub_kwargs(monkeypatch)

    doc_calls = []
    def _fake_doc_ingest(**kwargs):
        doc_calls.append(kwargs)
        return {
            "status": "succeeded", "files_seen": 50, "files_ingested": 50,
            "artifact_count": 50, "chunk_count": 600, "latency_ms": 3000, "error": None,
        }

    outcome = worker_mod.process_one(
        cfg, _make_claim(),
        **stubs,
        _run_doc_ingest=_fake_doc_ingest,
    )
    assert outcome["status"] == "succeeded"
    # _run_doc_ingest called with correct kwargs.
    assert len(doc_calls) == 1
    assert doc_calls[0]["org_id"] == "org-1"
    assert doc_calls[0]["commit_sha"] == _make_claim()["commit_sha"]
    assert doc_calls[0]["timeout_seconds"] == 60
    # Codex r1 PR #10: source_root (from _checkout_worktree) must be
    # passed as the repo-path for git ops, not core_repo_path.
    assert doc_calls[0]["source_root"] == Path("/fake/worktree")
    assert doc_calls[0]["core_repo_path"] == cfg.core_repo_path
    assert doc_calls[0]["source_root"] != doc_calls[0]["core_repo_path"]


def test_process_one_skips_doc_ingest_when_disabled(tmp_path: Path, monkeypatch):
    """doc_ingest_enabled=False → SCIP runs normally, doc-ingest skipped
    entirely (zero subprocesses)."""
    cfg = _make_cfg(tmp_path, doc_ingest_enabled=False)
    _bootstrap_db(cfg)
    stubs = _scip_stub_kwargs(monkeypatch)

    def _should_not_be_called(**kwargs):
        pytest.fail("_run_doc_ingest must not be invoked when disabled")

    outcome = worker_mod.process_one(
        cfg, _make_claim(),
        **stubs,
        _run_doc_ingest=_should_not_be_called,
    )
    assert outcome["status"] == "succeeded"


def test_process_one_doc_ingest_failure_does_not_fail_commit(tmp_path: Path, monkeypatch, capsys):
    """If doc-ingest returns status=failed/timeout/etc, SCIP success
    stays — overall commit is still succeeded. Warning emitted to stderr."""
    cfg = _make_cfg(tmp_path)
    _bootstrap_db(cfg)
    stubs = _scip_stub_kwargs(monkeypatch)

    outcome = worker_mod.process_one(
        cfg, _make_claim(),
        **stubs,
        _run_doc_ingest=lambda **kw: {
            "status": "failed", "files_seen": None, "files_ingested": None,
            "artifact_count": None, "chunk_count": None, "latency_ms": None,
            "error": "exit=1; stderr='bang'",
        },
    )
    assert outcome["status"] == "succeeded"  # SCIP succeeded
    err = capsys.readouterr().err
    assert "doc ingest status=failed" in err
    assert "SCIP succeeded" in err


def test_process_one_doc_ingest_exception_does_not_raise(tmp_path: Path, monkeypatch, capsys):
    """A truly unhandled exception in _run_doc_ingest still doesn't
    propagate — outer try wraps it."""
    cfg = _make_cfg(tmp_path)
    _bootstrap_db(cfg)
    stubs = _scip_stub_kwargs(monkeypatch)

    def _raise(**kwargs):
        raise RuntimeError("simulated escape")

    outcome = worker_mod.process_one(
        cfg, _make_claim(),
        **stubs,
        _run_doc_ingest=_raise,
    )
    assert outcome["status"] == "succeeded"  # SCIP succeeded
    err = capsys.readouterr().err
    assert "RuntimeError: simulated escape" in err


def test_process_one_partial_doc_ingest_logged_distinctly(tmp_path: Path, monkeypatch, capsys):
    """status='partial' (some artifacts zero-chunked) gets its own
    log line so operators can audit gaps without confusing with
    outright failure."""
    cfg = _make_cfg(tmp_path)
    _bootstrap_db(cfg)
    stubs = _scip_stub_kwargs(monkeypatch)

    outcome = worker_mod.process_one(
        cfg, _make_claim(),
        **stubs,
        _run_doc_ingest=lambda **kw: {
            "status": "partial", "files_seen": 100, "files_ingested": 95,
            "artifact_count": 95, "chunk_count": 1100, "latency_ms": 4500,
            "error": None,
        },
    )
    assert outcome["status"] == "succeeded"
    err = capsys.readouterr().err
    assert "doc ingest partial" in err
    assert "95" in err and "100" in err
