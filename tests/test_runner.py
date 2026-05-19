"""Tests for atlas_shadow.runner.

Atlas spawn-out is fully stubbed via subprocess injection so these tests
run with no live core checkout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas_shadow import parser as parser_mod
from atlas_shadow import runner as runner_mod


def test_build_atlas_query_argv_required_fields(monkeypatch):
    monkeypatch.delenv("ATLAS_SHADOW_ATLAS_QUERY_CMD", raising=False)
    monkeypatch.delenv("ATLAS_SHADOW_WORKSPACE_CMD", raising=False)
    monkeypatch.delenv("WORKSPACE_PY", raising=False)
    monkeypatch.delenv("WORKSPACE_VENV_PY", raising=False)
    argv = runner_mod.build_atlas_query_argv(
        question="what is foo?",
        org_id="af6ef504-7492-4dbb-99cb-9437141bd029",
        atlas_python="/atlas/.venv/bin/python",
    )
    assert argv[:3] == ["/atlas/.venv/bin/python", "-m", "scripts.workspace_atlas_query"]
    assert "--question" in argv
    assert "--org-id" in argv
    assert "--tool" in argv  # default auto
    assert "--output-format" in argv
    # Default tool is auto, output-format is json
    assert argv[argv.index("--tool") + 1] == "auto"
    assert argv[argv.index("--output-format") + 1] == "json"


def test_build_atlas_query_argv_env_override_for_direct_launcher(monkeypatch):
    """ATLAS_SHADOW_ATLAS_QUERY_CMD overrides the module launcher."""
    monkeypatch.setenv(
        "ATLAS_SHADOW_ATLAS_QUERY_CMD",
        "/path/to/python -m scripts.workspace_atlas_query",
    )
    argv = runner_mod.build_atlas_query_argv(question="q", org_id="o")
    assert argv[0] == "/path/to/python"
    assert argv[1] == "-m"
    assert argv[2] == "scripts.workspace_atlas_query"


def test_build_atlas_query_argv_legacy_workspace_override(monkeypatch):
    monkeypatch.delenv("ATLAS_SHADOW_ATLAS_QUERY_CMD", raising=False)
    monkeypatch.setenv("ATLAS_SHADOW_WORKSPACE_CMD", "/path/to/python /path/to/workspace.py")
    argv = runner_mod.build_atlas_query_argv(question="q", org_id="o")
    assert argv[:5] == [
        "/path/to/python",
        "/path/to/workspace.py",
        "run",
        "atlas-query",
        "--",
    ]


def test_build_atlas_query_argv_env_override_via_workspace_py_pair(monkeypatch):
    """WORKSPACE_PY + WORKSPACE_VENV_PY (Atlas shell-function convention)
    are also honored so users don't need to compose ATLAS_SHADOW_WORKSPACE_CMD."""
    monkeypatch.delenv("ATLAS_SHADOW_ATLAS_QUERY_CMD", raising=False)
    monkeypatch.delenv("ATLAS_SHADOW_WORKSPACE_CMD", raising=False)
    monkeypatch.setenv("WORKSPACE_PY", "/p/ws.py")
    monkeypatch.setenv("WORKSPACE_VENV_PY", "/p/venv/bin/python")
    argv = runner_mod.build_atlas_query_argv(question="q", org_id="o")
    assert argv[0] == "/p/venv/bin/python"
    assert argv[1] == "/p/ws.py"
    assert argv[2:5] == ["run", "atlas-query", "--"]


def test_build_atlas_query_argv_passes_all_optional_kwargs():
    """Wrapper-drift check: ensure the runner threads every optional kwarg
    the wrapper accepts. Mirrors the dogfood-v2 reference's call shape."""
    argv = runner_mod.build_atlas_query_argv(
        question="q",
        org_id="org",
        tool="find_code",
        principal_id="prin",
        domain_pack="code",
        code_revision_id="rev",
    )
    assert "--principal-id" in argv
    assert "--domain-pack" in argv
    assert "--code-revision-id" in argv
    assert argv[argv.index("--principal-id") + 1] == "prin"
    assert argv[argv.index("--domain-pack") + 1] == "code"
    assert argv[argv.index("--code-revision-id") + 1] == "rev"


def test_build_atlas_query_argv_omits_unset_optionals():
    argv = runner_mod.build_atlas_query_argv(question="q", org_id="o")
    assert "--principal-id" not in argv
    assert "--domain-pack" not in argv
    assert "--code-revision-id" not in argv


def _stub_subprocess_run(*, stdout: str, stderr: str = "", returncode: int = 0):
    def _run(argv, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return _run


def test_invoke_atlas_query_parses_wrapper_payload(tmp_path):
    payload = {
        "question": "q?",
        "commit": "abcdef",
        "tool_used": "find_code",
        "answer_text": "the answer text",
        "raw_result": {"foo": 1},
        "evidence_keys": ["a.py:1-5"],
        "metrics": {"atlas_latency_ms": 123},
        "request_id": "req-1",
    }
    fake = _stub_subprocess_run(stdout=json.dumps(payload))
    resp = runner_mod.invoke_atlas_query(
        ["workspace", "run", "atlas-query"],
        cwd=tmp_path,
        _subprocess_run=fake,
    )
    assert resp.tool_used == "find_code"
    assert resp.answer_text == "the answer text"
    assert resp.evidence_keys == ["a.py:1-5"]
    assert resp.atlas_latency_ms == 123
    assert resp.request_id == "req-1"
    assert resp.returncode == 0
    assert resp.exception is None


def test_invoke_atlas_query_handles_non_json_stdout(tmp_path):
    fake = _stub_subprocess_run(stdout="not json", stderr="error", returncode=1)
    resp = runner_mod.invoke_atlas_query(["x"], cwd=tmp_path, _subprocess_run=fake)
    assert resp.returncode == 1
    assert resp.exception and "JSONDecodeError" in resp.exception
    assert resp.stderr == "error"


def test_invoke_atlas_query_handles_timeout(tmp_path):
    def _run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=kw.get("timeout", 1))
    resp = runner_mod.invoke_atlas_query(["x"], cwd=tmp_path, _subprocess_run=_run, timeout=1)
    assert resp.returncode == -1
    assert resp.exception and "TimeoutExpired" in resp.exception


def test_invoke_atlas_query_handles_file_not_found(tmp_path):
    def _run(*a, **kw):
        raise FileNotFoundError("workspace not on PATH")
    resp = runner_mod.invoke_atlas_query(["x"], cwd=tmp_path, _subprocess_run=_run)
    assert resp.returncode == -1
    assert resp.exception and "FileNotFoundError" in resp.exception


def test_run_one_routes_through_invoke_and_records_metadata(tmp_path):
    # Build a fake "core repo" tree so the atlas leaf check passes.
    atlas_leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    atlas_leaf.mkdir(parents=True)

    calls = []

    def fake_invoke(argv, *, cwd, timeout):
        calls.append({"argv": argv, "cwd": cwd, "timeout": timeout})
        return runner_mod.AtlasResponse(
            tool_used="find_code",
            answer_text="ans",
            raw_result=None,
            evidence_keys=["k"],
            atlas_latency_ms=10,
            request_id="r",
            commit="c",
        )

    receipt = parser_mod.Receipt(
        question_id="Q01",
        question="q?",
        oracle_excerpt="e",
        oracle_claim="c",
    )
    resp = runner_mod.run_one(
        receipt,
        fixture_id="fx",
        org_id="org",
        core_repo_path=tmp_path,
        tool="find_code",
        principal_id="prin",
        domain_pack="code",
        code_revision_id="rev",
        _invoke=fake_invoke,
    )
    assert resp.question_id == "Q01"
    assert resp.fixture_id == "fx"
    assert resp.org_id == "org"
    assert resp.tool == "find_code"
    assert resp.atlas_response.tool_used == "find_code"
    # The single call should have happened against the atlas leaf, not the
    # core root.
    assert calls[0]["cwd"] == atlas_leaf
    # All four optional kwargs threaded through.
    argv = calls[0]["argv"]
    assert "--principal-id" in argv
    assert "--domain-pack" in argv
    assert "--code-revision-id" in argv


def test_run_one_missing_atlas_leaf_raises(tmp_path):
    receipt = parser_mod.Receipt(
        question_id="Q01", question="q?", oracle_excerpt="e", oracle_claim="c"
    )
    with pytest.raises(FileNotFoundError, match="Atlas leaf not found"):
        runner_mod.run_one(
            receipt,
            fixture_id="fx",
            org_id="org",
            core_repo_path=tmp_path,  # has no products/... tree
        )


def test_run_batch_collects_all_responses(tmp_path):
    atlas_leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    atlas_leaf.mkdir(parents=True)
    receipts = [
        parser_mod.Receipt(question_id=f"Q{i:02d}", question="q?", oracle_excerpt="e", oracle_claim="c")
        for i in range(1, 4)
    ]
    calls = []

    def fake_invoke(argv, *, cwd, timeout):
        calls.append(argv)
        return runner_mod.AtlasResponse(
            tool_used="auto", answer_text="a", raw_result=None,
            evidence_keys=[], atlas_latency_ms=1, request_id="", commit=""
        )

    out = runner_mod.run_batch(
        receipts, fixture_id="fx", org_id="org",
        core_repo_path=tmp_path, _invoke=fake_invoke
    )
    assert len(out) == 3
    assert [r.question_id for r in out] == ["Q01", "Q02", "Q03"]
    assert len(calls) == 3
