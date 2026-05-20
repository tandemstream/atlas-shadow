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

**D5 freshness handoff (amendment decision #1):** when the ingest daemon
is running, it writes ``<atlas-shadow>/.daemon-state.json`` after each
successful ingest. ``resolve_code_revision_id`` reads that file first
and falls back to ``shadow-config.yaml:continuous_shadow_code_revision_id``
when the file is missing or unparseable. Daemon doesn't need to be up at
runner-call time — the file is the IPC.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .parser import Receipt


def resolve_code_revision_id(
    config: dict,
    *,
    config_path: Optional[Path] = None,
) -> Optional[str]:
    """Resolve the active ``code_revision_id`` per amendment decision #1.

    Read order:
      1. ``<atlas-shadow>/.daemon-state.json`` (if present + parseable) —
         use ``latest_code_revision_id``. The path is the ``state_file``
         setting under ``config['ingest_daemon']`` (relative paths resolved
         against ``config_path``'s parent dir, or cwd if not provided).
      2. ``config['continuous_shadow_code_revision_id']`` — the pre-D5
         pinned value (still authoritative when the daemon is off).
      3. ``None`` — runner omits ``--code-revision-id`` entirely.

    Args:
      config: Parsed ``shadow-config.yaml`` (the dict from
        ``cli._load_config``).
      config_path: Path to ``shadow-config.yaml`` (used as the anchor for
        resolving the relative ``state_file`` path). If None, the daemon
        section's ``state_file`` is resolved against cwd.

    Returns the resolved ``code_revision_id`` (str) or ``None``.
    """
    section = config.get("ingest_daemon") or {}
    state_file_raw = section.get("state_file") or ".daemon-state.json"
    state_path = Path(str(state_file_raw)).expanduser()
    if not state_path.is_absolute():
        base = Path(config_path).parent.expanduser() if config_path else Path.cwd()
        state_path = (base / state_path).resolve()
    if state_path.exists():
        try:
            with state_path.open("r", encoding="utf-8") as fp:
                state = json.load(fp)
        except (OSError, json.JSONDecodeError):
            state = None
        if isinstance(state, dict):
            rev = state.get("latest_code_revision_id")
            if rev:
                return str(rev)
    rev = config.get("continuous_shadow_code_revision_id")
    return str(rev) if rev else None


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
    """Per-question record emitted to atlas-qa-shadow.jsonl (pre-grade).

    ``atlas_cache_status`` is populated when :func:`run_one` is called
    with an :class:`AtlasQueryCache` instance. Values: ``"hit"`` (the
    response came from the cache; no subprocess fired),
    ``"miss"`` (the cache was checked, missed, response computed and
    stored), ``"disabled"`` (run_one was called without a cache —
    subprocess fired normally). ``None`` is reserved for callers that
    don't go through the atlas subprocess path at all (e.g. doc
    receipts resolved via :mod:`doc_resolver`).
    """

    question_id: str
    question: str
    fixture_id: str
    atlas_response: AtlasResponse
    wall_time_ms: int
    captured_at: str
    org_id: str
    tool: str
    extra: dict = field(default_factory=dict)
    atlas_cache_status: Optional[str] = None


def _atlas_query_launcher(*, atlas_python: Optional[str] = None) -> list[str]:
    """Resolve the Atlas query launcher.

    Order of precedence:
      1. ``ATLAS_SHADOW_ATLAS_QUERY_CMD`` env var (space-split argv).
      2. Legacy ``ATLAS_SHADOW_WORKSPACE_CMD`` env var (space-split argv),
         expanded as ``<cmd> run atlas-query --``.
      3. Legacy ``WORKSPACE_PY`` + ``WORKSPACE_VENV_PY`` env vars (Atlas
         shell-function convention) — if both set, return
         ``[$WORKSPACE_VENV_PY, $WORKSPACE_PY]``.
      4. The Atlas leaf's Python interpreter with
         ``-m scripts.workspace_atlas_query``.

    The workspace CLI forwarding contract has drifted more than once. The
    module invocation is the stable path because ``run_one`` already uses
    the Atlas leaf as cwd.
    """
    direct = os.environ.get("ATLAS_SHADOW_ATLAS_QUERY_CMD")
    if direct:
        return direct.split()
    cmd = os.environ.get("ATLAS_SHADOW_WORKSPACE_CMD")
    if cmd:
        return [*cmd.split(), "run", "atlas-query", "--"]
    py = os.environ.get("WORKSPACE_PY")
    venv_py = os.environ.get("WORKSPACE_VENV_PY")
    if py and venv_py:
        return [venv_py, py, "run", "atlas-query", "--"]
    return [atlas_python or sys.executable, "-m", "scripts.workspace_atlas_query"]


def build_atlas_query_argv(
    *,
    question: str,
    org_id: str,
    tool: str = "auto",
    principal_id: Optional[str] = None,
    domain_pack: Optional[str] = None,
    code_revision_id: Optional[str] = None,
    output_format: str = "json",
    atlas_python: Optional[str] = None,
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
        *_atlas_query_launcher(atlas_python=atlas_python),
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
    atlas_query_cache: Any = None,
    _invoke=invoke_atlas_query,
) -> ShadowResponse:
    """Run a single receipt; return a pre-grade :class:`ShadowResponse`.

    The Atlas leaf is the cwd for the subprocess (the `atlas-query`
    workspace command itself does ``cd`` into the Atlas leaf via the venv
    check, but workspace.yaml is anchored there — invoking from elsewhere
    surfaces resolution errors).

    **Cache integration** (PR atlas-shadow-query-cache-v1): when
    ``atlas_query_cache`` is an :class:`AtlasQueryCache` instance, the
    cache is consulted before invoking the subprocess. On cache hit
    the subprocess is skipped entirely; on cache miss the response is
    stored after the call. The returned ShadowResponse's
    ``atlas_cache_status`` field reports ``"hit"`` / ``"miss"`` /
    ``"disabled"`` (the latter when ``atlas_query_cache is None``).

    The cache is opt-in at the call site. Batch mode constructs a
    cache and passes it; the live webhook PR-grading path does NOT,
    so production gates never observe a cached result. See
    :mod:`atlas_query_cache` module docstring for the boundary
    rationale.
    """
    atlas_leaf = core_repo_path / "products" / "tandem" / "packages" / "python" / "atlas"
    if not atlas_leaf.exists():
        raise FileNotFoundError(
            f"Atlas leaf not found under core_repo_path={core_repo_path}: "
            f"expected {atlas_leaf}"
        )

    cache_status: Optional[str] = None
    cache_key = None
    if atlas_query_cache is not None:
        # Local import to keep the cache module out of the runner's
        # cold path when the caller doesn't use the cache.
        from atlas_shadow.ingest_daemon.atlas_query_cache import CacheKey

        cache_key = CacheKey(
            query_text=receipt.question,
            tool=tool,
            source_path=receipt.source_path,
            source_lines=receipt.source_lines,
            source_commit=receipt.commit_sha or "",
            code_revision_id=code_revision_id,
            org_id=org_id,
            principal_id=principal_id,
            domain_pack=domain_pack,
        )
        hit = atlas_query_cache.get(cache_key)
        if hit is not None:
            # Reconstruct AtlasResponse from cached JSON. Treat any
            # deserialization error as a cache miss (defensive — a
            # malformed entry shouldn't be load-bearing).
            try:
                payload = json.loads(hit.response_json)
                cached_response = AtlasResponse(
                    tool_used=str(payload.get("tool_used") or ""),
                    answer_text=str(payload.get("answer_text") or ""),
                    raw_result=payload.get("raw_result"),
                    evidence_keys=list(payload.get("evidence_keys") or []),
                    atlas_latency_ms=int(payload.get("atlas_latency_ms") or 0),
                    request_id=str(payload.get("request_id") or ""),
                    commit=str(payload.get("commit") or ""),
                    stderr=str(payload.get("stderr") or ""),
                    returncode=int(payload.get("returncode") or 0),
                    exception=payload.get("exception"),
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                cached_response = None
            if cached_response is not None:
                return ShadowResponse(
                    question_id=receipt.question_id,
                    question=receipt.question,
                    fixture_id=fixture_id,
                    atlas_response=cached_response,
                    wall_time_ms=hit.response_latency_ms,
                    captured_at=datetime.now(timezone.utc).isoformat(),
                    org_id=org_id,
                    tool=tool,
                    extra={"atlas_cache_key_prefix": hit.key_fingerprint[:12]},
                    atlas_cache_status="hit",
                )
        cache_status = "miss"
    else:
        cache_status = "disabled"

    argv = build_atlas_query_argv(
        question=receipt.question,
        org_id=org_id,
        tool=tool,
        principal_id=principal_id,
        domain_pack=domain_pack,
        code_revision_id=code_revision_id,
        atlas_python=str(atlas_leaf / ".venv" / "bin" / "python"),
    )
    started = time.perf_counter()
    response = _invoke(argv, cwd=atlas_leaf, timeout=timeout)
    wall_ms = int((time.perf_counter() - started) * 1000)

    # Store on cache miss. Only cache successful responses (returncode
    # 0, no exception) — caching a transient subprocess failure would
    # be a foot-gun. The defensive check also ensures we don't store
    # garbage during partial atlas-side failures.
    if (
        atlas_query_cache is not None
        and cache_key is not None
        and response.returncode == 0
        and not response.exception
    ):
        try:
            atlas_query_cache.set(
                cache_key,
                response_json=json.dumps(_atlas_response_to_jsonable(response)),
                response_latency_ms=wall_ms,
            )
        except Exception as exc:  # noqa: BLE001 — cache failures non-fatal
            import sys
            print(
                f"[runner] WARN: atlas_query_cache.set failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    extra = {"argv": shlex.join(argv)}
    if cache_key is not None:
        extra["atlas_cache_key_prefix"] = cache_key.fingerprint()[:12]

    return ShadowResponse(
        question_id=receipt.question_id,
        question=receipt.question,
        fixture_id=fixture_id,
        atlas_response=response,
        wall_time_ms=wall_ms,
        captured_at=datetime.now(timezone.utc).isoformat(),
        org_id=org_id,
        tool=tool,
        extra=extra,
        atlas_cache_status=cache_status,
    )


def _atlas_response_to_jsonable(response: AtlasResponse) -> dict[str, Any]:
    """Round-trip serializer for AtlasResponse → cache payload.

    Mirrors the field set on :class:`AtlasResponse` exactly. The
    deserializer side (in run_one's cache-hit path) reads from the
    same set of keys. ``raw_result`` may be ``None`` or any
    JSON-serializable structure — atlas's wrapper emits a dict, but
    we don't constrain the shape because future atlas tooling may
    add fields.
    """
    return {
        "tool_used": response.tool_used,
        "answer_text": response.answer_text,
        "raw_result": response.raw_result,
        "evidence_keys": list(response.evidence_keys),
        "atlas_latency_ms": response.atlas_latency_ms,
        "request_id": response.request_id,
        "commit": response.commit,
        "stderr": response.stderr,
        "returncode": response.returncode,
        "exception": response.exception,
    }


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
    atlas_query_cache: Any = None,
    _invoke=invoke_atlas_query,
) -> list[ShadowResponse]:
    """Run a list of receipts in order. ``progress_cb(i, n, response)`` is
    called after each completion if supplied.

    The ``atlas_query_cache`` parameter is forwarded to each per-receipt
    ``run_one`` call. See :func:`run_one` for cache semantics.
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
            atlas_query_cache=atlas_query_cache,
            _invoke=_invoke,
        )
        out.append(resp)
        if progress_cb:
            progress_cb(i, n, resp)
    return out
