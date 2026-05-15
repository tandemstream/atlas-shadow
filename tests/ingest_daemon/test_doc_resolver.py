"""AG8 — T4a doc-anchored resolver tests.

Coverage (per `02-plan.md` §8 AG8):

  * (a) `db_commit_scoped` happy path — primary DB lookup returns an
    artifact + chunks + heading metadata.
  * (b) `git_receipt_snapshot` fallback — DB has no row, but the local
    clone has the commit; sha256 verification passes.
  * (c) `unresolved_source_ref` — neither path resolves.
  * (d) Heading metadata extraction — ``metadata.chunk_headings``
    populated -> resolver returns ``heading_path`` / ``heading_level``;
    absent -> returns None.

T4a's DB connection is stubbed via the ``_connect`` injection seam — no
live Atlas DB required. The git-fallback test uses ``tempfile`` +
``git init`` to materialize a real clone.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from atlas_shadow.ingest_daemon import doc_resolver as doc_resolver_mod
from atlas_shadow.ingest_daemon.grader_service import PacketReceipt


TEST_ORG_ID = "05e93005-565f-46f0-a8f7-c0448050f43f"
TEST_REPO = "tandemstream/core"
TEST_COMMIT = "abc1234567890abcdef0123456789abcdef01234"
TEST_PATH = "docs/architecture.md"


def _mk_doc_receipt(
    *,
    source_path: str = TEST_PATH,
    source_commit: str = TEST_COMMIT,
    source_lines: str = "1-3",
    excerpt_sha256: str = None,
    oracle_excerpt: str = "",
) -> PacketReceipt:
    return PacketReceipt(
        question_id="q1",
        question="doc q",
        oracle_claim="doc claim",
        oracle_excerpt=oracle_excerpt,
        source_path=source_path,
        source_lines=source_lines,
        source_commit=source_commit,
        excerpt_sha256=excerpt_sha256,
        status="ok",
        evidence_type="source_excerpt",
    )


def _fake_conn(rows_for_artifacts: Any, rows_for_chunks: list[tuple]):
    """Build a fake psycopg2 connection that returns canned results.

    rows_for_artifacts: a single (artifact_id, metadata_dict) tuple, or None.
    rows_for_chunks: list of (chunk_id, chunk_index, raw_text, start_offset, end_offset).
    """
    class _Cursor:
        def __init__(self):
            self._last_query = ""

        def execute(self, query, params=None):
            self._last_query = query
            return self

        def fetchone(self):
            if "FROM artifacts" in self._last_query:
                return rows_for_artifacts
            return None

        def fetchall(self):
            if "FROM artifact_chunks" in self._last_query:
                return list(rows_for_chunks)
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    def connect(*args, **kwargs):
        return _Conn()

    return connect


# ===========================================================================
# AG8 (a) — db_commit_scoped happy path
# ===========================================================================


def test_doc_resolver_db_commit_scoped_happy_path(monkeypatch):
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    receipt = _mk_doc_receipt(oracle_excerpt="# Architecture overview")
    chunk_text = "# Architecture overview\n\nAtlas separates ingest from query.\n"
    fake_artifact = (
        "00000000-0000-0000-0000-000000000001",
        {"chunk_headings": {"0": {"heading_path": ["Architecture overview"], "heading_level": 1}}},
    )
    fake_chunks = [
        (
            "10000000-0000-0000-0000-000000000001",
            0,
            chunk_text,
            0,
            len(chunk_text),
        )
    ]
    result = doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=Path("/nonexistent"),
        _connect=_fake_conn(fake_artifact, fake_chunks),
    )
    assert result.status == doc_resolver_mod.STATUS_DB_COMMIT_SCOPED
    assert result.revision_binding == doc_resolver_mod.BINDING_DB_COMMIT_SCOPED
    assert result.artifact_id == "00000000-0000-0000-0000-000000000001"
    assert result.chunk_id == "10000000-0000-0000-0000-000000000001"
    assert "Architecture overview" in result.raw_text
    # AG8 (d): heading metadata extraction
    assert result.heading_path == ["Architecture overview"]
    assert result.heading_level == 1
    assert result.start_offset == 0
    assert result.end_offset == len(chunk_text)


def test_doc_resolver_db_commit_scoped_no_heading_metadata(monkeypatch):
    """AG8 (d): when chunk_headings is empty, heading_path/level are None."""
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    receipt = _mk_doc_receipt(oracle_excerpt="some content")
    fake_artifact = (
        "00000000-0000-0000-0000-000000000002",
        {"chunk_headings": {}},  # empty
    )
    fake_chunks = [
        (
            "20000000-0000-0000-0000-000000000001",
            0,
            "some content here\n",
            0,
            18,
        )
    ]
    result = doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=Path("/nonexistent"),
        _connect=_fake_conn(fake_artifact, fake_chunks),
    )
    assert result.status == doc_resolver_mod.STATUS_DB_COMMIT_SCOPED
    assert result.heading_path is None
    assert result.heading_level is None


# ===========================================================================
# AG8 (b) — git_receipt_snapshot fallback
# ===========================================================================


@pytest.fixture
def tmp_git_repo(tmp_path):
    """Create a tmp git repo with one commit containing a known doc file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Make .git inside
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, timeout=10)
    subprocess.run(
        ["git", "config", "user.email", "t@t.test"], cwd=str(repo), check=True, timeout=10
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), check=True, timeout=10
    )
    doc_path = repo / "docs" / "architecture.md"
    doc_path.parent.mkdir(parents=True)
    doc_content = "Line 1\nLine 2 has content\nLine 3\nLine 4\nLine 5\n"
    doc_path.write_text(doc_content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, timeout=10)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=str(repo), check=True, timeout=10
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    return repo, head, doc_content


def test_doc_resolver_git_receipt_snapshot_fallback(tmp_git_repo, monkeypatch):
    """No DB row -> git show <commit>:<path> -> sha256 verifies -> snapshot."""
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    repo_path, commit, doc_content = tmp_git_repo

    # Compute the expected sha256 for lines 2-3 of the doc body.
    lines = doc_content.splitlines()
    sliced = "\n".join(lines[1:3])  # 1-indexed lines 2-3
    canon = doc_resolver_mod._excerpt_canonical(sliced, indent="")
    expected_sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()

    receipt = _mk_doc_receipt(
        source_path="docs/architecture.md",
        source_commit=commit,
        source_lines="2-3",
        excerpt_sha256=expected_sha,
    )
    result = doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=repo_path,
        _connect=_fake_conn(None, []),  # primary lookup misses
    )
    assert result.status == doc_resolver_mod.STATUS_GIT_RECEIPT_SNAPSHOT
    assert result.revision_binding == doc_resolver_mod.BINDING_GIT_RECEIPT_SNAPSHOT
    assert result.raw_text == sliced
    assert result.artifact_id is None  # snapshot path doesn't know the artifact


# ===========================================================================
# AG8 (c) — unresolved_source_ref
# ===========================================================================


def test_doc_resolver_unresolved_unknown_commit(tmp_git_repo, monkeypatch):
    """Receipt's commit is NOT in the local clone -> unresolved."""
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    repo_path, _real_commit, _ = tmp_git_repo
    receipt = _mk_doc_receipt(
        source_path="docs/architecture.md",
        source_commit="f" * 40,  # not in the repo
        source_lines="1-3",
        excerpt_sha256="0" * 64,
    )
    result = doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=repo_path,
        _connect=_fake_conn(None, []),
    )
    assert result.status == doc_resolver_mod.STATUS_UNRESOLVED
    assert result.revision_binding == doc_resolver_mod.BINDING_NONE
    assert any("git_show_failed" in w for w in result.warnings)


def test_doc_resolver_unresolved_sha256_mismatch(tmp_git_repo, monkeypatch):
    """Receipt's commit IS in the repo, but excerpt_sha256 doesn't match
    the canonicalized slice -> unresolved (no silent fallback)."""
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    repo_path, commit, _ = tmp_git_repo
    receipt = _mk_doc_receipt(
        source_path="docs/architecture.md",
        source_commit=commit,
        source_lines="2-3",
        excerpt_sha256="badc0de" + "0" * 57,  # 64 chars, wrong
    )
    result = doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=repo_path,
        _connect=_fake_conn(None, []),
    )
    assert result.status == doc_resolver_mod.STATUS_UNRESOLVED
    assert any("excerpt_sha256_mismatch" in w for w in result.warnings)


def test_doc_resolver_unresolved_no_source_path():
    """Receipt with no source_path -> unresolved + warning (defensive)."""
    receipt = _mk_doc_receipt(source_path=None, source_commit=TEST_COMMIT)
    result = doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=Path("/nonexistent"),
        _connect=_fake_conn(None, []),
    )
    assert result.status == doc_resolver_mod.STATUS_UNRESOLVED


# ===========================================================================
# AG8 — DB error handling
# ===========================================================================


def test_doc_resolver_db_error_falls_through_to_git_snapshot(
    tmp_git_repo, monkeypatch
):
    """psycopg errors -> warning -> tier 2 fallback (NOT retry).

    Per the plan: 'On psycopg errors ... Treat as a grading data point;
    do NOT retry.' But the resolver should still try the git_receipt_snapshot
    path before giving up.
    """
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    repo_path, commit, doc_content = tmp_git_repo
    lines = doc_content.splitlines()
    sliced = "\n".join(lines[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced, indent="")
    expected_sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    receipt = _mk_doc_receipt(
        source_path="docs/architecture.md",
        source_commit=commit,
        source_lines="2-3",
        excerpt_sha256=expected_sha,
    )

    def _broken_connect(*args, **kwargs):
        raise ConnectionRefusedError("postgres not running")

    result = doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=repo_path,
        _connect=_broken_connect,
    )
    # DB error logged as warning, but git_receipt_snapshot path succeeded.
    assert result.status == doc_resolver_mod.STATUS_GIT_RECEIPT_SNAPSHOT
    assert any("db_error" in w for w in result.warnings)


# ===========================================================================
# AG8 — Org-id discipline (I5)
# ===========================================================================


def test_doc_resolver_rejects_empty_org_id(monkeypatch):
    """Plan §3 T4a: org_id REQUIRED, no defaulting."""
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    receipt = _mk_doc_receipt()
    with pytest.raises(ValueError, match="org_id"):
        doc_resolver_mod.resolve_doc_receipt(
            receipt,
            org_id="",
            repo=TEST_REPO,
            repo_path=Path("/x"),
        )


def test_doc_resolver_rejects_empty_repo(monkeypatch):
    """Plan §3 T4a: repo full_name required for doc_id construction."""
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    receipt = _mk_doc_receipt()
    with pytest.raises(ValueError, match="repo"):
        doc_resolver_mod.resolve_doc_receipt(
            receipt,
            org_id=TEST_ORG_ID,
            repo="",
            repo_path=Path("/x"),
        )


# ===========================================================================
# AG8 — Excerpt canonicalization parity with core
# ===========================================================================


def test_excerpt_canonical_matches_core_algorithm():
    """Canonicalization mirror of core/work_packets/qna_receipts.py:
    dedent + rstrip-per-line + ensure-trailing-newline.
    """
    raw = "  def foo():    \n  return 42  \n"
    canon = doc_resolver_mod._excerpt_canonical(raw, indent="  ")
    assert canon == "def foo():\nreturn 42\n"

    # No indent: just rstrip + trailing newline.
    raw = "abc   \ndef\n"
    canon = doc_resolver_mod._excerpt_canonical(raw, indent="")
    assert canon == "abc\ndef\n"

    # Already-canonical content stays canonical.
    raw = "stable\n"
    canon = doc_resolver_mod._excerpt_canonical(raw, indent="")
    assert canon == raw
