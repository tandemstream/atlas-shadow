"""Shared fixtures for ingest_daemon tests.

Every test uses a tmp DB + tmp state file; no shared mutable state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas_shadow.ingest_daemon import queue as queue_mod
from atlas_shadow.ingest_daemon.config import DEFAULTS, DaemonConfig


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh SQLite DB with schema applied."""
    p = tmp_path / "ingest.db"
    queue_mod.init_db(p)
    return p


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    """A path the daemon may write to; not pre-created."""
    return tmp_path / ".daemon-state.json"


@pytest.fixture
def daemon_config(tmp_path: Path, db_path: Path, state_file: Path) -> DaemonConfig:
    """A DaemonConfig pointing at the tmp dir."""
    return DaemonConfig(
        continuous_shadow_org_id="3ec689a0-678b-47ed-af17-a72e5adbfad8",
        core_repo_path=tmp_path / "core",
        port=DEFAULTS["port"],
        host=DEFAULTS["host"],
        cache_dir=tmp_path / "cache",
        db_path=db_path,
        state_file=state_file,
        scip_indexer_version=DEFAULTS["scip_indexer_version"],
        pack_bundle_revision=DEFAULTS["pack_bundle_revision"],
        core_repo_url=DEFAULTS["core_repo_url"],
        worker_idle_sleep_seconds=DEFAULTS["worker_idle_sleep_seconds"],
        max_attempts_per_commit=DEFAULTS["max_attempts_per_commit"],
        scip_build_timeout_seconds=DEFAULTS["scip_build_timeout_seconds"],
        ingest_shell_out_timeout_seconds=DEFAULTS["ingest_shell_out_timeout_seconds"],
        webhook_secret="test-secret-do-not-use-in-prod",
    )
