"""ingest — out-of-band one-off Atlas ingest at a target commit (D4).

This module is reached only via ``make shadow-run COMMIT=<sha>`` (or
``atlas_shadow.cli shadow-run --commit <sha>``). The default-mode benchmark
does NOT touch this code — it queries ``continuous_shadow_org_id`` directly
per amendment 3 of the Phase 2 packet.

## D4 SPEC

The authoritative reference is the dogfood-v2 ingest script in the core
repo:

    products/tandem/packages/python/atlas/scripts/dogfood_v2_smoketest_ingest_code.py

That script's pattern is:

1. Resolve-or-create the target org (`_resolve_or_create_v2_org`).
2. Read the SCIP blob from disk; require a source root for body chunking.
3. Call ``core.code.ingest_scip_upload.ingest_scip_upload(*, org_id,
   repo_url, commit_sha, indexer_version, scip_blob, uploaded_by,
   pack_bundle_revision, incremental=False)`` → returns a
   ``CodeRevisionRef`` with ``code_revision_id``.
4. Call ``core.code.code_chunker.chunk_code_revision(code_revision_id,
   org_id=..., source_root=...)`` → ``ChunkerStats``.
5. Sanity-count files / symbols / edges / chunk_refs for the new
   ``code_revision_id``.

Atlas-shadow has no Atlas Python deps, so it shells out:

- Org creation: ``workspace run org-create NAME=<auto>`` (workspace.yaml
  target at line 510 of the Atlas leaf's workspace.yaml).
- Ingest + chunking: ``.venv/bin/python -m scripts.dogfood_v2_smoketest_ingest_code
  --org-id <uuid> --scip-path <path> --source-root <path>``. That script's
  argparse already accepts ``--org-id`` (line 174 of the script); ``--scip-path``
  and ``--source-root`` default to the dogfood paths but accept overrides,
  so for a non-dogfood commit the caller is responsible for placing the
  SCIP blob and source checkout at the requested paths first.

The wrapper-drift lesson from Phase 2.A: **mirror the dogfood reference's
kwargs and behavior**. The ingest CLI's argparse contract IS the spec.

## Cache by SHA

Successful ingests are recorded in ``.ingest-cache.json`` in the repo root
(gitignored). A second invocation with the same SHA returns the cached
org_id without re-running ingest. The cache stores:

    {
      "<commit_sha>": {
        "org_id": "<uuid>",
        "code_revision_id": "<uuid>",
        "ingested_at": "<iso8601>",
        "scip_path": "<path>",
        "source_root": "<path>",
        "latency_ms": <int>
      }
    }

## Cost tripwires

Per Phase 2 plan amendment 3:

- per-commit ingest wall-time target ≤5 min (flag if >10 min)
- out-of-band smoketest total ≤15 min (flag if >30 min)

The wall time is logged on stderr and recorded in the cache entry. The
runner uses the cache hit when present, so repeat runs amortize the
ingest cost.

## Escalation

If a target commit cannot be ingested because:
- ``workspace run org-create`` fails for permission reasons, OR
- the dogfood ingest script cannot be parameterized cleanly for the
  requested commit (e.g. the SCIP needs a specialized indexer not
  available locally),

…then the caller surfaces the failure rather than silently falling back
to the dogfood org. The default-mode (no ``--commit``) is the graceful
fallback at the CLI layer.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


CACHE_FILENAME = ".ingest-cache.json"

# Per amendment 3 of Phase 2: ingest tripwires.
INGEST_WALL_TIME_TARGET_SEC = 5 * 60
INGEST_WALL_TIME_FLAG_SEC = 10 * 60


@dataclass
class IngestResult:
    commit_sha: str
    org_id: str
    code_revision_id: Optional[str] = None
    ingested_at: str = ""
    scip_path: str = ""
    source_root: str = ""
    latency_ms: int = 0
    cache_hit: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "commit_sha": self.commit_sha,
            "org_id": self.org_id,
            "code_revision_id": self.code_revision_id,
            "ingested_at": self.ingested_at,
            "scip_path": self.scip_path,
            "source_root": self.source_root,
            "latency_ms": self.latency_ms,
            "cache_hit": self.cache_hit,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _cache_path(cwd: Optional[Path] = None) -> Path:
    base = cwd or Path.cwd()
    return base / CACHE_FILENAME


def load_cache(cwd: Optional[Path] = None) -> dict[str, dict]:
    p = _cache_path(cwd)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict[str, dict], cwd: Optional[Path] = None) -> None:
    p = _cache_path(cwd)
    p.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Shell-out helpers
# ---------------------------------------------------------------------------


def _atlas_leaf(core_repo_path: Path) -> Path:
    return core_repo_path / "products" / "tandem" / "packages" / "python" / "atlas"


def _make_org_name(commit_sha: str) -> str:
    """Org name for shell-out. Lowercase + first 12 chars of sha, scoped to
    atlas-shadow so it's distinguishable from the dogfood org.
    """
    short = (commit_sha or "")[:12].lower()
    return f"atlas_shadow_{short}_{uuid.uuid4().hex[:6]}"


def create_org(
    *,
    core_repo_path: Path,
    name: str,
    _subprocess_run=subprocess.run,
) -> str:
    """Shell out to ``workspace run org-create NAME=<name>``. Returns the
    org_id (uuid string).

    workspace.yaml's `org-create` is idempotent: it switches to the org if
    it already exists, otherwise creates + switches. Either way, the
    active org id is then resolvable via ``workspace run org-current``.
    """
    leaf = _atlas_leaf(core_repo_path)
    env = os.environ.copy()
    env["NAME"] = name
    # Create-or-switch
    proc = _subprocess_run(
        ["workspace", "run", "org-create"],
        cwd=str(leaf),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"workspace run org-create NAME={name} failed (rc={proc.returncode}): "
            f"stderr={proc.stderr}"
        )
    # Resolve the org id via `workspace run org-current` (it prints a
    # human-readable line containing the active org id; we parse loosely).
    cur = _subprocess_run(
        ["workspace", "run", "org-current"],
        cwd=str(leaf),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if cur.returncode != 0:
        raise RuntimeError(
            f"workspace run org-current failed: stderr={cur.stderr}"
        )
    org_id = _parse_org_id(cur.stdout)
    if not org_id:
        raise RuntimeError(
            f"could not parse org_id from `workspace run org-current` output: "
            f"{cur.stdout!r}"
        )
    return org_id


def _parse_org_id(text: str) -> Optional[str]:
    """Find a UUID-shaped substring in `workspace run org-current` output."""
    import re

    match = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        text or "",
    )
    return match.group(0) if match else None


def run_dogfood_ingest_script(
    *,
    core_repo_path: Path,
    org_id: str,
    scip_path: Path,
    source_root: Path,
    _subprocess_run=subprocess.run,
) -> dict[str, Any]:
    """Shell out to the dogfood ingest script with --org-id / --scip-path /
    --source-root overrides.

    Returns the parsed JSON payload the script emits on stdout (see
    `dogfood_v2_smoketest_ingest_code.py`'s `main()` — it prints a JSON
    object with `org_id`, `commit_sha`, `code_revision_id`, `latency_ms`,
    `chunk_stats`, and `counts`).

    Raises ``RuntimeError`` on non-zero return or on JSON parse failure.
    """
    leaf = _atlas_leaf(core_repo_path)
    venv_py = leaf / ".venv" / "bin" / "python"
    if not venv_py.exists():
        raise FileNotFoundError(
            f"Atlas venv missing at {venv_py}; run `workspace up` from {leaf}."
        )
    cmd = [
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
    proc = _subprocess_run(
        cmd,
        cwd=str(leaf),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"dogfood_v2_smoketest_ingest_code failed (rc={proc.returncode}): "
            f"stderr={proc.stderr or proc.stdout}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"could not parse ingest JSON output: {exc}; stdout={proc.stdout[:500]}"
        ) from exc


# ---------------------------------------------------------------------------
# Top-level entry point used by cli.shadow_run --commit
# ---------------------------------------------------------------------------


def ensure_org_for_commit(
    *,
    commit_sha: str,
    core_repo_path: Path,
    scip_path: Optional[Path] = None,
    source_root: Optional[Path] = None,
    template_org_id: Optional[str] = None,
    cwd: Optional[Path] = None,
    _create_org=create_org,
    _run_ingest=run_dogfood_ingest_script,
) -> dict[str, Any]:
    """Ensure a fresh Atlas org is ingested at ``commit_sha``; return the
    cache record.

    Order:
      1. Cache hit? Return record with ``cache_hit=True``.
      2. Resolve `scip_path` / `source_root` (defaults derived from sha
         per the dogfood naming convention: ``/tmp/dogfood-v2-<sha>.scip``
         and ``/tmp/dogfood-v2-playground-<sha>``).
      3. Create org via ``workspace run org-create`` (or use
         ``template_org_id`` if provided).
      4. Run the dogfood ingest script with overrides.
      5. Record + return cache entry.

    Wall-time tripwires emit a stderr warning at >5min target and >10min
    flag thresholds. The runner doesn't abort — even slow ingests are
    valid; the warning surfaces them for postmortem analysis.
    """
    cache = load_cache(cwd)
    hit = cache.get(commit_sha)
    if hit:
        return {**hit, "cache_hit": True, "commit_sha": commit_sha}

    short = (commit_sha or "")[:12].lower()
    scip_path = scip_path or Path(f"/tmp/dogfood-v2-{short}.scip")
    source_root = source_root or Path(f"/tmp/dogfood-v2-playground-{short}")

    started = time.perf_counter()

    if template_org_id:
        org_id = template_org_id
    else:
        name = _make_org_name(commit_sha)
        org_id = _create_org(core_repo_path=core_repo_path, name=name)

    ingest_payload = _run_ingest(
        core_repo_path=core_repo_path,
        org_id=org_id,
        scip_path=scip_path,
        source_root=source_root,
    )
    elapsed_sec = time.perf_counter() - started
    latency_ms = int(elapsed_sec * 1000)

    if elapsed_sec > INGEST_WALL_TIME_FLAG_SEC:
        import sys as _sys

        print(
            f"[atlas-shadow] WARN: ingest wall-time {elapsed_sec:.1f}s "
            f"exceeded {INGEST_WALL_TIME_FLAG_SEC}s flag threshold "
            f"(per Phase 2 amendment 3)",
            file=_sys.stderr,
        )
    elif elapsed_sec > INGEST_WALL_TIME_TARGET_SEC:
        import sys as _sys

        print(
            f"[atlas-shadow] NOTE: ingest wall-time {elapsed_sec:.1f}s "
            f"exceeded {INGEST_WALL_TIME_TARGET_SEC}s target",
            file=_sys.stderr,
        )

    code_revision_id = ingest_payload.get("code_revision_id")

    record = {
        "commit_sha": commit_sha,
        "org_id": str(org_id),
        "code_revision_id": str(code_revision_id) if code_revision_id else None,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "scip_path": str(scip_path),
        "source_root": str(source_root),
        "latency_ms": latency_ms,
        "cache_hit": False,
        "ingest_counts": ingest_payload.get("counts"),
        "chunk_stats": ingest_payload.get("chunk_stats"),
    }
    cache[commit_sha] = {k: v for k, v in record.items() if k != "cache_hit"}
    save_cache(cache, cwd)
    return record
