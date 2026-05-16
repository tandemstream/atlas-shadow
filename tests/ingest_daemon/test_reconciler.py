"""Tests for ``atlas_shadow.ingest_daemon.reconciler``.

The reconciler is the safety net for a dead ``gh webhook forward``: it
polls ``origin/main`` on a timer and enqueues the head SHA when the
daemon's state file is behind. These tests stub the
``git ls-remote`` call so they run offline.
"""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from atlas_shadow.ingest_daemon import queue as queue_mod
from atlas_shadow.ingest_daemon import reconciler as reconciler_mod
from atlas_shadow.ingest_daemon import state_file as state_mod


_REMOTE_SHA = "abcdef0123456789" + "0" * 24
_LOCAL_SHA = "111111110000000" + "f" * 25


def _ls_remote_stdout(sha: str) -> str:
    return f"{sha}\trefs/heads/main\n"


def _fake_subprocess_run(stdout: str = "", returncode: int = 0):
    """Build a callable usable as ``_subprocess_run`` for ls-remote tests."""

    def _run(cmd, **kwargs):  # noqa: ANN001
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    return _run


# ---------------------------------------------------------------------------
# _parse_ls_remote_sha
# ---------------------------------------------------------------------------


def test_parse_ls_remote_sha_returns_main_sha():
    out = _ls_remote_stdout(_REMOTE_SHA)
    assert reconciler_mod._parse_ls_remote_sha(out) == _REMOTE_SHA


def test_parse_ls_remote_sha_ignores_unrelated_refs():
    # Mixed output: tags, branches other than main, then main last.
    out = (
        f"{_LOCAL_SHA}\trefs/tags/v1\n"
        f"deadbeef{'0' * 32}\trefs/heads/other\n"
        f"{_REMOTE_SHA}\trefs/heads/main\n"
    )
    assert reconciler_mod._parse_ls_remote_sha(out) == _REMOTE_SHA


def test_parse_ls_remote_sha_returns_none_when_empty():
    assert reconciler_mod._parse_ls_remote_sha("") is None
    assert reconciler_mod._parse_ls_remote_sha("\n\n") is None


def test_parse_ls_remote_sha_returns_none_when_main_missing():
    out = f"{_LOCAL_SHA}\trefs/heads/develop\n"
    assert reconciler_mod._parse_ls_remote_sha(out) is None


def test_parse_ls_remote_sha_rejects_non_sha_token():
    out = "not-a-sha\trefs/heads/main\n"
    assert reconciler_mod._parse_ls_remote_sha(out) is None


# ---------------------------------------------------------------------------
# fetch_remote_head_sha
# ---------------------------------------------------------------------------


def test_fetch_remote_head_sha_returns_parsed_sha(daemon_config):
    sha = reconciler_mod.fetch_remote_head_sha(
        daemon_config,
        _subprocess_run=_fake_subprocess_run(stdout=_ls_remote_stdout(_REMOTE_SHA)),
    )
    assert sha == _REMOTE_SHA


def test_fetch_remote_head_sha_returns_none_on_nonzero_exit(daemon_config):
    sha = reconciler_mod.fetch_remote_head_sha(
        daemon_config,
        _subprocess_run=_fake_subprocess_run(stdout="", returncode=128),
    )
    assert sha is None


def test_fetch_remote_head_sha_returns_none_on_timeout(daemon_config):
    def _raises(cmd, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

    sha = reconciler_mod.fetch_remote_head_sha(
        daemon_config, _subprocess_run=_raises
    )
    assert sha is None


def test_fetch_remote_head_sha_returns_none_on_oserror(daemon_config):
    def _raises(cmd, **kwargs):  # noqa: ANN001
        raise OSError("git binary not found")

    sha = reconciler_mod.fetch_remote_head_sha(
        daemon_config, _subprocess_run=_raises
    )
    assert sha is None


def test_fetch_remote_head_sha_uses_configured_repo_url(daemon_config):
    captured: dict[str, Any] = {}

    def _capture(cmd, **kwargs):  # noqa: ANN001
        captured["cmd"] = list(cmd)
        captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(
            stdout=_ls_remote_stdout(_REMOTE_SHA), stderr="", returncode=0
        )

    reconciler_mod.fetch_remote_head_sha(daemon_config, _subprocess_run=_capture)
    assert captured["cmd"][:2] == ["git", "ls-remote"]
    assert captured["cmd"][2] == daemon_config.core_repo_url
    assert captured["cmd"][3] == "refs/heads/main"
    # Honors the configured timeout (default 60s).
    assert captured["timeout"] == daemon_config.reconciler_ls_remote_timeout_seconds


# ---------------------------------------------------------------------------
# reconcile_once
# ---------------------------------------------------------------------------


def test_reconcile_once_in_sync(daemon_config, state_file):
    # State already at remote SHA.
    state_mod.write_state(
        state_file_path=state_file,
        latest_commit_ingested=_REMOTE_SHA,
        latest_code_revision_id="rev-1",
    )

    def _fetch(cfg):
        return _REMOTE_SHA

    result = reconciler_mod.reconcile_once(daemon_config, _fetch_remote=_fetch)
    assert result["outcome"] == "in-sync"
    assert result["remote_sha"] == _REMOTE_SHA
    assert result["local_sha"] == _REMOTE_SHA
    assert result["queue_id"] is None
    # No row should have been enqueued.
    assert queue_mod.queue_depth(daemon_config.db_path) == 0


def test_reconcile_once_enqueues_when_local_is_behind(daemon_config, state_file):
    state_mod.write_state(
        state_file_path=state_file,
        latest_commit_ingested=_LOCAL_SHA,
        latest_code_revision_id="rev-1",
    )

    def _fetch(cfg):
        return _REMOTE_SHA

    result = reconciler_mod.reconcile_once(daemon_config, _fetch_remote=_fetch)
    assert result["outcome"] == "enqueued"
    assert result["remote_sha"] == _REMOTE_SHA
    assert result["local_sha"] == _LOCAL_SHA
    assert result["queue_id"] is not None
    # Exactly one queued row, sourced from "reconciler".
    assert queue_mod.queue_depth(daemon_config.db_path) == 1
    import sqlite3

    with sqlite3.connect(str(daemon_config.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT commit_sha, source, status FROM ingest_queue"
        ).fetchone()
    assert row["commit_sha"] == _REMOTE_SHA
    assert row["source"] == "reconciler"
    assert row["status"] == "queued"


def test_reconcile_once_enqueues_when_state_file_missing(daemon_config, state_file):
    # state_file does not exist on disk.
    assert not state_file.exists()

    def _fetch(cfg):
        return _REMOTE_SHA

    result = reconciler_mod.reconcile_once(daemon_config, _fetch_remote=_fetch)
    assert result["outcome"] == "enqueued"
    assert result["local_sha"] is None
    assert queue_mod.queue_depth(daemon_config.db_path) == 1


def test_reconcile_once_no_remote_when_ls_remote_fails(daemon_config, state_file):
    state_mod.write_state(
        state_file_path=state_file,
        latest_commit_ingested=_LOCAL_SHA,
        latest_code_revision_id="rev-1",
    )

    def _fetch(cfg):
        return None

    result = reconciler_mod.reconcile_once(daemon_config, _fetch_remote=_fetch)
    assert result["outcome"] == "no-remote"
    assert result["remote_sha"] is None
    # Nothing enqueued — we have no idea what the remote head is.
    assert queue_mod.queue_depth(daemon_config.db_path) == 0


def test_reconcile_once_dedups_when_remote_already_succeeded(daemon_config, state_file):
    # Simulate the race: webhook delivered the SHA, worker already
    # succeeded, but the state file write hasn't landed yet (or we read
    # state before the worker fsynced). The reconciler must NOT
    # re-enqueue the same SHA.
    queue_mod.enqueue(daemon_config.db_path, _REMOTE_SHA, source="webhook")
    claim = queue_mod.claim_next(daemon_config.db_path)
    queue_mod.mark_terminal(daemon_config.db_path, claim["queue_id"], status="succeeded")
    # State file still points at the *old* SHA (the write race).
    state_mod.write_state(
        state_file_path=state_file,
        latest_commit_ingested=_LOCAL_SHA,
        latest_code_revision_id="rev-1",
    )

    def _fetch(cfg):
        return _REMOTE_SHA

    result = reconciler_mod.reconcile_once(daemon_config, _fetch_remote=_fetch)
    assert result["outcome"] == "skipped-dedup-succeeded"
    # Still only the one (now-succeeded) row; reconciler did not
    # re-queue.
    assert queue_mod.queue_depth(daemon_config.db_path) == 0


def test_reconcile_once_skips_when_already_queued(daemon_config, state_file):
    queue_mod.enqueue(daemon_config.db_path, _REMOTE_SHA, source="webhook")
    state_mod.write_state(
        state_file_path=state_file,
        latest_commit_ingested=_LOCAL_SHA,
        latest_code_revision_id="rev-1",
    )

    def _fetch(cfg):
        return _REMOTE_SHA

    result = reconciler_mod.reconcile_once(daemon_config, _fetch_remote=_fetch)
    assert result["outcome"] == "skipped-already-queued"
    # No duplicate row.
    assert queue_mod.queue_depth(daemon_config.db_path) == 1


def test_reconcile_once_skips_when_running(daemon_config, state_file):
    queue_mod.enqueue(daemon_config.db_path, _REMOTE_SHA, source="webhook")
    queue_mod.claim_next(daemon_config.db_path)  # mark running
    state_mod.write_state(
        state_file_path=state_file,
        latest_commit_ingested=_LOCAL_SHA,
        latest_code_revision_id="rev-1",
    )

    def _fetch(cfg):
        return _REMOTE_SHA

    result = reconciler_mod.reconcile_once(daemon_config, _fetch_remote=_fetch)
    assert result["outcome"] == "skipped-already-running"


def test_reconcile_once_swallows_enqueue_error(daemon_config, state_file):
    state_mod.write_state(
        state_file_path=state_file,
        latest_commit_ingested=_LOCAL_SHA,
        latest_code_revision_id="rev-1",
    )

    def _fetch(cfg):
        return _REMOTE_SHA

    def _boom_enqueue(*args, **kwargs):
        raise RuntimeError("db locked")

    result = reconciler_mod.reconcile_once(
        daemon_config, _fetch_remote=_fetch, _enqueue=_boom_enqueue
    )
    assert result["outcome"] == "enqueue-error"
    assert "RuntimeError" in result["error"]
    assert "db locked" in result["error"]


# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------


def test_run_loop_exits_on_stop_event(daemon_config):
    """When ``stop_event`` is already set, ``run_loop`` returns immediately
    without ticking."""
    stop = threading.Event()
    stop.set()
    calls: list[dict[str, Any]] = []

    def _tick(cfg):
        calls.append({})
        return {"outcome": "in-sync"}

    reconciler_mod.run_loop(daemon_config, stop, _reconcile_once=_tick)
    assert calls == []


def test_run_loop_runs_one_tick_then_stops(daemon_config):
    """Single tick then stop_event is set during the sleep."""
    stop = threading.Event()
    ticks: list[None] = []

    def _tick(cfg):
        ticks.append(None)
        return {"outcome": "in-sync"}

    # First call to _sleep returns True (simulating "stop_event set
    # during wait"), so run_loop should return after exactly one tick.
    sleep_calls: list[int] = []

    def _sleep(*, timeout: int) -> bool:
        sleep_calls.append(timeout)
        return True

    reconciler_mod.run_loop(
        daemon_config, stop, _reconcile_once=_tick, _sleep=_sleep
    )
    assert len(ticks) == 1
    assert sleep_calls == [daemon_config.reconciler_interval_seconds]


def test_run_loop_swallows_tick_exception(daemon_config, capsys):
    """If a tick raises, the loop logs and continues to the next tick."""
    stop = threading.Event()
    call_counter = {"n": 0}

    def _tick(cfg):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            raise RuntimeError("transient blow-up")
        return {"outcome": "in-sync"}

    def _sleep(*, timeout: int) -> bool:
        return call_counter["n"] >= 2  # stop after the second tick

    reconciler_mod.run_loop(
        daemon_config, stop, _reconcile_once=_tick, _sleep=_sleep
    )
    assert call_counter["n"] == 2
    err = capsys.readouterr().err
    assert "reconciler tick raised RuntimeError" in err
