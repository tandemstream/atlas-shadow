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


def test_code_snapshot_no_line_range(tmp_path):
    repo, commit, _body = _git_repo(tmp_path)
    receipt = _mk_receipt(source_commit=commit, source_lines="not-a-range")

    result = code_snapshot_mod.resolve_code_receipt_snapshot(
        receipt,
        repo_path=repo,
    )

    assert result.status == code_snapshot_mod.STATUS_NO_LINE_RANGE
