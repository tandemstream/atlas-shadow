from __future__ import annotations

import hashlib
import subprocess

from atlas_shadow.ingest_daemon import code_snapshot as code_snapshot_mod
from atlas_shadow.ingest_daemon import doc_resolver as doc_resolver_mod
from atlas_shadow.ingest_daemon.grader_service import PacketReceipt


def _mk_receipt(**kwargs) -> PacketReceipt:
    defaults = dict(
        question_id="q1",
        question="code q",
        oracle_claim="claim",
        oracle_excerpt="line two\nline three\n",
        source_path="src/example.py",
        source_lines="2-3",
        excerpt_sha256="",
        source_commit="HEAD",
    )
    defaults.update(kwargs)
    return PacketReceipt(**defaults)


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, timeout=10)
    path = repo / "src" / "example.py"
    path.parent.mkdir(parents=True)
    body = "line one\nline two\nline three\nline four\n"
    path.write_text(body, encoding="utf-8")
    schema = repo / "products" / "tandem" / "packages" / "python" / "atlas" / "schema_v0.2.sql"
    schema.parent.mkdir(parents=True)
    schema_body = "create table artifact_chunks (\n  chunk_id uuid,\n  artifact_id uuid\n);\n"
    schema.write_text(schema_body, encoding="utf-8")
    chunker = repo / "products" / "tandem" / "packages" / "python" / "atlas" / "core" / "ingest" / "chunkers" / "markdown_chunker.py"
    chunker.parent.mkdir(parents=True)
    chunker_body = "class MarkdownChunker:\n    def chunk(self, text):\n        return text.splitlines()\n"
    chunker.write_text(chunker_body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, timeout=10)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    return repo, commit, body


def test_code_snapshot_hash_match(tmp_path):
    repo, commit, body = _git_repo(tmp_path)
    sliced = "\n".join(body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    receipt = _mk_receipt(
        source_commit=commit,
        excerpt_sha256=hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    )

    result = code_snapshot_mod.resolve_code_receipt_snapshot(
        receipt,
        repo_path=repo,
    )

    assert result.status == code_snapshot_mod.STATUS_MATCH
    assert result.hash_match is True
    assert result.raw_text_len == len(canon)
    assert result.resolved_sha256 == receipt.excerpt_sha256


def test_code_snapshot_source_missing(tmp_path):
    repo, commit, _body = _git_repo(tmp_path)
    receipt = _mk_receipt(source_commit=commit, source_path="missing.py")

    result = code_snapshot_mod.resolve_code_receipt_snapshot(
        receipt,
        repo_path=repo,
    )

    assert result.status == code_snapshot_mod.STATUS_SOURCE_MISSING
    assert result.hash_match is None


def test_code_snapshot_resolves_atlas_leaf_schema_path(tmp_path):
    """Receipts may cite Atlas package paths from the package root.

    Snapshot checks run from the monorepo root, so `schema_v0.2.sql`
    should resolve to `products/tandem/packages/python/atlas/schema_v0.2.sql`.
    """
    repo, commit, _body = _git_repo(tmp_path)
    full_path = "products/tandem/packages/python/atlas/schema_v0.2.sql"
    body = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{full_path}"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout
    sliced = "\n".join(body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    receipt = _mk_receipt(
        source_commit=commit,
        source_path="schema_v0.2.sql",
        source_lines="2-3",
        excerpt_sha256=hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    )

    result = code_snapshot_mod.resolve_code_receipt_snapshot(
        receipt,
        repo_path=repo,
    )

    assert result.status == code_snapshot_mod.STATUS_MATCH
    assert result.path == full_path


def test_code_snapshot_resolves_atlas_leaf_core_path(tmp_path):
    """Atlas package-relative `core/...` paths should also resolve."""
    repo, commit, _body = _git_repo(tmp_path)
    full_path = (
        "products/tandem/packages/python/atlas/core/ingest/chunkers/"
        "markdown_chunker.py"
    )
    body = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{full_path}"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout
    sliced = "\n".join(body.splitlines()[0:1])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    receipt = _mk_receipt(
        source_commit=commit,
        source_path="core/ingest/chunkers/markdown_chunker.py",
        source_lines="1-1",
        excerpt_sha256=hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    )

    result = code_snapshot_mod.resolve_code_receipt_snapshot(
        receipt,
        repo_path=repo,
    )

    assert result.status == code_snapshot_mod.STATUS_MATCH
    assert result.path == full_path


def test_code_snapshot_no_line_range(tmp_path):
    repo, commit, _body = _git_repo(tmp_path)
    receipt = _mk_receipt(source_commit=commit, source_lines="not-a-range")

    result = code_snapshot_mod.resolve_code_receipt_snapshot(
        receipt,
        repo_path=repo,
    )

    assert result.status == code_snapshot_mod.STATUS_NO_LINE_RANGE


# ─── PR #15: resolve_code_receipt_run_snapshot ────────────────────────


def _git_repo_with_second_commit(tmp_path):
    """Variant of ``_git_repo`` that adds a second commit which edits
    the same line range. Returns (repo, receipt_commit, run_commit,
    receipt_body, run_body)."""
    repo, receipt_commit, receipt_body = _git_repo(tmp_path)
    path = repo / "src" / "example.py"
    # Rewrite lines 2-3 to different content. The path still exists at
    # the run commit; the line numbers still point at *something*; but
    # those bytes now differ from what the receipt's excerpt_sha256
    # described.
    new_body = "line one\nedited two\nedited three\nline four\n"
    path.write_text(new_body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    subprocess.run(
        ["git", "commit", "-q", "-m", "edit lines 2-3"],
        cwd=repo, check=True, timeout=10,
    )
    run_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()
    return repo, receipt_commit, run_commit, receipt_body, new_body


def test_run_snapshot_match_when_no_drift(tmp_path):
    """Run-commit snapshot returns ``run_commit_hash_match`` when the
    cited line range still renders the receipt's excerpt at the run
    commit. (The grading commit == the receipt commit, or no edits
    intervened.)"""
    repo, commit, body = _git_repo(tmp_path)
    sliced = "\n".join(body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    receipt = _mk_receipt(
        source_commit=commit,
        excerpt_sha256=hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    )

    result = code_snapshot_mod.resolve_code_receipt_run_snapshot(
        receipt,
        repo_path=repo,
        run_commit=commit,
    )

    assert result.status == code_snapshot_mod.STATUS_RUN_COMMIT_MATCH
    assert result.hash_match is True


def test_run_snapshot_mismatch_when_file_edited(tmp_path):
    """The q12 content-dedup case: receipt-commit snapshot matches, but
    the file was edited between receipt commit and run commit so the
    same line range now renders different bytes. Status:
    ``run_commit_hash_mismatch``."""
    repo, receipt_commit, run_commit, receipt_body, _run_body = (
        _git_repo_with_second_commit(tmp_path)
    )
    sliced = "\n".join(receipt_body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    receipt = _mk_receipt(
        source_commit=receipt_commit,
        excerpt_sha256=hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    )

    # Receipt-commit snapshot should still match (sanity check).
    src_result = code_snapshot_mod.resolve_code_receipt_snapshot(
        receipt,
        repo_path=repo,
    )
    assert src_result.status == code_snapshot_mod.STATUS_MATCH

    # Run-commit snapshot at the later commit should NOT match.
    run_result = code_snapshot_mod.resolve_code_receipt_run_snapshot(
        receipt,
        repo_path=repo,
        run_commit=run_commit,
    )
    assert run_result.status == code_snapshot_mod.STATUS_RUN_COMMIT_MISMATCH
    assert run_result.hash_match is False
    # Useful diagnostic: the resolved sha at the run commit is captured
    # so consumers can confirm what atlas actually saw.
    assert run_result.resolved_sha256 is not None
    assert run_result.resolved_sha256 != receipt.excerpt_sha256


def test_run_snapshot_source_missing_when_path_deleted(tmp_path):
    """The path itself doesn't exist at the run commit — different
    status from line drift (the file is gone, not just edited)."""
    repo, receipt_commit, receipt_body = _git_repo(tmp_path)
    # Delete the file in a second commit.
    (repo / "src" / "example.py").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, timeout=10)
    subprocess.run(
        ["git", "commit", "-q", "-m", "delete example.py"],
        cwd=repo, check=True, timeout=10,
    )
    run_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()

    sliced = "\n".join(receipt_body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    receipt = _mk_receipt(
        source_commit=receipt_commit,
        excerpt_sha256=hashlib.sha256(canon.encode("utf-8")).hexdigest(),
    )

    result = code_snapshot_mod.resolve_code_receipt_run_snapshot(
        receipt,
        repo_path=repo,
        run_commit=run_commit,
    )
    assert result.status == code_snapshot_mod.STATUS_RUN_COMMIT_SOURCE_MISSING


def test_run_snapshot_not_applicable_when_run_commit_omitted(tmp_path):
    """Callers without a run_commit available pass None; the resolver
    short-circuits without hitting git. Used by the grade-batch path
    when --commit-sha isn't carried through to grading."""
    repo, commit, _body = _git_repo(tmp_path)
    receipt = _mk_receipt(source_commit=commit)
    result = code_snapshot_mod.resolve_code_receipt_run_snapshot(
        receipt,
        repo_path=repo,
        run_commit=None,
    )
    assert result.status == code_snapshot_mod.STATUS_NOT_APPLICABLE


def test_run_snapshot_no_line_range(tmp_path):
    """Same precondition as the receipt-commit resolver — receipts
    without a parseable line range get ``no_line_range``."""
    repo, commit, _body = _git_repo(tmp_path)
    receipt = _mk_receipt(source_commit=commit, source_lines="invalid")
    result = code_snapshot_mod.resolve_code_receipt_run_snapshot(
        receipt,
        repo_path=repo,
        run_commit=commit,
    )
    assert result.status == code_snapshot_mod.STATUS_NO_LINE_RANGE
