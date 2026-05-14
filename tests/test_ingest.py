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


# ---------------------------------------------------------------------------
# Rollback / orphan-org prevention (PR follow-up after 2.A smoke testing
# observed a real orphan-org leak: a failed ingest left an `orgs` row
# behind because create_org → run_ingest had no try/except).
# ---------------------------------------------------------------------------


def test_ensure_org_for_commit_rolls_back_fresh_org_on_ingest_failure(tmp_path):
    """When run_ingest raises AND we created the org (no template), we
    MUST attempt to delete the fresh org via _delete_org. The original
    ingest exception must still propagate (rollback never masks it)."""
    create_calls = []
    delete_calls = []

    def _create_org(*, core_repo_path, name):
        create_calls.append(name)
        return "FRESH-ORG-UUID"

    def _run_ingest(**kw):
        raise RuntimeError("simulated ingest failure (missing SCIP file)")

    def _delete_org(*, core_repo_path, org_id):
        delete_calls.append(org_id)
        return {"deleted": True, "name": "atlas_shadow_xxx", "org_id": org_id}

    with pytest.raises(RuntimeError, match="simulated ingest failure"):
        ingest_mod.ensure_org_for_commit(
            commit_sha="failsha000000",
            core_repo_path=Path("/fake/core"),
            cwd=tmp_path,
            _create_org=_create_org,
            _run_ingest=_run_ingest,
            _delete_org=_delete_org,
        )

    # Created exactly one org, deleted exactly one org, same UUID:
    assert create_calls == ["atlas_shadow_failsha00000_" + create_calls[0].split("_")[-1]]
    assert delete_calls == ["FRESH-ORG-UUID"]
    # No cache record written (the ingest failed):
    assert ingest_mod.load_cache(tmp_path) == {}


def test_ensure_org_for_commit_skips_rollback_when_template_org_used(tmp_path):
    """If template_org_id was passed (we did NOT create the org), a
    failed ingest must NOT trigger _delete_org — we don't own that org."""
    delete_calls = []

    def _run_ingest(**kw):
        raise RuntimeError("ingest failed")

    def _delete_org(*, core_repo_path, org_id):
        delete_calls.append(org_id)
        return {"deleted": True}

    with pytest.raises(RuntimeError, match="ingest failed"):
        ingest_mod.ensure_org_for_commit(
            commit_sha="templ000000",
            core_repo_path=Path("/fake/core"),
            template_org_id="USER-PROVIDED-ORG",
            cwd=tmp_path,
            _run_ingest=_run_ingest,
            _delete_org=_delete_org,
        )

    # Did NOT call delete on the user-provided template org:
    assert delete_calls == []


def test_ensure_org_for_commit_swallows_rollback_failure_but_reraises_original(tmp_path, capsys):
    """If _delete_org itself raises, the warning goes to stderr but the
    ORIGINAL ingest exception still propagates — rollback failure must
    never mask the cause of the ingest failure."""
    def _create_org(**kw):
        return "FRESH-2"

    def _run_ingest(**kw):
        raise RuntimeError("ingest boom — original cause")

    def _delete_org(**kw):
        raise RuntimeError("rollback also boom — should NOT propagate")

    with pytest.raises(RuntimeError, match="ingest boom — original cause"):
        ingest_mod.ensure_org_for_commit(
            commit_sha="ohnosha000000",
            core_repo_path=Path("/fake/core"),
            cwd=tmp_path,
            _create_org=_create_org,
            _run_ingest=_run_ingest,
            _delete_org=_delete_org,
        )

    err = capsys.readouterr().err
    assert "rollback failed for fresh org FRESH-2" in err
    assert "Manual cleanup required" in err


def test_ensure_org_for_commit_warns_when_rollback_declined_non_pristine(tmp_path, capsys):
    """If delete_org returns deleted=False with a non-pristine reason
    (the org received real ingest data before the failure), the warning
    surfaces — the org is NOT deleted, manual review needed."""
    def _create_org(**kw):
        return "FRESH-NP"

    def _run_ingest(**kw):
        raise RuntimeError("ingest partial failure")

    def _delete_org(**kw):
        return {
            "deleted": False,
            "reason": "non-pristine",
            "non_pristine": [("code_revisions", 1)],
            "org_id": "FRESH-NP",
        }

    with pytest.raises(RuntimeError, match="ingest partial failure"):
        ingest_mod.ensure_org_for_commit(
            commit_sha="partialsha00",
            core_repo_path=Path("/fake/core"),
            cwd=tmp_path,
            _create_org=_create_org,
            _run_ingest=_run_ingest,
            _delete_org=_delete_org,
        )

    err = capsys.readouterr().err
    assert "rollback declined for fresh org FRESH-NP" in err
    assert "non-pristine" in err


def test_delete_org_uses_atlas_venv_python_and_returns_parsed_json(tmp_path):
    """delete_org shells out to the Atlas venv's python and parses the
    JSON status the inline script emits."""
    leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (leaf / ".venv" / "bin").mkdir(parents=True)
    venv_py = leaf / ".venv" / "bin" / "python"
    venv_py.touch()

    captured = {}

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        return SimpleNamespace(
            returncode=0,
            stdout='{"deleted": true, "name": "atlas_shadow_foo"}\n',
            stderr="",
        )

    result = ingest_mod.delete_org(
        core_repo_path=tmp_path,
        org_id="ORG-X",
        _subprocess_run=_fake_run,
    )

    assert result == {"deleted": True, "name": "atlas_shadow_foo", "org_id": "ORG-X"}
    # Argv shape: [<venv_py>, -c, <script>, <org_id>]
    assert captured["cmd"][0] == str(venv_py)
    assert captured["cmd"][1] == "-c"
    assert captured["cmd"][-1] == "ORG-X"
    assert "DELETE FROM orgs WHERE org_id" in captured["cmd"][2]


def test_delete_org_raises_on_subprocess_failure(tmp_path):
    leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (leaf / ".venv" / "bin").mkdir(parents=True)
    (leaf / ".venv" / "bin" / "python").touch()

    def _fake_run(cmd, **kw):
        return SimpleNamespace(returncode=2, stdout="", stderr="DB connection refused")

    with pytest.raises(RuntimeError, match="delete_org subprocess failed"):
        ingest_mod.delete_org(
            core_repo_path=tmp_path,
            org_id="ORG-Y",
            _subprocess_run=_fake_run,
        )


def test_list_shadow_orgs_returns_parsed_list(tmp_path):
    leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    (leaf / ".venv" / "bin").mkdir(parents=True)
    (leaf / ".venv" / "bin" / "python").touch()

    def _fake_run(cmd, **kw):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([
                {"org_id": "aaaa", "name": "atlas_shadow_foo", "created_at": "2026-05-13"},
                {"org_id": "bbbb", "name": "atlas_shadow_bar", "created_at": "2026-05-13"},
            ]) + "\n",
            stderr="",
        )

    rows = ingest_mod.list_shadow_orgs(
        core_repo_path=tmp_path,
        _subprocess_run=_fake_run,
    )
    assert len(rows) == 2
    assert rows[0]["name"] == "atlas_shadow_foo"
