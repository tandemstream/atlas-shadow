"""runner — shells out to `workspace run atlas-query` for each receipt.

The runner is the bridge between atlas-shadow (pure Python) and Atlas (the
sibling `tandemstream/core` checkout). For each :class:`Receipt`, it builds
the `workspace run atlas-query -- ...` argv, runs it from the Atlas leaf,
captures stdout, parses the JSON payload, and emits a per-question response
record.

The wrapper itself is implemented in
`tandemstream/core` at
`products/tandem/packages/python/atlas/scripts/workspace_atlas_query.py`.
Its kwargs + output schema are mirrored here so any drift surfaces as a
test failure rather than silent data corruption.

Default-mode runs use `continuous_shadow_org_id` from `shadow-config.yaml`.
Out-of-band runs (`--commit <sha>`) invoke `atlas_shadow.ingest` first and
substitute the freshly-created org id.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .parser import Receipt


@dataclass(frozen=True)
class AtlasResponse:
    """Normalized response from `workspace run atlas-query`."""

    tool_used: str
    answer_text: str
    raw_result: Any
    evidence_keys: list[str]
    atlas_latency_ms: int
    request_id: str
    commit: str
    stderr: str = ""
    returncode: int = 0
    exception: Optional[str] = None


@dataclass(frozen=True)
class ShadowResponse:
    """Per-question record emitted to atlas-qa-shadow.jsonl (pre-grade)."""

    question_id: str
    question: str
    fixture_id: str
    atlas_response: AtlasResponse
    wall_time_ms: int
    captured_at: str
    org_id: str
    tool: str
    extra: dict = field(default_factory=dict)


def _workspace_launcher() -> list[str]:
    """Resolve the workspace CLI launcher.

    Order of precedence:
      1. ``ATLAS_SHADOW_WORKSPACE_CMD`` env var (space-split argv).
      2. ``WORKSPACE_PY`` + ``WORKSPACE_VENV_PY`` env vars (Atlas shell-
         function convention) — if both set, return
         ``[$WORKSPACE_VENV_PY, $WORKSPACE_PY]``.
      3. Bare ``workspace`` (assumes the user's PATH points at a launcher
         that includes the post-PR-#169 workspace.py with REMAINDER
         support).
    """
    cmd = os.environ.get("ATLAS_SHADOW_WORKSPACE_CMD")
    if cmd:
        return cmd.split()
    py = os.environ.get("WORKSPACE_PY")
    venv_py = os.environ.get("WORKSPACE_VENV_PY")
    if py and venv_py:
        return [venv_py, py]
    return ["workspace"]


def build_atlas_query_argv(
    *,
    question: str,
    org_id: str,
    tool: str = "auto",
    principal_id: Optional[str] = None,
    domain_pack: Optional[str] = None,
    code_revision_id: Optional[str] = None,
    output_format: str = "json",
) -> list[str]:
    """Assemble the argv for `workspace run atlas-query -- ...`.

    Mirrors the wrapper's argparse contract (see
    `tandemstream/core` at `scripts/workspace_atlas_query.py`):

    - `--question` REQUIRED
    - `--org-id` REQUIRED
    - `--tool` default ``auto``; choices ``answer|find_code|scan_search|auto``
    - `--principal-id` optional (wrapper has its own default)
    - `--domain-pack` optional (default in wrapper: ``scheduling_admin``;
      atlas-shadow passes ``code`` for the dogfood-v2 fixture)
    - `--code-revision-id` optional; forwarded to both find_code and
      scan_search to pin the queried revision
    - `--output-format` json only in v1
    """
    argv = [
        *_workspace_launcher(),
        "run",
        "atlas-query",
        "--",
        "--question",
        question,
        "--org-id",
        org_id,
        "--tool",
        tool,
        "--output-format",
        output_format,
    ]
    if principal_id:
        argv += ["--principal-id", principal_id]
    if domain_pack:
        argv += ["--domain-pack", domain_pack]
    if code_revision_id:
        argv += ["--code-revision-id", code_revision_id]
    return argv


def invoke_atlas_query(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 180,
    env: Optional[dict[str, str]] = None,
    _subprocess_run=subprocess.run,
) -> AtlasResponse:
    """Run the wrapper subprocess; parse the JSON payload from stdout.

    ``_subprocess_run`` is injectable so tests can stub the subprocess
    without monkeypatching the module-level :mod:`subprocess`.

    The wrapper writes diagnostics to stderr and JSON to stdout (per the
    PR-r3 fix on the workspace runner — stdout-only JSON so callers can
    pipe to `jq`). Atlas-shadow records both streams; only stdout is
    expected to parse.
    """
    started = time.perf_counter()
    try:
        proc = _subprocess_run(
            argv,
            cwd=str(cwd),
            env=env or os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return AtlasResponse(
            tool_used="",
            answer_text="",
            raw_result=None,
            evidence_keys=[],
            atlas_latency_ms=elapsed_ms,
            request_id="",
            commit="",
            stderr=f"TimeoutExpired after {exc.timeout}s",
            returncode=-1,
            exception=f"TimeoutExpired: {exc}",
        )
    except FileNotFoundError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return AtlasResponse(
            tool_used="",
            answer_text="",
            raw_result=None,
            evidence_keys=[],
            atlas_latency_ms=elapsed_ms,
            request_id="",
            commit="",
            stderr=str(exc),
            returncode=-1,
            exception=f"FileNotFoundError: {exc}",
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    payload: dict[str, Any] = {}
    parse_error: Optional[str] = None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        parse_error = f"JSONDecodeError: {exc}"

    metrics = payload.get("metrics") or {}
    return AtlasResponse(
        tool_used=str(payload.get("tool_used") or ""),
        answer_text=str(payload.get("answer_text") or ""),
        raw_result=payload.get("raw_result"),
        evidence_keys=list(payload.get("evidence_keys") or []),
        atlas_latency_ms=int(metrics.get("atlas_latency_ms") or elapsed_ms),
        request_id=str(payload.get("request_id") or ""),
        commit=str(payload.get("commit") or ""),
        stderr=stderr,
        returncode=int(proc.returncode),
        exception=parse_error,
    )


def run_one(
    receipt: Receipt,
    *,
    fixture_id: str,
    org_id: str,
    core_repo_path: Path,
    tool: str = "auto",
    principal_id: Optional[str] = None,
    domain_pack: Optional[str] = None,
    code_revision_id: Optional[str] = None,
    timeout: int = 180,
    _invoke=invoke_atlas_query,
) -> ShadowResponse:
    """Run a single receipt; return a pre-grade :class:`ShadowResponse`.

    The Atlas leaf is the cwd for the subprocess (the `atlas-query`
    workspace command itself does ``cd`` into the Atlas leaf via the venv
    check, but workspace.yaml is anchored there — invoking from elsewhere
    surfaces resolution errors).
    """
    atlas_leaf = core_repo_path / "products" / "tandem" / "packages" / "python" / "atlas"
    if not atlas_leaf.exists():
        raise FileNotFoundError(
            f"Atlas leaf not found under core_repo_path={core_repo_path}: "
            f"expected {atlas_leaf}"
        )
    argv = build_atlas_query_argv(
        question=receipt.question,
        org_id=org_id,
        tool=tool,
        principal_id=principal_id,
        domain_pack=domain_pack,
        code_revision_id=code_revision_id,
    )
    started = time.perf_counter()
    response = _invoke(argv, cwd=atlas_leaf, timeout=timeout)
    wall_ms = int((time.perf_counter() - started) * 1000)
    return ShadowResponse(
        question_id=receipt.question_id,
        question=receipt.question,
        fixture_id=fixture_id,
        atlas_response=response,
        wall_time_ms=wall_ms,
        captured_at=datetime.now(timezone.utc).isoformat(),
        org_id=org_id,
        tool=tool,
        extra={"argv": shlex.join(argv)},
    )


def run_batch(
    receipts: list[Receipt],
    *,
    fixture_id: str,
    org_id: str,
    core_repo_path: Path,
    tool: str = "auto",
    principal_id: Optional[str] = None,
    domain_pack: Optional[str] = None,
    code_revision_id: Optional[str] = None,
    timeout: int = 180,
    progress_cb=None,
    _invoke=invoke_atlas_query,
) -> list[ShadowResponse]:
    """Run a list of receipts in order. ``progress_cb(i, n, response)`` is
    called after each completion if supplied.
    """
    out: list[ShadowResponse] = []
    n = len(receipts)
    for i, r in enumerate(receipts, start=1):
        resp = run_one(
            r,
            fixture_id=fixture_id,
            org_id=org_id,
            core_repo_path=core_repo_path,
            tool=tool,
            principal_id=principal_id,
            domain_pack=domain_pack,
            code_revision_id=code_revision_id,
            timeout=timeout,
            _invoke=_invoke,
        )
        out.append(resp)
        if progress_cb:
            progress_cb(i, n, resp)
    return out
