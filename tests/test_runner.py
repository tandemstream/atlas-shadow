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


def test_build_atlas_query_argv_prefers_daemon_query_script(tmp_path, monkeypatch):
    """Modern core removed scripts.workspace_atlas_query; atlas-shadow should
    use the daemon-owned workflow query script when that seam exists."""
    monkeypatch.delenv("ATLAS_SHADOW_ATLAS_QUERY_CMD", raising=False)
    monkeypatch.delenv("ATLAS_SHADOW_WORKSPACE_CMD", raising=False)
    monkeypatch.delenv("WORKSPACE_PY", raising=False)
    monkeypatch.delenv("WORKSPACE_VENV_PY", raising=False)

    atlas_python = (
        tmp_path
        / "products"
        / "tandem"
        / "packages"
        / "python"
        / "atlas"
        / ".venv"
        / "bin"
        / "python"
    )
    daemon_script = (
        tmp_path
        / "products"
        / "tandem"
        / "services"
        / "tandem-daemon"
        / "scripts"
        / "atlas_workflow.py"
    )
    daemon_script.parent.mkdir(parents=True)
    daemon_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    argv = runner_mod.build_atlas_query_argv(
        question="q",
        org_id="o",
        atlas_python=str(atlas_python),
    )

    assert argv[:3] == [str(atlas_python), str(daemon_script), "query"]
    assert "--question" in argv
    assert "--org-id" in argv


def test_build_atlas_query_argv_uses_daemon_venv_when_present(tmp_path, monkeypatch):
    monkeypatch.delenv("ATLAS_SHADOW_ATLAS_QUERY_CMD", raising=False)
    monkeypatch.delenv("ATLAS_SHADOW_WORKSPACE_CMD", raising=False)
    monkeypatch.delenv("WORKSPACE_PY", raising=False)
    monkeypatch.delenv("WORKSPACE_VENV_PY", raising=False)

    atlas_python = (
        tmp_path
        / "products"
        / "tandem"
        / "packages"
        / "python"
        / "atlas"
        / ".venv"
        / "bin"
        / "python"
    )
    daemon_root = tmp_path / "products" / "tandem" / "services" / "tandem-daemon"
    daemon_script = daemon_root / "scripts" / "atlas_workflow.py"
    daemon_python = daemon_root / ".venv" / "bin" / "python"
    daemon_script.parent.mkdir(parents=True)
    daemon_python.parent.mkdir(parents=True)
    daemon_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    daemon_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    argv = runner_mod.build_atlas_query_argv(
        question="q",
        org_id="o",
        atlas_python=str(atlas_python),
    )

    assert argv[:3] == [str(daemon_python), str(daemon_script), "query"]


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


# ─── Atlas-query cache integration ────────────────────────────────────


def _make_atlas_leaf(tmp_path):
    atlas_leaf = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas"
    atlas_leaf.mkdir(parents=True)
    return atlas_leaf


def _stub_invoke(*, response=None):
    """Build a fake _invoke that records argv calls + returns a stub
    AtlasResponse. Default response is a successful one suitable for
    caching."""
    calls = []
    default = runner_mod.AtlasResponse(
        tool_used="find_code",
        answer_text="cached-or-fresh",
        raw_result={"sample": "payload"},
        evidence_keys=["k"],
        atlas_latency_ms=42,
        request_id="r",
        commit="c",
    )

    def _invoke(argv, *, cwd, timeout):
        calls.append({"argv": argv, "cwd": cwd, "timeout": timeout})
        return response or default

    return _invoke, calls


def _receipt():
    return parser_mod.Receipt(
        question_id="Q01",
        question="what does foo do?",
        oracle_excerpt="e",
        oracle_claim="c",
        source_path="core/foo.py",
        source_lines="10-20",
        commit_sha="408858a",
    )


def test_run_one_without_cache_returns_disabled_status(tmp_path):
    """No cache passed → cache_status='disabled', subprocess called
    normally. Live PR-grading webhook path follows this codepath."""
    _make_atlas_leaf(tmp_path)
    _invoke, calls = _stub_invoke()

    resp = runner_mod.run_one(
        _receipt(),
        fixture_id="fx",
        org_id="org-1",
        core_repo_path=tmp_path,
        principal_id="user-1",
        domain_pack="code",
        code_revision_id="rev-1",
        _invoke=_invoke,
    )
    assert resp.atlas_cache_status == "disabled"
    assert len(calls) == 1


def test_run_one_with_cache_hit_skips_subprocess(tmp_path):
    """Cache hit → no subprocess call, atlas_response reconstructed
    from the cached JSON. This is the path that makes regen-loops
    cheap."""
    from atlas_shadow.ingest_daemon.atlas_query_cache import (
        AtlasQueryCache,
        CacheKey,
    )

    _make_atlas_leaf(tmp_path)
    cache = AtlasQueryCache(tmp_path / "cache.sqlite")
    receipt = _receipt()
    # Pre-populate the cache with what the runner would store.
    cache.set(
        CacheKey(
            query_text=receipt.question,
            tool="find_code",
            source_path=receipt.source_path,
            source_lines=receipt.source_lines,
            source_commit=receipt.commit_sha,
            code_revision_id="rev-1",
            org_id="org-1",
            principal_id="user-1",
            domain_pack="code",
        ),
        response_json=__import__("json").dumps({
            "tool_used": "find_code",
            "answer_text": "from-cache",
            "raw_result": {"src": "cache"},
            "evidence_keys": ["e1"],
            "atlas_latency_ms": 999,
            "request_id": "cached-req",
            "commit": "408858a",
            "stderr": "",
            "returncode": 0,
            "exception": None,
        }),
        response_latency_ms=999,
    )

    _invoke, calls = _stub_invoke()
    resp = runner_mod.run_one(
        receipt,
        fixture_id="fx",
        org_id="org-1",
        core_repo_path=tmp_path,
        tool="find_code",
        principal_id="user-1",
        domain_pack="code",
        code_revision_id="rev-1",
        atlas_query_cache=cache,
        _invoke=_invoke,
    )
    assert resp.atlas_cache_status == "hit"
    assert resp.atlas_response.answer_text == "from-cache"
    assert resp.atlas_response.request_id == "cached-req"
    assert len(calls) == 0, "subprocess must NOT be called on cache hit"


def test_run_one_with_cache_miss_calls_then_stores(tmp_path):
    """Cache miss → subprocess called, result stored. Second call with
    same inputs is a hit. This is the canonical "first run misses,
    second run hits" behavior the operator validates."""
    from atlas_shadow.ingest_daemon.atlas_query_cache import AtlasQueryCache

    _make_atlas_leaf(tmp_path)
    cache = AtlasQueryCache(tmp_path / "cache.sqlite")
    _invoke, calls = _stub_invoke()
    receipt = _receipt()

    resp1 = runner_mod.run_one(
        receipt, fixture_id="fx", org_id="org-1",
        core_repo_path=tmp_path, tool="find_code",
        principal_id="user-1", domain_pack="code",
        code_revision_id="rev-1",
        atlas_query_cache=cache, _invoke=_invoke,
    )
    assert resp1.atlas_cache_status == "miss"
    assert len(calls) == 1

    # Second call: same inputs → cache hit, no new subprocess.
    resp2 = runner_mod.run_one(
        receipt, fixture_id="fx", org_id="org-1",
        core_repo_path=tmp_path, tool="find_code",
        principal_id="user-1", domain_pack="code",
        code_revision_id="rev-1",
        atlas_query_cache=cache, _invoke=_invoke,
    )
    assert resp2.atlas_cache_status == "hit"
    assert len(calls) == 1, "second call must come from cache"
    # Round-trip: cached response equals the original.
    assert resp2.atlas_response.answer_text == resp1.atlas_response.answer_text
    assert resp2.atlas_response.tool_used == resp1.atlas_response.tool_used


def test_run_one_does_not_cache_failed_responses(tmp_path):
    """A subprocess that returned nonzero / raised an exception must
    NOT be cached — caching a transient failure would mask
    intermittent infrastructure issues on subsequent runs."""
    from atlas_shadow.ingest_daemon.atlas_query_cache import AtlasQueryCache

    _make_atlas_leaf(tmp_path)
    cache = AtlasQueryCache(tmp_path / "cache.sqlite")
    failure_response = runner_mod.AtlasResponse(
        tool_used="", answer_text="", raw_result=None,
        evidence_keys=[], atlas_latency_ms=5, request_id="", commit="",
        stderr="boom", returncode=1, exception="RuntimeError: subprocess died",
    )
    _invoke, calls = _stub_invoke(response=failure_response)

    resp = runner_mod.run_one(
        _receipt(), fixture_id="fx", org_id="org-1",
        core_repo_path=tmp_path, tool="find_code",
        principal_id="user-1", domain_pack="code",
        code_revision_id="rev-1",
        atlas_query_cache=cache, _invoke=_invoke,
    )
    assert resp.atlas_cache_status == "miss"
    # Cache should be empty — the failed response was not stored.
    assert cache.stats()["entries"] == 0


def test_run_one_cache_key_includes_source_anchors(tmp_path):
    """Critical correctness: two receipts with identical query text
    but different source_path / source_lines must produce different
    cache keys. PR #426 fast-path retrieval depends on those anchors,
    so they must invalidate the cache."""
    from atlas_shadow.ingest_daemon.atlas_query_cache import AtlasQueryCache

    _make_atlas_leaf(tmp_path)
    cache = AtlasQueryCache(tmp_path / "cache.sqlite")
    _invoke, calls = _stub_invoke()

    a = parser_mod.Receipt(
        question_id="Q01", question="same question",
        oracle_excerpt="e", oracle_claim="c",
        source_path="core/a.py", source_lines="1-10",
        commit_sha="408858a",
    )
    b = parser_mod.Receipt(
        question_id="Q02", question="same question",
        oracle_excerpt="e", oracle_claim="c",
        source_path="core/b.py", source_lines="1-10",
        commit_sha="408858a",
    )
    runner_mod.run_one(
        a, fixture_id="fx", org_id="org-1",
        core_repo_path=tmp_path, tool="find_code",
        atlas_query_cache=cache, _invoke=_invoke,
    )
    runner_mod.run_one(
        b, fixture_id="fx", org_id="org-1",
        core_repo_path=tmp_path, tool="find_code",
        atlas_query_cache=cache, _invoke=_invoke,
    )
    # Two distinct cache entries → both required separate subprocess calls.
    assert len(calls) == 2
    assert cache.stats()["entries"] == 2
