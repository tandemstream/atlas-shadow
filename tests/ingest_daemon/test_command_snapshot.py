"""Tests for the command_snapshot lane (PR #20).

Covers:
  - ``parse_command_text`` whitelist (each shape, malicious rejection,
    flag whitelisting on find, qa_lookup.sh prefix stripping).
  - ``synthesize_command`` for path-only / path+lines receipts.
  - Each handler against a real local git fixture
    (show-range, sed-range, grep, ls, find, wc -l).
  - Absence_search classification (no-match-expected-absent vs.
    found-but-expected-absent).
  - Timeout / OS error path returns STATUS_ERROR cleanly.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas_shadow.ingest_daemon import command_snapshot as cs_mod
from atlas_shadow.ingest_daemon import doc_resolver as doc_resolver_mod


# ─── Fixtures ─────────────────────────────────────────────────────────


def _make_receipt(**kwargs):
    """Convenience receipt builder (uses SimpleNamespace to keep tests
    independent of the PacketReceipt dataclass schema)."""
    defaults = dict(
        question_id="q",
        question="q",
        oracle_claim="",
        oracle_excerpt="",
        evidence_type="source_excerpt",
        source_path=None,
        source_lines=None,
        source_commit=None,
        excerpt_sha256=None,
        command_text="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture
def git_fixture(tmp_path):
    """A tiny git repo with one commit. Yields (repo_path, commit_sha,
    body) so tests can compute expected hashes against known content."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(
        ["git", "config", "user.email", "t@t.test"],
        cwd=repo, check=True, timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo, check=True, timeout=10,
    )
    src = repo / "src" / "example.py"
    src.parent.mkdir(parents=True)
    body = "line one\nline two\nline three\nline four\n"
    src.write_text(body, encoding="utf-8")
    # Add a couple more files for grep / ls tests
    (repo / "docs").mkdir()
    (repo / "docs" / "spec.md").write_text("# Spec\nheading_path is forbidden\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "build.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=repo, check=True, timeout=10,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()
    return repo, commit, body


# ─── Parser tests ─────────────────────────────────────────────────────


def test_parse_show_range():
    p = cs_mod.parse_command_text(
        "scripts/qa_lookup.sh show-range abc1234:src/x.py 10 20"
    )
    assert p == {
        "op": "show-range", "commit": "abc1234",
        "path": "src/x.py", "start": 10, "end": 20,
    }


def test_parse_sed_range_with_qa_lookup_prefix():
    p = cs_mod.parse_command_text(
        "scripts/qa_lookup.sh sed-range src/x.py 5 9"
    )
    assert p == {
        "op": "sed-range", "path": "src/x.py", "start": 5, "end": 9,
    }


def test_parse_sed_range_without_prefix():
    """Bare ``sed-range`` form (no qa_lookup.sh wrapper)."""
    p = cs_mod.parse_command_text("sed-range src/x.py 1 3")
    assert p == {"op": "sed-range", "path": "src/x.py", "start": 1, "end": 3}


def test_parse_grep():
    p = cs_mod.parse_command_text(
        'scripts/qa_lookup.sh grep "heading_path" core tests'
    )
    assert p == {
        "op": "grep", "pattern": "heading_path",
        "paths": ["core", "tests"],
    }


def test_parse_rg_treated_same_as_grep():
    p = cs_mod.parse_command_text(
        "scripts/qa_lookup.sh rg pattern path1"
    )
    assert p == {"op": "grep", "pattern": "pattern", "paths": ["path1"]}


def test_parse_ls():
    p = cs_mod.parse_command_text("ls src/example.py")
    assert p == {"op": "ls", "path": "src/example.py"}


def test_parse_find_bare_path_only():
    p = cs_mod.parse_command_text("find scripts")
    assert p == {"op": "find", "path": "scripts", "extra": []}


def test_parse_find_rejects_flags():
    """v1 rejects any find flags. The handler delegates to ls and
    ignores filters, so accepting -type/-name would produce wrong
    absence-search verdicts (any unrelated file under the search
    root would falsely contradict the absence claim).
    """
    for cmd in [
        "find docs -type d -name agents",
        "find docs -type f",
        "find docs -maxdepth 1",
        "find docs -name '*.md'",
    ]:
        assert cs_mod.parse_command_text(cmd) is None, cmd


def test_parse_wc_l():
    p = cs_mod.parse_command_text("wc -l src/example.py")
    assert p == {"op": "wc-l", "path": "src/example.py"}


@pytest.mark.parametrize("cmd", [
    # Shell metacharacters reject — defense against accidental injection.
    "ls foo && find bar",
    "ls foo | grep bar",
    "cat /etc/passwd > /tmp/x",
    "ls $(whoami)",
    "echo `id`",
    "find docs -exec ls {}",  # arbitrary flag not on whitelist
    "ls -la docs",  # flag form not allowed for ls
    "grep pattern",  # missing path
    "sed-range x 1",  # missing end
    "show-range nocolon:in-path 1 2",  # actually matches; weed out below
    "show-range : 1 2",  # empty path
    "show-range deadbeef:src/x.py notanint 2",
    "",
    None,
])
def test_parse_rejects_unsupported(cmd):
    """Each rejected form returns None."""
    out = cs_mod.parse_command_text(cmd)
    if cmd == "show-range nocolon:in-path 1 2":
        # this is actually valid — commit=nocolon, path=in-path. Skip.
        pytest.skip("ambiguous form — parser accepts as commit=nocolon")
    assert out is None


# ─── Synthesize tests ─────────────────────────────────────────────────


def test_synthesize_from_trailing_slash_directory():
    """Trailing-slash paths unambiguously intend directory listing."""
    r = _make_receipt(source_path="scripts/", source_commit="abc")
    p = cs_mod.synthesize_command(r)
    assert p == {"op": "ls", "path": "scripts/", "synthesized": True}


def test_synthesize_path_only_file_returns_none():
    """File paths without trailing slash don't synthesize — even though
    they look like ``ls -la`` candidates, the LLM grader has
    historically resolved them via atlas. Pre-empting would eat real
    measurements."""
    r = _make_receipt(source_path="Makefile", source_commit="abc")
    assert cs_mod.synthesize_command(r) is None


def test_synthesize_path_plus_lines_returns_none():
    """Path+lines receipts are NOT synthesized — they should flow
    through the atlas grading path. Authors who explicitly want
    command verification can put ``sed-range`` in command_text
    (parsed via the whitelist), which is explicit consent.
    """
    r = _make_receipt(
        source_path="src/x.py", source_lines="10-20", source_commit="abc"
    )
    assert cs_mod.synthesize_command(r) is None


def test_synthesize_returns_none_when_no_path():
    r = _make_receipt(source_commit="abc")
    assert cs_mod.synthesize_command(r) is None


# ─── Handler tests (with real git fixture) ────────────────────────────


def test_show_range_matches_excerpt_sha(git_fixture):
    repo, commit, body = git_fixture
    sliced = "\n".join(body.splitlines()[1:3])  # lines 2-3
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    expected = hashlib.sha256(canon.encode("utf-8")).hexdigest()

    r = _make_receipt(
        source_commit=commit, excerpt_sha256=expected,
        command_text=f"scripts/qa_lookup.sh show-range {commit}:src/example.py 2 3",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_MATCH
    assert result.hash_match is True
    assert result.resolved_sha256 == expected


def test_sed_range_matches_excerpt_sha(git_fixture):
    repo, commit, body = git_fixture
    sliced = "\n".join(body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    expected = hashlib.sha256(canon.encode("utf-8")).hexdigest()

    r = _make_receipt(
        source_commit=commit, excerpt_sha256=expected,
        command_text="scripts/qa_lookup.sh sed-range src/example.py 2 3",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_MATCH


def test_sed_range_mismatch_returns_mismatch(git_fixture):
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_commit=commit, excerpt_sha256="0" * 64,
        command_text="scripts/qa_lookup.sh sed-range src/example.py 1 1",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_MISMATCH
    assert result.hash_match is False


def test_sed_range_source_missing(git_fixture):
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_commit=commit,
        command_text="scripts/qa_lookup.sh sed-range missing.py 1 5",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_SOURCE_MISSING


def test_grep_absence_search_no_match_verifies_claim(git_fixture):
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_commit=commit,
        evidence_type="absence_search",
        command_text='scripts/qa_lookup.sh grep "this_never_appears" src',
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_NO_MATCH_EXPECTED_ABSENT


def test_grep_absence_search_found_contradicts_claim(git_fixture):
    """Pattern is present → absence claim contradicted."""
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_commit=commit,
        evidence_type="absence_search",
        command_text='scripts/qa_lookup.sh grep "heading_path" docs',
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_FOUND_BUT_EXPECTED_ABSENT
    assert "heading_path" in result.output_head


def test_ls_directory_listing(git_fixture):
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_commit=commit,
        evidence_type="source_excerpt",
        command_text="ls scripts/",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_MATCH
    assert "scripts/build.sh" in result.output_head


def test_ls_path_missing(git_fixture):
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_commit=commit,
        evidence_type="source_excerpt",
        command_text="ls does/not/exist",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    # Empty ls-tree output → path absent at this commit.
    assert result.status == cs_mod.STATUS_SOURCE_MISSING


def test_find_bare_treated_as_ls(git_fixture):
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_commit=commit,
        command_text="find scripts",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_MATCH


def test_find_with_flags_falls_through_to_unsupported(git_fixture):
    """Codex's P1 from PR #20 review: filtered find forms must not
    silently behave like ``ls <path>``. Bare absence claim:
    ``find docs -type d -name agents`` against a repo where
    ``docs/agents`` is absent but ``docs/other/`` exists must NOT
    return found_but_expected_absent. The fix is to reject the
    parse so the row falls through to normal grading.
    """
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_commit=commit,
        evidence_type="absence_search",
        command_text="find scripts -type d -name nonexistent",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_UNSUPPORTED


def test_wc_l_counts_lines(git_fixture):
    repo, commit, body = git_fixture
    # body = "line one\nline two\nline three\nline four\n" — 4 newlines.
    expected_output = doc_resolver_mod._excerpt_canonical(
        f"{body.count(chr(10))} src/example.py"
    )
    expected_sha = hashlib.sha256(expected_output.encode("utf-8")).hexdigest()
    r = _make_receipt(
        source_commit=commit,
        excerpt_sha256=expected_sha,
        command_text="wc -l src/example.py",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_MATCH


def test_path_plus_lines_without_explicit_command_falls_through(git_fixture):
    """Path+lines receipts with no command_text are NOT synthesized.
    They return UNSUPPORTED so atlas-grading runs (preserves atlas
    retrieval measurement)."""
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_path="src/example.py",
        source_lines="2-3",
        source_commit=commit,
        command_text="",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_UNSUPPORTED


def test_synthesized_ls_from_trailing_slash_directory(git_fixture):
    """q12 shape: trailing-slash directory listing synthesizes ls."""
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_path="scripts/",
        source_commit=commit,
        command_text="",  # empty → synthesize
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_MATCH
    assert "scripts/build.sh" in result.output_head


def test_path_only_file_falls_through(git_fixture):
    """q10 / q1 shape: file path without trailing slash and no
    command_text returns UNSUPPORTED — atlas-graded as before."""
    repo, commit, _body = git_fixture
    r = _make_receipt(
        source_path="src/example.py",  # file, no trailing slash
        source_commit=commit,
        command_text="",
    )
    result = cs_mod.resolve_command_snapshot(r, repo_path=repo)
    assert result.status == cs_mod.STATUS_UNSUPPORTED


def test_unsupported_command_falls_through():
    r = _make_receipt(command_text="ls -la docs && find docs")
    result = cs_mod.resolve_command_snapshot(r, repo_path=Path("/tmp"))
    assert result.status == cs_mod.STATUS_UNSUPPORTED


def test_no_command_and_no_anchor_returns_unsupported():
    r = _make_receipt(command_text="", source_path=None)
    result = cs_mod.resolve_command_snapshot(r, repo_path=Path("/tmp"))
    assert result.status == cs_mod.STATUS_UNSUPPORTED


# ─── Timeout / error path ─────────────────────────────────────────────


def test_subprocess_timeout_returns_error_cleanly(git_fixture, monkeypatch):
    repo, commit, _body = git_fixture

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    r = _make_receipt(
        source_commit=commit,
        command_text="scripts/qa_lookup.sh sed-range src/example.py 1 3",
    )
    result = cs_mod.resolve_command_snapshot(
        r, repo_path=repo, _subprocess_run=fake_run,
    )
    # _git_show returns (None, 124) on timeout → SOURCE_MISSING per the
    # handler contract. The handler doesn't re-raise.
    assert result.status == cs_mod.STATUS_SOURCE_MISSING
    assert result.exit_code == 124


def test_oserror_returns_error_cleanly(git_fixture):
    repo, commit, _body = git_fixture

    def fake_run(*args, **kwargs):
        raise OSError("test failure")

    r = _make_receipt(
        source_commit=commit,
        command_text="scripts/qa_lookup.sh grep pattern src",
    )
    result = cs_mod.resolve_command_snapshot(
        r, repo_path=repo, _subprocess_run=fake_run,
    )
    # _git_grep returns (None, 124) → handler converts to STATUS_ERROR
    # because grep errors out.
    assert result.status == cs_mod.STATUS_ERROR
