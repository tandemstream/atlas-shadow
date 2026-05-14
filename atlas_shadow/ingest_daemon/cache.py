"""cache — clone-or-fetch ``tandemstream/core`` into a local checkout dir.

The daemon needs a working tree of ``tandemstream/core`` at the target
commit for two purposes:

  1. Run ``scip-python`` against it (the SCIP build needs source files).
  2. Pass the path as ``--source-root`` to the dogfood ingest CLI for the
     body-chunking pass.

We keep a single bare-or-worktree clone under ``cache_dir/core`` and
checkout per-commit worktrees under ``cache_dir/worktrees/<short-sha>``.
This matches the dogfood naming convention used in
``atlas_shadow.ingest.ensure_org_for_commit`` (``/tmp/dogfood-v2-playground-<sha>``).

Public surface:
- :func:`ensure_core_clone` — make sure ``cache_dir/core`` is a working
  clone of ``core_repo_url``; create on first call, ``git fetch origin`` on
  subsequent calls.
- :func:`checkout_worktree_at_commit` — create or refresh a worktree at
  ``cache_dir/worktrees/<short-sha>`` pointing at ``commit_sha``.

The caller is responsible for resolving the path it gets back into the
SCIP build command and the dogfood ingest CLI's ``--source-root`` arg.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable


def _short_sha(commit_sha: str) -> str:
    return (commit_sha or "").strip().lower()[:12]


def ensure_core_clone(
    *,
    cache_dir: Path,
    core_repo_url: str,
    _subprocess_run: Callable = subprocess.run,
) -> Path:
    """Ensure ``cache_dir/core`` is a clone of ``core_repo_url``.

    Returns the path to the clone. On first run, performs ``git clone``;
    on subsequent runs, performs ``git fetch origin``.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    clone = cache_dir / "core"
    if not (clone / ".git").exists():
        proc = _subprocess_run(
            ["git", "clone", core_repo_url, str(clone)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone {core_repo_url} → {clone} failed (rc={proc.returncode}): "
                f"stderr={proc.stderr}"
            )
        return clone
    proc = _subprocess_run(
        ["git", "fetch", "--prune", "origin"],
        cwd=str(clone),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git fetch origin in {clone} failed (rc={proc.returncode}): "
            f"stderr={proc.stderr}"
        )
    return clone


def checkout_worktree_at_commit(
    *,
    cache_dir: Path,
    commit_sha: str,
    core_clone: Path,
    _subprocess_run: Callable = subprocess.run,
) -> Path:
    """Create or refresh a git worktree of ``core_clone`` at ``commit_sha``.

    Worktree path: ``cache_dir/worktrees/<short-sha>``. If a worktree
    already exists there, we ``git checkout <sha>`` inside it; otherwise
    we ``git worktree add``.

    Returns the worktree path.
    """
    short = _short_sha(commit_sha)
    if not short:
        raise ValueError("commit_sha must be non-empty")
    worktrees_root = cache_dir / "worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_root / short
    if wt_path.exists():
        # Already a worktree — just checkout
        proc = _subprocess_run(
            ["git", "checkout", "--detach", commit_sha],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git checkout {commit_sha} in {wt_path} failed "
                f"(rc={proc.returncode}): stderr={proc.stderr}"
            )
        return wt_path
    # Fresh worktree
    proc = _subprocess_run(
        ["git", "worktree", "add", "--detach", str(wt_path), commit_sha],
        cwd=str(core_clone),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git worktree add {wt_path} @ {commit_sha} failed "
            f"(rc={proc.returncode}): stderr={proc.stderr}"
        )
    return wt_path


def remove_worktree(
    *,
    cache_dir: Path,
    commit_sha: str,
    core_clone: Path,
    _subprocess_run: Callable = subprocess.run,
) -> None:
    """Best-effort worktree cleanup. Used by the runbook's manual cleanup
    flow; never called by the worker (cache hygiene is operator-driven
    per plan §12 open questions)."""
    short = _short_sha(commit_sha)
    wt_path = cache_dir / "worktrees" / short
    if not wt_path.exists():
        return
    _subprocess_run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=str(core_clone),
        capture_output=True,
        text=True,
        check=False,
    )
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)
