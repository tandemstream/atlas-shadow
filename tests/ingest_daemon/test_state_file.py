"""Tests for state_file atomic write/read + runner read-then-fallback."""

from __future__ import annotations

import json
from pathlib import Path

from atlas_shadow import runner as runner_mod
from atlas_shadow.ingest_daemon import state_file as state_mod


def test_write_state_creates_file_with_expected_keys(tmp_path):
    sf = tmp_path / ".daemon-state.json"
    state_mod.write_state(
        state_file_path=sf,
        latest_commit_ingested="A" * 40,
        latest_code_revision_id="11111111-1111-1111-1111-111111111111",
        daemon_pid=99999,
    )
    assert sf.exists()
    data = json.loads(sf.read_text(encoding="utf-8"))
    assert data["latest_commit_ingested"] == "a" * 40  # lowercased
    assert data["latest_code_revision_id"] == "11111111-1111-1111-1111-111111111111"
    assert data["daemon_pid"] == 99999
    assert data["updated_at"].endswith("+00:00")


def test_write_state_atomic_replaces_existing(tmp_path):
    sf = tmp_path / ".daemon-state.json"
    state_mod.write_state(
        state_file_path=sf,
        latest_commit_ingested="a" * 40,
        latest_code_revision_id="rev-A",
    )
    state_mod.write_state(
        state_file_path=sf,
        latest_commit_ingested="b" * 40,
        latest_code_revision_id="rev-B",
    )
    data = json.loads(sf.read_text(encoding="utf-8"))
    assert data["latest_commit_ingested"] == "b" * 40
    assert data["latest_code_revision_id"] == "rev-B"


def test_read_state_returns_none_when_missing(tmp_path):
    assert state_mod.read_state(tmp_path / "missing.json") is None


def test_read_state_returns_none_on_corrupt_json(tmp_path):
    sf = tmp_path / ".daemon-state.json"
    sf.write_text("not json", encoding="utf-8")
    assert state_mod.read_state(sf) is None


def test_runner_resolve_prefers_state_file_when_present(tmp_path):
    """Amendment decision #1: runner reads state file first."""
    cfg_path = tmp_path / "shadow-config.yaml"
    state_path = tmp_path / ".daemon-state.json"
    state_mod.write_state(
        state_file_path=state_path,
        latest_commit_ingested="a" * 40,
        latest_code_revision_id="from-state-file",
    )
    cfg = {
        "continuous_shadow_code_revision_id": "from-config-pinned",
        "ingest_daemon": {"state_file": ".daemon-state.json"},
    }
    resolved = runner_mod.resolve_code_revision_id(cfg, config_path=cfg_path)
    assert resolved == "from-state-file"


def test_runner_resolve_falls_back_to_config_when_state_missing(tmp_path):
    cfg_path = tmp_path / "shadow-config.yaml"
    cfg = {
        "continuous_shadow_code_revision_id": "from-config-pinned",
        "ingest_daemon": {"state_file": ".daemon-state.json"},
    }
    resolved = runner_mod.resolve_code_revision_id(cfg, config_path=cfg_path)
    assert resolved == "from-config-pinned"


def test_runner_resolve_falls_back_to_config_when_state_corrupt(tmp_path):
    cfg_path = tmp_path / "shadow-config.yaml"
    state_path = tmp_path / ".daemon-state.json"
    state_path.write_text("not json", encoding="utf-8")
    cfg = {
        "continuous_shadow_code_revision_id": "from-config-pinned",
        "ingest_daemon": {"state_file": ".daemon-state.json"},
    }
    resolved = runner_mod.resolve_code_revision_id(cfg, config_path=cfg_path)
    assert resolved == "from-config-pinned"


def test_runner_resolve_returns_none_when_both_absent(tmp_path):
    cfg_path = tmp_path / "shadow-config.yaml"
    cfg = {"ingest_daemon": {"state_file": ".daemon-state.json"}}
    resolved = runner_mod.resolve_code_revision_id(cfg, config_path=cfg_path)
    assert resolved is None


def test_runner_resolve_handles_absolute_state_path(tmp_path):
    absolute_state = tmp_path / "elsewhere" / "state.json"
    state_mod.write_state(
        state_file_path=absolute_state,
        latest_commit_ingested="a" * 40,
        latest_code_revision_id="abs-rev",
    )
    cfg = {
        "continuous_shadow_code_revision_id": "fallback",
        "ingest_daemon": {"state_file": str(absolute_state)},
    }
    resolved = runner_mod.resolve_code_revision_id(cfg, config_path=tmp_path / "cfg.yaml")
    assert resolved == "abs-rev"
