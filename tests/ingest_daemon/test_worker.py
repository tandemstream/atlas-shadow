"""T-D4: worker integration test with stubbed cache + scip + ingest CLI.

Critical assertion (amendment decision #10): the worker's invocation of
the dogfood ingest CLI uses argv that mirrors the dogfood script's
argparse declaration. We verify this by parsing the dogfood script's
argparse declaration as part of the test — if the script's CLI surface
ever changes, this test fails loudly.

The dogfood script lives in `tandemstream/core` at:

    products/tandem/packages/python/atlas/scripts/dogfood_v2_smoketest_ingest_code.py

We look up the user's `core_repo_path` from `shadow-config.yaml` and
parse the file at that path. If `core_repo_path` isn't set or the file
isn't present (e.g., on a CI host without the core checkout), we skip
the parity check with a clear pytest.skip — the rest of the worker
contract is still tested.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from atlas_shadow.ingest_daemon import scip_builder as scip_mod
from atlas_shadow.ingest_daemon import state_file as state_mod
from atlas_shadow.ingest_daemon import worker as worker_mod


def _make_claim(sha: str = "abc" + "0" * 37) -> dict:
    return {
        "queue_id": 1,
        "commit_sha": sha,
        "attempt_number": 1,
        "started_at": "2026-05-14T00:00:00+00:00",
    }


def test_process_one_succeeded_writes_ledger_and_state(daemon_config, db_path, state_file):
    from atlas_shadow.ingest_daemon import ledger as ledger_mod
    from atlas_shadow.ingest_daemon import queue as queue_mod

    # Pre-state: enqueue a row so mark_terminal has something to update.
    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    captured: dict[str, Any] = {}

    def fake_ensure_clone(*, cache_dir, core_repo_url):
        captured["cache_dir"] = cache_dir
        captured["core_repo_url"] = core_repo_url
        return cache_dir / "core"

    def fake_checkout(*, cache_dir, commit_sha, core_clone):
        captured["commit_sha"] = commit_sha
        return cache_dir / "worktrees" / commit_sha[:12]

    def fake_build_scip(*, source_root, commit_sha, cache_dir, indexer_version, timeout_seconds):
        scip = cache_dir / "scip" / f"core-{commit_sha}.scip"
        scip.parent.mkdir(parents=True, exist_ok=True)
        scip.write_bytes(b"fake-scip")
        captured["indexer_version"] = indexer_version
        return scip

    def fake_run_ingest(
        *,
        core_repo_path,
        org_id,
        scip_path,
        source_root,
        commit_sha,
        repo_url,
        timeout_seconds,
        parent_code_revision_id=None,
    ):
        captured["ingest_org_id"] = org_id
        captured["ingest_scip_path"] = scip_path
        captured["ingest_source_root"] = source_root
        captured["ingest_commit_sha"] = commit_sha
        captured["ingest_repo_url"] = repo_url
        captured["ingest_parent_code_revision_id"] = parent_code_revision_id
        return {
            "org_id": org_id,
            "code_revision_id": "11111111-1111-1111-1111-111111111111",
            "chunk_stats": {"total_chunks": 42},
            "counts": {"file_count": 7, "symbol_count": 99},
            "latency_ms": 5000,
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
    assert outcome["code_revision_id"] == "11111111-1111-1111-1111-111111111111"
    assert captured["ingest_org_id"] == daemon_config.continuous_shadow_org_id
    # D5 v2 follow-on: worker must thread the queue-row sha + the
    # configured repo_url into _run_ingest so the dogfood CLI can drive
    # Atlas's idempotency cache key on the real values (not the dogfood
    # pin).
    assert captured["ingest_commit_sha"] == claim["commit_sha"]
    assert captured["ingest_repo_url"] == daemon_config.repo_url
    # P1 T2 (packet 2026-05-14-atlas-shadow-substrate-enablers-v1):
    # cold start — no state file exists at fixture setup, so
    # parent_code_revision_id is None and the worker passes that
    # through to _run_ingest. Verified separately in
    # test_process_one_threads_parent_code_revision_id_when_state_exists.
    assert captured["ingest_parent_code_revision_id"] is None

    # Ledger row written.
    latest = ledger_mod.latest_succeeded(db_path)
    assert latest is not None
    assert latest["code_revision_id"] == "11111111-1111-1111-1111-111111111111"
    assert latest["chunker_stats_total"] == 42

    # State file written atomically.
    state = state_mod.read_state(state_file)
    assert state is not None
    assert state["latest_code_revision_id"] == "11111111-1111-1111-1111-111111111111"


def test_process_one_failure_marks_failed_and_no_state_write(daemon_config, db_path, state_file):
    from atlas_shadow.ingest_daemon import ledger as ledger_mod
    from atlas_shadow.ingest_daemon import queue as queue_mod

    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    def fake_ensure_clone(**kwargs):
        return Path("/tmp/dummy")

    def fake_checkout(**kwargs):
        return Path("/tmp/dummy/wt")

    def fake_build_scip(**kwargs):
        raise RuntimeError("scip-python crashed")

    def fake_run_ingest(**kwargs):
        raise AssertionError("run_ingest should NOT be called when scip build fails")

    outcome = worker_mod.process_one(
        daemon_config,
        claim,
        _build_scip=fake_build_scip,
        _run_ingest=fake_run_ingest,
        _ensure_clone=fake_ensure_clone,
        _checkout_worktree=fake_checkout,
    )
    assert outcome["status"] == "failed"
    assert "scip-python crashed" in outcome["error"]
    assert state_mod.read_state(state_file) is None  # no state write on failure
    latest = ledger_mod.latest_attempt(db_path)
    assert latest["status"] == "failed"
    assert "scip-python crashed" in latest["error_message"]


def test_process_one_state_write_failure_does_not_retry(daemon_config, db_path, state_file, monkeypatch):
    """Amendment decision #12: if state write fails after a successful ingest,
    log+continue. Atlas has the data; bookkeeping is what's stale."""
    from atlas_shadow.ingest_daemon import ledger as ledger_mod
    from atlas_shadow.ingest_daemon import queue as queue_mod

    queue_mod.enqueue(db_path, _make_claim()["commit_sha"])
    claim = queue_mod.claim_next(db_path)

    def fake_ensure_clone(**kwargs):
        return Path("/tmp/dummy")

    def fake_checkout(**kwargs):
        return Path("/tmp/dummy/wt")

    def fake_build_scip(*, cache_dir, commit_sha, **kwargs):
        scip = cache_dir / "scip" / f"core-{commit_sha}.scip"
        scip.parent.mkdir(parents=True, exist_ok=True)
        scip.write_bytes(b"x")
        return scip

    def fake_run_ingest(**kwargs):
        return {"code_revision_id": "rev-X", "chunk_stats": {}, "counts": {}}

    def boom_write_state(**kwargs):
        raise OSError("disk full")

    outcome = worker_mod.process_one(
        daemon_config,
        claim,
        _build_scip=fake_build_scip,
        _run_ingest=fake_run_ingest,
        _ensure_clone=fake_ensure_clone,
        _checkout_worktree=fake_checkout,
        _write_state=boom_write_state,
    )
    # Outcome is still succeeded; the ingest itself worked.
    assert outcome["status"] == "succeeded"
    # Ledger has a succeeded row.
    latest = ledger_mod.latest_succeeded(db_path)
    assert latest["code_revision_id"] == "rev-X"
    # State file remains unwritten.
    assert state_mod.read_state(state_file) is None


def test_dogfood_ingest_argv_shape_is_stable(tmp_path):
    """Argv shape doesn't drift from the documented contract.

    This catches anyone editing scip_builder.py to add/remove flags
    without updating the argv-parity test (and the runbook).
    """
    core_repo_path = tmp_path / "core"
    argv = scip_mod.dogfood_ingest_argv(
        core_repo_path=core_repo_path,
        org_id="3ec689a0-678b-47ed-af17-a72e5adbfad8",
        scip_path=Path("/tmp/x.scip"),
        source_root=Path("/tmp/x"),
        commit_sha="deadbeef" + "0" * 32,
        repo_url="https://github.com/tandemstream/core",
    )
    # Position-independent assertions on the argv shape.
    assert argv[1] == "-m"
    assert argv[2] == "scripts.dogfood_v2_smoketest_ingest_code"
    # Required flags are present + paired.
    pairs = dict(zip(argv[3::2], argv[4::2]))
    assert pairs["--org-id"] == "3ec689a0-678b-47ed-af17-a72e5adbfad8"
    assert pairs["--scip-path"] == "/tmp/x.scip"
    assert pairs["--source-root"] == "/tmp/x"
    # D5 v2 follow-on: --commit-sha + --repo-url must be passed through
    # (core PR #209 added these flags to the dogfood CLI).
    assert pairs["--commit-sha"] == "deadbeef" + "0" * 32
    assert pairs["--repo-url"] == "https://github.com/tandemstream/core"
    # P1 T2 (packet 2026-05-14-atlas-shadow-substrate-enablers-v1):
    # cold-start argv (parent_code_revision_id is None / unset) must NOT
    # include the incremental flags. The daemon's first ingest after a
    # state-file reset relies on this to fall back to full ingest
    # cleanly.
    assert "--incremental" not in argv, (
        "Cold-start argv (no parent_code_revision_id) must not include "
        "--incremental — the dogfood CLI's default is full ingest."
    )
    assert "--parent-code-revision-id" not in argv


def test_dogfood_ingest_argv_with_parent_revision_appends_incremental_flags(tmp_path):
    """P1 T2 (packet 2026-05-14-atlas-shadow-substrate-enablers-v1) —
    when `parent_code_revision_id` is non-None the daemon argv appends
    ``--incremental --parent-code-revision-id <uuid>`` so the dogfood
    CLI dispatches to file_memoization.ingest_with_carry_forward
    [qa:q4]."""
    core_repo_path = tmp_path / "core"
    parent_uuid = "11111111-2222-3333-4444-555555555555"
    argv = scip_mod.dogfood_ingest_argv(
        core_repo_path=core_repo_path,
        org_id="3ec689a0-678b-47ed-af17-a72e5adbfad8",
        scip_path=Path("/tmp/x.scip"),
        source_root=Path("/tmp/x"),
        commit_sha="deadbeef" + "0" * 32,
        repo_url="https://github.com/tandemstream/core",
        parent_code_revision_id=parent_uuid,
    )

    assert "--incremental" in argv
    # --parent-code-revision-id <uuid> must be paired correctly.
    flag_idx = argv.index("--parent-code-revision-id")
    assert argv[flag_idx + 1] == parent_uuid

    # The original v1 + v2 flags must still be present (paired).
    pairs = dict(zip(argv[3::2], argv[4::2]))
    assert pairs["--org-id"] == "3ec689a0-678b-47ed-af17-a72e5adbfad8"
    assert pairs["--scip-path"] == "/tmp/x.scip"
    assert pairs["--source-root"] == "/tmp/x"
    assert pairs["--commit-sha"] == "deadbeef" + "0" * 32
    assert pairs["--repo-url"] == "https://github.com/tandemstream/core"


def _parse_dogfood_argparse_flags(script_path: Path) -> set[str]:
    """Parse the dogfood script's argparse declaration and return the set
    of declared CLI flags (e.g., ``{"--org-id", "--scip-path", "--source-root"}``).

    We use the AST to avoid importing the script (it would need its venv).
    """
    src = script_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    flags: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Looking for calls like parser.add_argument("--scip-path", ...)
        if isinstance(func, ast.Attribute) and func.attr == "add_argument":
            if node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str) and val.startswith("--"):
                    flags.add(val)
    return flags


def test_argv_matches_dogfood_argparse():
    """Amendment decision #10: assert argv parity against the dogfood
    script's argparse declaration. Skips when ``shadow-config.yaml`` is
    missing or its ``core_repo_path`` doesn't resolve.
    """
    cfg_path = Path("shadow-config.yaml")
    if not cfg_path.exists():
        pytest.skip("shadow-config.yaml not present in cwd")
    with cfg_path.open("r", encoding="utf-8") as fp:
        cfg = yaml.safe_load(fp) or {}
    core_repo_path = cfg.get("core_repo_path")
    if not core_repo_path:
        pytest.skip("shadow-config.yaml has no core_repo_path")
    script_path = (
        Path(core_repo_path).expanduser()
        / "products"
        / "tandem"
        / "packages"
        / "python"
        / "atlas"
        / "scripts"
        / "dogfood_v2_smoketest_ingest_code.py"
    )
    if not script_path.exists():
        pytest.skip(f"dogfood ingest script not found at {script_path}")
    declared_flags = _parse_dogfood_argparse_flags(script_path)

    # The flags we PASS to the script — extracted from our own argv shape.
    argv = scip_mod.dogfood_ingest_argv(
        core_repo_path=Path(core_repo_path).expanduser(),
        org_id="some-uuid",
        scip_path=Path("/tmp/x.scip"),
        source_root=Path("/tmp/x"),
        commit_sha="deadbeef" + "0" * 32,
        repo_url="https://github.com/tandemstream/core",
    )
    used_flags = {tok for tok in argv if isinstance(tok, str) and tok.startswith("--")}

    # Every flag we use MUST be declared by the script.
    missing = used_flags - declared_flags
    assert not missing, (
        f"Worker passes flags the dogfood script does NOT declare: {missing}. "
        f"Dogfood script declares: {sorted(declared_flags)}; "
        f"worker uses: {sorted(used_flags)}. "
        f"Update atlas_shadow/ingest_daemon/scip_builder.py::dogfood_ingest_argv "
        f"to match the dogfood script's argparse (and the runbook)."
    )

    # D5 v2 follow-on (core PR #209): the dogfood script MUST declare
    # --commit-sha + --repo-url, and the daemon MUST pass them. The
    # parity assertion is bidirectional — both sides must agree.
    v2_flags = {"--commit-sha", "--repo-url"}
    assert v2_flags <= declared_flags, (
        f"Dogfood script is missing v2 flags {v2_flags - declared_flags}. "
        f"Make sure tandemstream/core is at or past PR #209 "
        f"(merge commit c713448) — the daemon depends on these flags "
        f"to drive Atlas's idempotency cache key per real core SHA."
    )
    assert v2_flags <= used_flags, (
        f"Daemon argv is missing v2 flags {v2_flags - used_flags}. "
        f"Update atlas_shadow/ingest_daemon/scip_builder.py::dogfood_ingest_argv."
    )

    # P1 T2 follow-on (packet 2026-05-14-atlas-shadow-substrate-enablers-v1):
    # the dogfood script MUST declare --incremental + --parent-code-revision-id,
    # and the daemon MUST pass them when a parent UUID is supplied. The
    # parity assertion is bidirectional — both sides must agree.
    p1_flags = {"--incremental", "--parent-code-revision-id"}
    assert p1_flags <= declared_flags, (
        f"Dogfood script is missing P1 T2 flags {p1_flags - declared_flags}. "
        f"Make sure tandemstream/core is at or past the merged "
        f"P1 packet — the daemon depends on these flags to engage "
        f"file_memoization.ingest_with_carry_forward [qa:q4]."
    )
    # The cold-start argv above does NOT include the P1 flags (correct
    # — they only appear when parent_code_revision_id is supplied). To
    # exercise the parent-supplied path against the same dogfood
    # argparse, build a second argv with a parent UUID and assert it.
    argv_with_parent = scip_mod.dogfood_ingest_argv(
        core_repo_path=Path(core_repo_path).expanduser(),
        org_id="some-uuid",
        scip_path=Path("/tmp/x.scip"),
        source_root=Path("/tmp/x"),
        commit_sha="deadbeef" + "0" * 32,
        repo_url="https://github.com/tandemstream/core",
        parent_code_revision_id="11111111-2222-3333-4444-555555555555",
    )
    used_flags_with_parent = {
        tok for tok in argv_with_parent if isinstance(tok, str) and tok.startswith("--")
    }
    assert p1_flags <= used_flags_with_parent, (
        f"Daemon argv with parent_code_revision_id supplied is missing "
        f"P1 T2 flags {p1_flags - used_flags_with_parent}. "
        f"Update atlas_shadow/ingest_daemon/scip_builder.py::dogfood_ingest_argv."
    )
    # And the parent-supplied argv must STILL be a subset of declared
    # flags (no daemon-side stray flags the dogfood CLI wouldn't accept).
    missing_with_parent = used_flags_with_parent - declared_flags
    assert not missing_with_parent, (
        f"Worker passes P1 flags the dogfood script does NOT declare: "
        f"{missing_with_parent}."
    )


def test_drain_once_returns_none_on_empty_queue(daemon_config, db_path):
    outcome = worker_mod.drain_once(daemon_config)
    assert outcome is None
