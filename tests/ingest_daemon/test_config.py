"""Tests for ``atlas_shadow.ingest_daemon.config.load_config``."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from atlas_shadow.ingest_daemon import config as config_mod


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "shadow-config.yaml"
    with p.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp)
    return p


def test_load_config_applies_defaults(tmp_path):
    p = _write_config(
        tmp_path,
        {
            "continuous_shadow_org_id": "3ec689a0-678b-47ed-af17-a72e5adbfad8",
            "core_repo_path": str(tmp_path / "core"),
        },
    )
    cfg = config_mod.load_config(p)
    assert cfg.continuous_shadow_org_id == "3ec689a0-678b-47ed-af17-a72e5adbfad8"
    assert cfg.port == 8765
    assert cfg.max_attempts_per_commit == 3
    # state_file is resolved against config dir
    assert cfg.state_file == (tmp_path / ".daemon-state.json").resolve()


def test_load_config_respects_section_overrides(tmp_path):
    p = _write_config(
        tmp_path,
        {
            "continuous_shadow_org_id": "abc",
            "core_repo_path": str(tmp_path / "core"),
            "ingest_daemon": {
                "port": 9999,
                "host": "0.0.0.0",
                "max_attempts_per_commit": 7,
                "state_file": "/abs/path/state.json",
            },
        },
    )
    cfg = config_mod.load_config(p)
    assert cfg.port == 9999
    assert cfg.host == "0.0.0.0"
    assert cfg.max_attempts_per_commit == 7
    assert cfg.state_file == Path("/abs/path/state.json")


def test_load_config_requires_continuous_shadow_org_id(tmp_path):
    p = _write_config(tmp_path, {"core_repo_path": str(tmp_path / "core")})
    with pytest.raises(ValueError, match="continuous_shadow_org_id"):
        config_mod.load_config(p)


def test_load_config_requires_core_repo_path(tmp_path):
    p = _write_config(tmp_path, {"continuous_shadow_org_id": "abc"})
    with pytest.raises(ValueError, match="core_repo_path"):
        config_mod.load_config(p)


def test_load_config_reads_webhook_secret_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "super-secret")
    p = _write_config(
        tmp_path,
        {
            "continuous_shadow_org_id": "abc",
            "core_repo_path": str(tmp_path / "core"),
        },
    )
    cfg = config_mod.load_config(p)
    assert cfg.webhook_secret == "super-secret"


def test_load_config_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        config_mod.load_config(tmp_path / "nope.yaml")
