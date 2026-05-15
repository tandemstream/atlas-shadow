"""config — load the ``ingest_daemon:`` section of ``shadow-config.yaml``.

The daemon is additive: the existing config keys (``continuous_shadow_org_id``,
``core_repo_path``, etc.) are untouched. The daemon owns one new top-level
key, ``ingest_daemon:``, containing port/cache/indexer settings.

Read order:
    1. Caller passes a config path (typically ``shadow-config.yaml``).
    2. Top-level fields are loaded the same way ``cli.py`` does (``yaml.safe_load``).
    3. The ``ingest_daemon`` sub-mapping is merged over defaults so a missing
       sub-key falls back to a documented default rather than raising.

The ``DaemonConfig`` dataclass is the daemon's "frozen settings" once
loaded; modules never re-read the file at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


DEFAULTS: dict[str, Any] = {
    "port": 8765,
    "cache_dir": "~/.atlas-shadow/cache",
    "db_path": "~/.atlas-shadow/ingest.db",
    "state_file": ".daemon-state.json",
    "scip_indexer_version": "scip-python@0.6.6",
    "pack_bundle_revision": "code@v1",
    "core_repo_url": "https://github.com/tandemstream/core.git",
    # Repo URL passed to the dogfood ingest CLI's --repo-url flag — this
    # becomes part of Atlas's idempotency cache key
    # ``(org_id, repo_url, commit_sha, indexer_version)``. Distinct from
    # ``core_repo_url`` above (which is the .git clone URL); the cache
    # key is a plain string match so the canonical form (no .git suffix)
    # is what downstream Atlas tools expect.
    "repo_url": "https://github.com/tandemstream/core",
    "worker_idle_sleep_seconds": 5,
    "max_attempts_per_commit": 3,
    "scip_build_timeout_seconds": 1200,
    "ingest_shell_out_timeout_seconds": 1800,
    "host": "127.0.0.1",
    # T5 (P2 2026-05-14-atlas-shadow-pre-merge-grading-gate-v1): grader
    # model + principal_id used by the PR-grading orchestrator. Mirror
    # the keys read by `atlas_shadow.cli` (which lives at atlas-shadow
    # repo root and consumes the same ``shadow-config.yaml``). Defaults
    # match the existing CLI fallback chain.
    "grader_model": "sonnet",
    "default_principal_id": None,
    "shadow_runs_dir": "shadow-runs",
}


@dataclass(frozen=True)
class DaemonConfig:
    # Required-from-top-level keys (no daemon defaults).
    continuous_shadow_org_id: str
    core_repo_path: Path

    # Daemon-owned settings (defaults filled from DEFAULTS, overridable via
    # ``ingest_daemon:`` section).
    port: int = DEFAULTS["port"]
    host: str = DEFAULTS["host"]
    cache_dir: Path = field(default_factory=lambda: Path(DEFAULTS["cache_dir"]).expanduser())
    db_path: Path = field(default_factory=lambda: Path(DEFAULTS["db_path"]).expanduser())
    state_file: Path = field(default_factory=lambda: Path(DEFAULTS["state_file"]))
    scip_indexer_version: str = DEFAULTS["scip_indexer_version"]
    pack_bundle_revision: str = DEFAULTS["pack_bundle_revision"]
    core_repo_url: str = DEFAULTS["core_repo_url"]
    repo_url: str = DEFAULTS["repo_url"]
    worker_idle_sleep_seconds: int = DEFAULTS["worker_idle_sleep_seconds"]
    max_attempts_per_commit: int = DEFAULTS["max_attempts_per_commit"]
    scip_build_timeout_seconds: int = DEFAULTS["scip_build_timeout_seconds"]
    ingest_shell_out_timeout_seconds: int = DEFAULTS["ingest_shell_out_timeout_seconds"]
    webhook_secret: Optional[str] = None
    # T5 (P2): PR-grading config. Read from the top-level YAML (mirroring
    # ``atlas_shadow.cli``), NOT from ``ingest_daemon:`` section, because
    # these settings are shared with the offline grader.
    grader_model: str = DEFAULTS["grader_model"]
    default_principal_id: Optional[str] = DEFAULTS["default_principal_id"]
    shadow_runs_dir: Path = field(default_factory=lambda: Path(DEFAULTS["shadow_runs_dir"]))


def load_config(
    config_path: Path | str,
    *,
    state_file_dir: Optional[Path] = None,
) -> DaemonConfig:
    """Load ``shadow-config.yaml`` and produce a :class:`DaemonConfig`.

    The state file path is *resolved* against ``state_file_dir`` (defaults
    to the parent dir of ``config_path``) so it sits next to the config
    file by default — matching amendment #1's "``<atlas-shadow>/.daemon-state.json``"
    spec.

    ``webhook_secret`` is read from the ``GITHUB_WEBHOOK_SECRET`` env var
    (per proposal r1) — NOT from YAML. The receiver refuses to start
    without it unless the daemon is invoked in replay-only mode.
    """
    cfg_path = Path(config_path).expanduser()
    if not cfg_path.exists():
        raise FileNotFoundError(f"shadow-config.yaml not found at {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    org_id = raw.get("continuous_shadow_org_id")
    if not org_id:
        raise ValueError(
            "shadow-config.yaml: continuous_shadow_org_id is required for "
            "the ingest daemon (it's the target Atlas org)."
        )
    core_repo_path = raw.get("core_repo_path")
    if not core_repo_path:
        raise ValueError(
            "shadow-config.yaml: core_repo_path is required (the dogfood "
            "ingest script + Atlas venv live under this path)."
        )

    section = raw.get("ingest_daemon") or {}
    merged = {**DEFAULTS, **section}

    base_dir = state_file_dir or cfg_path.parent
    state_file_raw = merged["state_file"]
    state_file = Path(state_file_raw)
    if not state_file.is_absolute():
        state_file = (base_dir / state_file).resolve()

    # T5 (P2): grader_model + default_principal_id live at the top-level
    # YAML alongside `continuous_shadow_org_id`. `shadow_runs_dir` is
    # daemon-owned (under `ingest_daemon:` section) but defaults to the
    # repo-rooted `shadow-runs/` directory.
    grader_model = str(raw.get("grader_model") or DEFAULTS["grader_model"])
    default_principal_id = raw.get("default_principal_id")
    if default_principal_id is not None:
        default_principal_id = str(default_principal_id)
    shadow_runs_dir_raw = merged.get("shadow_runs_dir") or DEFAULTS["shadow_runs_dir"]
    shadow_runs_dir = Path(str(shadow_runs_dir_raw)).expanduser()
    if not shadow_runs_dir.is_absolute():
        shadow_runs_dir = (cfg_path.parent / shadow_runs_dir).resolve()

    return DaemonConfig(
        continuous_shadow_org_id=str(org_id),
        core_repo_path=Path(core_repo_path).expanduser(),
        port=int(merged["port"]),
        host=str(merged["host"]),
        cache_dir=Path(str(merged["cache_dir"])).expanduser(),
        db_path=Path(str(merged["db_path"])).expanduser(),
        state_file=state_file,
        scip_indexer_version=str(merged["scip_indexer_version"]),
        pack_bundle_revision=str(merged["pack_bundle_revision"]),
        core_repo_url=str(merged["core_repo_url"]),
        repo_url=str(merged["repo_url"]),
        worker_idle_sleep_seconds=int(merged["worker_idle_sleep_seconds"]),
        max_attempts_per_commit=int(merged["max_attempts_per_commit"]),
        scip_build_timeout_seconds=int(merged["scip_build_timeout_seconds"]),
        ingest_shell_out_timeout_seconds=int(merged["ingest_shell_out_timeout_seconds"]),
        webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"),
        grader_model=grader_model,
        default_principal_id=default_principal_id,
        shadow_runs_dir=shadow_runs_dir,
    )
