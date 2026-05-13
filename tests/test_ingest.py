"""Tests for atlas_shadow.ingest.

Atlas + workspace shell-out is fully stubbed via subprocess injection."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas_shadow import ingest as ingest_mod


def test_parse_org_id_extracts_uuid():
    text = "→ creating org: foo\n✓ created and active: foo (org_id=af6ef504-7492-4dbb-99cb-9437141bd029)\n"
    assert ingest_mod._parse_org_id(text) == "af6ef504-7492-4dbb-99cb-9437141bd029"


def test_parse_org_id_returns_none_on_no_match():
    assert ingest_mod._parse_org_id("no uuid here") is None


def test_load_cache_returns_empty_when_missing(tmp_path):
    assert ingest_mod.load_cache(tmp_path) == {}


def test_save_and_load_cache_roundtrip(tmp_path):
    cache = {"abc": {"org_id": "0000-1111", "code_revision_id": "2222-3333"}}
    ingest_mod.save_cache(cache, tmp_path)
    loaded = ingest_mod.load_cache(tmp_path)
    assert loaded == cache


def test_load_cache_returns_empty_on_corrupt_json(tmp_path):
    (tmp_path / ingest_mod.CACHE_FILENAME).write_text("not json", encoding="utf-8")
    assert ingest_mod.load_cache(tmp_path) == {}


def test_make_org_name_includes_sha_prefix_and_random_suffix():
    name = ingest_mod._make_org_name("87aa9fa1234567890abcdef")
    assert name.startswith("atlas_shadow_87aa9fa12345")
    # name has random 6-char suffix
    assert len(name.split("_")[-1]) == 6


def test_ensure_org_for_commit_cache_hit_short_circuits(tmp_path):
    cache = {
        "abc1234": {
            "org_id": "OO",
            "code_revision_id": "CR",
            "ingested_at": "2026-05-13T00:00:00+00:00",
            "scip_path": "/tmp/x.scip",
            "source_root": "/tmp/x",
            "latency_ms": 1234,
        }
    }
    ingest_mod.save_cache(cache, tmp_path)

    def _fail_create_org(**kwargs):
        raise AssertionError("create_org should NOT be called on cache hit")

    def _fail_run_ingest(**kwargs):
        raise AssertionError("run_ingest should NOT be called on cache hit")

    result = ingest_mod.ensure_org_for_commit(
        commit_sha="abc1234",
        core_repo_path=tmp_path,
        cwd=tmp_path,
        _create_org=_fail_create_org,
        _run_ingest=_fail_run_ingest,
    )
    assert result["cache_hit"] is True
    assert result["org_id"] == "OO"
    assert result["code_revision_id"] == "CR"
    assert result["commit_sha"] == "abc1234"


def test_ensure_org_for_commit_invokes_create_then_ingest_then_caches(tmp_path):
    create_calls = []
    ingest_calls = []

    def _create_org(*, core_repo_path, name):
        create_calls.append({"core_repo_path": core_repo_path, "name": name})
        return "DEADBEEF-1234"

    def _run_ingest(*, core_repo_path, org_id, scip_path, source_root):
        ingest_calls.append({
            "core_repo_path": core_repo_path,
            "org_id": org_id,
            "scip_path": scip_path,
            "source_root": source_root,
        })
        return {
            "code_revision_id": "CR-9999",
            "counts": {"code_revision_count": 1, "file_count": 10, "symbol_count": 5, "edge_count": 3, "chunk_ref_count": 12},
            "chunk_stats": {"chunks_inserted": 12},
            "org_id": org_id,
            "commit_sha": "fakesha",
        }

    result = ingest_mod.ensure_org_for_commit(
        commit_sha="fakesha000000",
        core_repo_path=Path("/fake/core"),
        cwd=tmp_path,
        _create_org=_create_org,
        _run_ingest=_run_ingest,
    )
    assert len(create_calls) == 1
    assert len(ingest_calls) == 1
    assert ingest_calls[0]["org_id"] == "DEADBEEF-1234"
    # Default scip path follows the dogfood naming convention.
    assert "fakesha00000" in str(ingest_calls[0]["scip_path"])
    assert result["org_id"] == "DEADBEEF-1234"
    assert result["code_revision_id"] == "CR-9999"
    assert result["cache_hit"] is False
    # Cache was written
    cache = ingest_mod.load_cache(tmp_path)
    assert "fakesha000000" in cache
    assert cache["fakesha000000"]["org_id"] == "DEADBEEF-1234"


def test_ensure_org_for_commit_uses_template_org_id_when_provided(tmp_path):
    def _fail_create_org(**kw):
        raise AssertionError("must not create org when template_org_id provided")

    def _run_ingest(*, core_repo_path, org_id, scip_path, source_root):
        return {"code_revision_id": "CR", "counts": {}, "chunk_stats": None}

    result = ingest_mod.ensure_org_for_commit(
        commit_sha="aa",
        core_repo_path=Path("/fake"),
        template_org_id="TEMPLATE-ORG",
        cwd=tmp_path,
        _create_org=_fail_create_org,
        _run_ingest=_run_ingest,
    )
    assert result["org_id"] == "TEMPLATE-ORG"


def test_create_org_parses_org_id_from_org_current(tmp_path):
    """Stub the workspace subprocess pair (org-create + org-current)."""
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if "org-create" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "org-current" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="active org: foo (uuid=af6ef504-7492-4dbb-99cb-9437141bd029)\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown")

    org_id = ingest_mod.create_org(
        core_repo_path=tmp_path, name="atlas_shadow_test_001",
        _subprocess_run=_fake_run,
    )
    assert org_id == "af6ef504-7492-4dbb-99cb-9437141bd029"
    assert any("org-create" in a for a in calls)
    assert any("org-current" in a for a in calls)


def test_create_org_raises_on_org_create_failure(tmp_path):
    def _fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    with pytest.raises(RuntimeError, match="org-create"):
        ingest_mod.create_org(
            core_repo_path=tmp_path, name="x",
            _subprocess_run=_fake_run,
        )


def test_run_dogfood_ingest_script_threads_all_overrides(tmp_path):
    """Wrapper-drift check: ensure --org-id, --scip-path, --source-root all
    reach the dogfood script. Mirrors the script's argparse contract."""
    # Set up a fake atlas leaf with a .venv/bin/python placeholder
    leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (leaf / ".venv" / "bin").mkdir(parents=True)
    venv_py = leaf / ".venv" / "bin" / "python"
    venv_py.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    venv_py.chmod(0o755)

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "org_id": "OO",
                "code_revision_id": "CR",
                "commit_sha": "abc",
                "counts": {},
                "chunk_stats": None,
            }),
            stderr="",
        )

    result = ingest_mod.run_dogfood_ingest_script(
        core_repo_path=tmp_path,
        org_id="OO",
        scip_path=Path("/tmp/x.scip"),
        source_root=Path("/tmp/x"),
        _subprocess_run=_fake_run,
    )
    assert result["code_revision_id"] == "CR"
    argv = captured["argv"]
    assert "--org-id" in argv and argv[argv.index("--org-id") + 1] == "OO"
    assert "--scip-path" in argv
    assert "--source-root" in argv
    assert "scripts.dogfood_v2_smoketest_ingest_code" in argv


def test_run_dogfood_ingest_script_raises_on_missing_venv(tmp_path):
    """If `workspace up` hasn't been run, surface the missing venv early."""
    leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    leaf.mkdir(parents=True)  # no .venv/
    with pytest.raises(FileNotFoundError, match="Atlas venv missing"):
        ingest_mod.run_dogfood_ingest_script(
            core_repo_path=tmp_path,
            org_id="OO",
            scip_path=Path("/tmp/x"),
            source_root=Path("/tmp/y"),
        )


def test_run_dogfood_ingest_script_raises_on_nonzero_return(tmp_path):
    leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (leaf / ".venv" / "bin").mkdir(parents=True)
    (leaf / ".venv" / "bin" / "python").write_text("", encoding="utf-8")

    def _fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="ingest failed")

    with pytest.raises(RuntimeError, match="dogfood_v2_smoketest_ingest_code failed"):
        ingest_mod.run_dogfood_ingest_script(
            core_repo_path=tmp_path,
            org_id="OO",
            scip_path=Path("/tmp/x"),
            source_root=Path("/tmp/y"),
            _subprocess_run=_fake_run,
        )
