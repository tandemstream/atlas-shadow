"""scip_builder — shell out to ``scip-python`` to emit a SCIP blob.

The daemon never imports ``core.code.*``. Per amendment #2, the entire
ingest pipeline is a shell-out to ``scripts.dogfood_v2_smoketest_ingest_code``
in the Atlas leaf. This module's role is just the first half: get a
SCIP file on disk so the dogfood CLI can read it.

Public surface:
- :func:`build_scip` — invoke ``scip-python`` against a source tree at
  a target commit; write the SCIP index to a deterministic path; return
  that path.
- :func:`dogfood_ingest_argv` — assemble the argv for the dogfood ingest
  CLI subprocess. **Argv shape is the contract** — the worker test
  (``test_worker.py``) asserts argv parity against the dogfood CLI's
  argparse declaration (per amendment decision #10).
- :func:`run_dogfood_ingest` — invoke the dogfood ingest CLI subprocess
  inside the Atlas leaf via the Atlas venv's Python; parse stdout JSON
  and return the payload dict.

The Atlas leaf path is resolved from ``core_repo_path`` (per amendment
decision #8, no separate config key — the path is structurally fixed
under the Atlas leaf at
``products/tandem/packages/python/atlas/scripts/dogfood_v2_smoketest_ingest_code.py``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable


def atlas_leaf(core_repo_path: Path) -> Path:
    """Path to the Atlas leaf (where the dogfood ingest script + venv live)."""
    return core_repo_path / "products" / "tandem" / "packages" / "python" / "atlas"


def atlas_venv_python(core_repo_path: Path) -> Path:
    """Path to the Atlas venv's Python interpreter."""
    return atlas_leaf(core_repo_path) / ".venv" / "bin" / "python"


def scip_output_path(*, cache_dir: Path, commit_sha: str) -> Path:
    """Deterministic SCIP-blob path for a commit.

    Note: ``commit_sha`` is the full 40-char hex (we don't shorten here —
    keeps every blob path uniquely identifiable in cache dirs).
    """
    return cache_dir / "scip" / f"core-{commit_sha.lower()}.scip"


def build_scip(
    *,
    source_root: Path,
    commit_sha: str,
    cache_dir: Path,
    indexer_version: str,
    timeout_seconds: int = 1200,
    _subprocess_run: Callable = subprocess.run,
) -> Path:
    """Build a SCIP index for ``source_root`` at ``commit_sha`` using
    ``scip-python``; return the path to the written ``.scip`` file.

    The current behavior shells out via ``scip-python index --output
    <path>`` against the source tree, matching the dogfood-v2 reference's
    canonical indexer invocation. ``indexer_version`` is informational —
    if the locally-installed ``scip-python`` differs, the Atlas-side
    ``ingest_scip_upload`` row will record the configured version (see
    plan §10 and qa:q10 idempotency-key discussion).

    Args:
        source_root: Path to the checked-out source tree (e.g., the
          worktree returned by :func:`cache.checkout_worktree_at_commit`).
        commit_sha: 40-char hex SHA being indexed (for SCIP filename).
        cache_dir: where to drop the ``.scip`` file (under ``scip/`` subdir).
        indexer_version: informational; logged on stderr if scip-python's
          version differs.
        timeout_seconds: kill the SCIP build if it runs longer.

    Raises:
        FileNotFoundError if ``scip-python`` isn't on PATH.
        RuntimeError if the build exits non-zero or times out.

    Returns the path to the produced SCIP file.
    """
    scip_out = scip_output_path(cache_dir=cache_dir, commit_sha=commit_sha)
    scip_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "scip-python",
        "index",
        "--output",
        str(scip_out),
        "--project-name",
        "tandemstream/core",
    ]
    try:
        proc = _subprocess_run(
            cmd,
            cwd=str(source_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"scip-python not found on PATH; install it before starting the daemon. "
            f"original error: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"scip-python timed out after {timeout_seconds}s building "
            f"{source_root} @ {commit_sha}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"scip-python failed (rc={proc.returncode}) building {source_root} "
            f"@ {commit_sha}: stderr={proc.stderr[:2000]}"
        )
    if not scip_out.exists():
        raise RuntimeError(
            f"scip-python returned 0 but did not produce {scip_out}; "
            f"stdout={proc.stdout[:500]} stderr={proc.stderr[:500]}"
        )
    return scip_out


def dogfood_ingest_argv(
    *,
    core_repo_path: Path,
    org_id: str,
    scip_path: Path,
    source_root: Path,
) -> list[str]:
    """Assemble the argv for the dogfood ingest CLI subprocess.

    Shape mirrors the dogfood script's argparse contract:
        --org-id <uuid>
        --scip-path <path>
        --source-root <path>

    The script's other settings (REPO_URL, INGEST_SHA, INDEXER_VERSION,
    PACK_BUNDLE_REVISION) are module-level constants in the dogfood
    script — NOT CLI flags. The daemon accepts that; Atlas treats each
    ingest under the same constants but each distinct SCIP blob produces
    a distinct ``code_revision_id`` (the daemon records that id in the
    ledger + state file).

    This argv shape is the contract asserted by
    ``tests/ingest_daemon/test_worker.py::test_argv_matches_dogfood_argparse``
    (amendment decision #10).
    """
    venv_py = atlas_venv_python(core_repo_path)
    return [
        str(venv_py),
        "-m",
        "scripts.dogfood_v2_smoketest_ingest_code",
        "--org-id",
        org_id,
        "--scip-path",
        str(scip_path),
        "--source-root",
        str(source_root),
    ]


def run_dogfood_ingest(
    *,
    core_repo_path: Path,
    org_id: str,
    scip_path: Path,
    source_root: Path,
    timeout_seconds: int = 1800,
    _subprocess_run: Callable = subprocess.run,
) -> dict[str, Any]:
    """Run the dogfood ingest CLI; return parsed stdout JSON.

    Mirrors the structural pattern of
    ``atlas_shadow.ingest.run_dogfood_ingest_script`` (Phase 2 D4) so the
    two callers can share future fixes.

    Returns the parsed JSON payload (``org_id``, ``commit_sha``,
    ``code_revision_id``, ``latency_ms``, ``chunk_stats``, ``counts``,
    etc.). Raises ``RuntimeError`` on non-zero exit / parse failure.
    """
    leaf = atlas_leaf(core_repo_path)
    venv_py = atlas_venv_python(core_repo_path)
    if not venv_py.exists():
        raise FileNotFoundError(
            f"Atlas venv missing at {venv_py}; run `workspace up` from {leaf}."
        )
    argv = dogfood_ingest_argv(
        core_repo_path=core_repo_path,
        org_id=org_id,
        scip_path=scip_path,
        source_root=source_root,
    )
    try:
        proc = _subprocess_run(
            argv,
            cwd=str(leaf),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"dogfood ingest CLI timed out after {timeout_seconds}s for "
            f"org_id={org_id} scip_path={scip_path}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"dogfood_v2_smoketest_ingest_code failed (rc={proc.returncode}): "
            f"stderr={(proc.stderr or proc.stdout)[:2000]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"could not parse dogfood ingest stdout as JSON: {exc}; "
            f"stdout={proc.stdout[:500]}"
        ) from exc
