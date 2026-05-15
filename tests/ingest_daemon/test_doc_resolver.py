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
from dataclasses import dataclass
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


# ===========================================================================
# AG8 — DB boundary deepening (hotspot 2)
# ===========================================================================


@dataclass
class _RecordingConnect:
    """Fake psycopg2.connect that records args + delegates to a query stub."""

    connect_calls: list = None
    execute_calls: list = None
    rows_for_artifacts: object = None
    rows_for_chunks: list = None

    def __post_init__(self):
        self.connect_calls = []
        self.execute_calls = []
        if self.rows_for_chunks is None:
            self.rows_for_chunks = []

    def __call__(self, *args, **kwargs):
        self.connect_calls.append({"args": args, "kwargs": kwargs})

        class _Cur:
            def __init__(self2):
                self2._last = ""

            def execute(self2, query, params=None):
                self.execute_calls.append({"query": query, "params": params})
                self2._last = query

            def fetchone(self2):
                if "FROM artifacts" in self2._last:
                    return self.rows_for_artifacts
                return None

            def fetchall(self2):
                if "FROM artifact_chunks" in self2._last:
                    return list(self.rows_for_chunks)
                return []

            def __enter__(self2):
                return self2

            def __exit__(self2, *a):
                return False

        class _Conn:
            def cursor(self2):
                return _Cur()

            def close(self2):
                pass

        return _Conn()


def test_doc_resolver_builds_correct_doc_id_format(monkeypatch):
    """Primary lookup must query for doc_id=f'<repo>@<commit>:<path>'.

    This shape is the canonical Path-A / SCIP-path doc_id that P1's T1
    reframe established in `core/code/ingest.py`. If atlas-shadow drifts
    from this shape, the DB lookup silently misses every doc and every
    doc receipt grades via the git_receipt_snapshot fallback (or worse,
    unresolved).
    """
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    rec = _RecordingConnect(rows_for_artifacts=None)  # miss -> we just inspect the query
    receipt = _mk_doc_receipt(
        source_path="docs/architecture.md",
        source_commit="deadbeef" * 5,  # 40 chars
    )
    doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo="tandemstream/core",
        repo_path=Path("/nonexistent"),
        _connect=rec,
    )
    # First execute call hits artifacts.
    art_call = next(c for c in rec.execute_calls if "FROM artifacts" in c["query"])
    # params[1] is the doc_id (params[0] is org_id).
    assert art_call["params"][0] == TEST_ORG_ID
    assert art_call["params"][1] == (
        f"tandemstream/core@{'deadbeef' * 5}:docs/architecture.md"
    )


def test_doc_resolver_org_id_is_first_where_predicate(monkeypatch):
    """Both primary queries must scope `org_id` as the FIRST WHERE
    predicate (mirrors --org-id required everywhere; prevents accidental
    cross-tenant reads).
    """
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    rec = _RecordingConnect(
        rows_for_artifacts=("0" * 36, {"chunk_headings": {}}),
        rows_for_chunks=[("c0", 0, "x", 0, 1)],
    )
    receipt = _mk_doc_receipt()
    doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=Path("/nonexistent"),
        _connect=rec,
    )
    # Both queries should have `org_id` before any other column in WHERE.
    for c in rec.execute_calls:
        q = c["query"]
        # Strip whitespace; find the first WHERE.
        where_idx = q.upper().find("WHERE")
        assert where_idx != -1, f"query missing WHERE: {q}"
        after_where = q[where_idx + len("WHERE"):]
        # org_id appears before any other equality predicate.
        org_id_idx = after_where.find("org_id")
        assert org_id_idx >= 0, f"query missing org_id predicate: {q}"
        # No other named column appears before org_id (we accept = % s pattern).
        before_org = after_where[:org_id_idx]
        assert "doc_id" not in before_org, f"doc_id before org_id in: {q}"
        assert "artifact_id" not in before_org, f"artifact_id before org_id in: {q}"


def test_doc_resolver_env_var_fallback_chain(monkeypatch):
    """ATLAS_SHADOW_DOC_RESOLVER_DB_URL > ATLAS_ADMIN_DB_URL > ATLAS_DB_URL."""
    monkeypatch.delenv("ATLAS_SHADOW_DOC_RESOLVER_DB_URL", raising=False)
    monkeypatch.delenv("ATLAS_ADMIN_DB_URL", raising=False)
    monkeypatch.delenv("ATLAS_DB_URL", raising=False)

    # Only ATLAS_DB_URL set.
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://db_url/x")
    assert doc_resolver_mod._resolve_db_url() == "postgresql://db_url/x"

    # ATLAS_ADMIN_DB_URL takes precedence.
    monkeypatch.setenv("ATLAS_ADMIN_DB_URL", "postgresql://admin/x")
    assert doc_resolver_mod._resolve_db_url() == "postgresql://admin/x"

    # DOC_RESOLVER_DB_URL takes precedence over both.
    monkeypatch.setenv(
        "ATLAS_SHADOW_DOC_RESOLVER_DB_URL", "postgresql://resolver/x"
    )
    assert doc_resolver_mod._resolve_db_url() == "postgresql://resolver/x"


def test_doc_resolver_no_env_returns_none():
    """All three env vars unset -> _resolve_db_url returns None."""
    import os
    saved = {k: os.environ.pop(k, None) for k in (
        "ATLAS_SHADOW_DOC_RESOLVER_DB_URL",
        "ATLAS_ADMIN_DB_URL",
        "ATLAS_DB_URL",
    )}
    try:
        assert doc_resolver_mod._resolve_db_url() is None
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@pytest.mark.parametrize(
    "env_value,expected_ms",
    [
        (None, 10000),       # default
        ("5000", 5000),      # custom
        ("0", 10000),        # zero falls back to default
        ("-100", 10000),     # negative falls back to default
        ("not-a-number", 10000),  # malformed falls back
        ("250000", 250000),  # generous override allowed
    ],
)
def test_doc_resolver_statement_timeout_configurable(monkeypatch, env_value, expected_ms):
    """Statement timeout is configurable via env var; bad values fall
    back to the 10000ms default rather than passing garbage to psycopg.
    """
    monkeypatch.delenv("ATLAS_SHADOW_DOC_RESOLVER_QUERY_TIMEOUT_MS", raising=False)
    if env_value is not None:
        monkeypatch.setenv("ATLAS_SHADOW_DOC_RESOLVER_QUERY_TIMEOUT_MS", env_value)
    assert doc_resolver_mod._statement_timeout_ms() == expected_ms


def test_doc_resolver_connect_args_carry_timeouts(monkeypatch):
    """The psycopg2.connect call gets connect_timeout=5 + an `options`
    string containing the configured statement_timeout.
    """
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    monkeypatch.setenv("ATLAS_SHADOW_DOC_RESOLVER_QUERY_TIMEOUT_MS", "7777")
    rec = _RecordingConnect(rows_for_artifacts=None)
    receipt = _mk_doc_receipt()
    doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=Path("/nonexistent"),
        _connect=rec,
    )
    assert len(rec.connect_calls) == 1
    kwargs = rec.connect_calls[0]["kwargs"]
    assert kwargs.get("connect_timeout") == 5
    options = kwargs.get("options", "")
    assert "statement_timeout=7777" in options


def test_doc_resolver_no_db_url_warns_and_tries_git(tmp_git_repo):
    """No env vars set -> primary lookup skipped with a warning ->
    fallback to git_receipt_snapshot.
    """
    import os
    saved = {k: os.environ.pop(k, None) for k in (
        "ATLAS_SHADOW_DOC_RESOLVER_DB_URL",
        "ATLAS_ADMIN_DB_URL",
        "ATLAS_DB_URL",
    )}
    try:
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
        result = doc_resolver_mod.resolve_doc_receipt(
            receipt,
            org_id=TEST_ORG_ID,
            repo=TEST_REPO,
            repo_path=repo_path,
            _connect=lambda *a, **kw: pytest.fail("must not connect when no DB URL"),
        )
        assert result.status == doc_resolver_mod.STATUS_GIT_RECEIPT_SNAPSHOT
        assert any(
            "no DB URL configured" in w or "DB URL" in w
            for w in result.warnings
        )
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_doc_resolver_psycopg2_missing_warns_and_tries_git(tmp_git_repo, monkeypatch):
    """_connect=None (simulates missing psycopg2) -> warning + git
    fallback. Resolver MUST NOT crash when the optional driver isn't
    installed.
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

    # Patch out psycopg2 import in the resolver module so the resolver
    # treats it as missing (this is the "psycopg2 not installed" path).
    import sys
    saved_module = sys.modules.pop("psycopg2", None)
    sys.modules["psycopg2"] = None  # type: ignore
    try:
        result = doc_resolver_mod.resolve_doc_receipt(
            receipt,
            org_id=TEST_ORG_ID,
            repo=TEST_REPO,
            repo_path=repo_path,
        )
    finally:
        if saved_module is not None:
            sys.modules["psycopg2"] = saved_module
        else:
            sys.modules.pop("psycopg2", None)

    assert result.status == doc_resolver_mod.STATUS_GIT_RECEIPT_SNAPSHOT
    assert any("psycopg2" in w for w in result.warnings)


def test_doc_resolver_picks_chunk_best_matching_oracle_excerpt(monkeypatch):
    """When multiple chunks belong to the same doc, the resolver picks
    the chunk whose raw_text contains the longest prefix of the receipt's
    oracle_excerpt — that's the chunk most likely to be the cited region.
    """
    monkeypatch.setenv("ATLAS_DB_URL", "postgresql://test/fake")
    rec = _RecordingConnect(
        rows_for_artifacts=("artifact-1", {"chunk_headings": {
            "0": {"heading_path": ["A"], "heading_level": 1},
            "1": {"heading_path": ["B"], "heading_level": 2},
            "2": {"heading_path": ["C"], "heading_level": 2},
        }}),
        rows_for_chunks=[
            ("chunk-0", 0, "# A\n\nIntroduction text here.\n", 0, 30),
            ("chunk-1", 1, "## B\n\nThe needle target line we want to match.\n", 30, 80),
            ("chunk-2", 2, "## C\n\nUnrelated other content.\n", 80, 110),
        ],
    )
    receipt = _mk_doc_receipt(
        oracle_excerpt="The needle target line we want to match.",
    )
    result = doc_resolver_mod.resolve_doc_receipt(
        receipt,
        org_id=TEST_ORG_ID,
        repo=TEST_REPO,
        repo_path=Path("/nonexistent"),
        _connect=rec,
    )
    assert result.chunk_id == "chunk-1"
    assert result.heading_path == ["B"]
    assert result.heading_level == 2
