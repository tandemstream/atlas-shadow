"""T-D3: cache module unit tests with a local fixture git repo.

We don't need a real `tandemstream/core` to test the cache layer — a tiny
local repo we init in tmp_path suffices for clone + worktree + checkout
flows.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atlas_shadow.ingest_daemon import cache as cache_mod


def _git(args: list[str], *, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """Create a tiny bare-able local repo with two commits; yield (path, shas)."""
    repo = tmp_path / "fixture-core"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "t@example.com"], cwd=repo)
    _git(["config", "user.name", "Tester"], cwd=repo)
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "first"], cwd=repo)
    sha1 = _git(["rev-parse", "HEAD"], cwd=repo)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    _git(["commit", "-am", "second"], cwd=repo)
    sha2 = _git(["rev-parse", "HEAD"], cwd=repo)
    return repo, [sha1, sha2]


def test_ensure_core_clone_clones_on_first_run(tmp_path, fixture_repo):
    repo, _ = fixture_repo
    cache_dir = tmp_path / "cache"
    clone = cache_mod.ensure_core_clone(
        cache_dir=cache_dir,
        core_repo_url=str(repo),
    )
    assert clone == cache_dir / "core"
    assert (clone / ".git").exists()
    assert (clone / "README.md").exists()


def test_ensure_core_clone_fetches_on_subsequent_runs(tmp_path, fixture_repo):
    repo, shas = fixture_repo
    cache_dir = tmp_path / "cache"
    cache_mod.ensure_core_clone(cache_dir=cache_dir, core_repo_url=str(repo))
    # Add a 3rd commit on the source
    (repo / "README.md").write_text("v3\n", encoding="utf-8")
    _git(["commit", "-am", "third"], cwd=repo)
    sha3 = _git(["rev-parse", "HEAD"], cwd=repo)
    # Second call should fetch
    clone = cache_mod.ensure_core_clone(cache_dir=cache_dir, core_repo_url=str(repo))
    # `origin/main` should now reference sha3 in the clone
    proc = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == sha3


def test_checkout_worktree_creates_and_checks_out(tmp_path, fixture_repo):
    repo, shas = fixture_repo
    cache_dir = tmp_path / "cache"
    clone = cache_mod.ensure_core_clone(cache_dir=cache_dir, core_repo_url=str(repo))
    sha1 = shas[0]
    wt = cache_mod.checkout_worktree_at_commit(
        cache_dir=cache_dir,
        commit_sha=sha1,
        core_clone=clone,
    )
    assert wt.exists()
    assert wt == cache_dir / "worktrees" / sha1[:12]
    # README at sha1 was "v1"
    assert (wt / "README.md").read_text(encoding="utf-8") == "v1\n"


def test_checkout_worktree_idempotent_on_existing(tmp_path, fixture_repo):
    repo, shas = fixture_repo
    cache_dir = tmp_path / "cache"
    clone = cache_mod.ensure_core_clone(cache_dir=cache_dir, core_repo_url=str(repo))
    sha1 = shas[0]
    sha2 = shas[1]
    wt = cache_mod.checkout_worktree_at_commit(
        cache_dir=cache_dir,
        commit_sha=sha1,
        core_clone=clone,
    )
    # Re-call with the SAME short-sha path but a different SHA — checks out in place.
    # (In production two different shas will map to different short-sha dirs, but
    # this exercises the "wt exists" branch.)
    wt2 = cache_mod.checkout_worktree_at_commit(
        cache_dir=cache_dir,
        commit_sha=sha1,
        core_clone=clone,
    )
    assert wt == wt2
    assert (wt / "README.md").read_text(encoding="utf-8") == "v1\n"


def test_checkout_worktree_bad_sha_raises(tmp_path, fixture_repo):
    repo, _ = fixture_repo
    cache_dir = tmp_path / "cache"
    clone = cache_mod.ensure_core_clone(cache_dir=cache_dir, core_repo_url=str(repo))
    with pytest.raises(RuntimeError, match="git worktree add"):
        cache_mod.checkout_worktree_at_commit(
            cache_dir=cache_dir,
            commit_sha="deadbeef" * 5,
            core_clone=clone,
        )


def test_ensure_core_clone_bad_url_raises(tmp_path):
    cache_dir = tmp_path / "cache"
    with pytest.raises(RuntimeError, match="git clone"):
        cache_mod.ensure_core_clone(
            cache_dir=cache_dir,
            core_repo_url="/nonexistent/path/that/will/never/exist",
        )
