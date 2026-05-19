"""doc_resolver — doc-anchored receipt resolver (T4a).

Receipts whose ``source_ref.path`` has a doc extension
(``.md``/``.markdown``/``.txt``/``.json``/``.yaml``/``.yml``/``.log``) route
through this module instead of ``find_code`` — ``find_code`` doesn't emit
doc citations today (D-P2-10).

This is a P2-local resolver, NOT a CodePack semantic layer (D-P2-14
captures the v1.1 interface request for CodePack). The v1 contract reads:

  * **Module boundary:** direct psycopg2 from atlas-shadow. NO ``core.*``
    imports. Amendment-3 (2026-05-15) flipped the design from a
    ``workspace_atlas_query.py resolve-doc-receipt`` subcommand to direct
    DB access because P2 v1 ships zero core-repo changes.
  * **DB connection:** env-var chain
    ``ATLAS_SHADOW_DOC_RESOLVER_DB_URL`` →
    ``ATLAS_ADMIN_DB_URL`` →
    ``ATLAS_DB_URL``. Credentials NEVER inlined. Connect timeout 5s;
    statement timeout 10s default (override via
    ``ATLAS_SHADOW_DOC_RESOLVER_QUERY_TIMEOUT_MS``).
  * **Org-id discipline:** every query scopes ``org_id`` as the first
    WHERE predicate (mirrors ``--org-id required everywhere``). No
    defaulting.

The 3-tier resolution (D-P2-11):

  1. **Primary** — exact ``(org_id, doc_id)`` lookup with
     ``doc_id = f"{repo}@{commit}:{path}"`` (the canonical Path-A /
     SCIP-path shape established by P1's T1 reframe in
     ``core/code/ingest.py``). Loads matching ``artifact_chunks`` +
     ``metadata.chunk_headings`` JSONB. Marks
     ``revision_binding: db_commit_scoped``.
  2. **Fallback** — ``git_receipt_snapshot``. Shells
     ``git -C <repo_path> show <commit>:<path>``, slices the cited line
     range, applies the same canonicalization rule as core's
     ``excerpt_canonical`` (dedent by fence indent, rstrip per-line,
     ensure trailing newline), computes sha256, compares to
     ``source_ref.excerpt_sha256``. On match, returns the file content
     with ``revision_binding: git_receipt_snapshot``.
  3. **Terminal** — ``unresolved_source_ref``. Neither resolved.

``unresolved_source_ref`` is a GRADING DATA POINT, not a transient error.
The resolver does not retry. The PR comment (T8) surfaces it so packet
authors see resolution failures.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

# Resolution status values. The grader treats `db_commit_scoped` /
# `git_receipt_snapshot` as resolved (the chunk text is fed as
# `atlas_answer_text`); `unresolved_source_ref` becomes `atlas_not_found`
# in the grader's heuristic short-circuit (raw_text is empty).
STATUS_DB_COMMIT_SCOPED = "db_commit_scoped"
STATUS_GIT_RECEIPT_SNAPSHOT = "git_receipt_snapshot"
STATUS_UNRESOLVED = "unresolved_source_ref"

# Revision-binding values returned alongside the resolved chunk. Doc
# receipts whose binding is `none` did NOT resolve.
BINDING_DB_COMMIT_SCOPED = "db_commit_scoped"
BINDING_GIT_RECEIPT_SNAPSHOT = "git_receipt_snapshot"
BINDING_NONE = "none"


@dataclass(frozen=True)
class DocResolverResult:
    """Output of :func:`resolve_doc_receipt`.

    The grader (T5) feeds ``raw_text`` to ``grader.grade(...)`` as the
    ``atlas_answer_text`` and uses ``status`` + ``revision_binding`` to
    annotate the PR-comment row + JSON artifact (T8 / T9). Fields are
    intentionally nullable when the resolver couldn't populate them — the
    grader heuristic short-circuit handles empty ``raw_text`` correctly.
    """

    status: str
    revision_binding: str
    artifact_id: Optional[str] = None
    chunk_id: Optional[str] = None
    path: Optional[str] = None
    heading_path: Optional[list[str]] = None
    heading_level: Optional[int] = None
    raw_text: str = ""
    start_offset: Optional[int] = None
    end_offset: Optional[int] = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _resolve_db_url() -> Optional[str]:
    """Return the first non-empty DB URL from the env-var chain.

    Order:
      1. ``ATLAS_SHADOW_DOC_RESOLVER_DB_URL`` — T4a-specific override (lets
         operators point the resolver at a read-replica without touching
         the daemon's other DB-using paths).
      2. ``ATLAS_ADMIN_DB_URL`` — matches existing ``ingest.py`` pattern.
      3. ``ATLAS_DB_URL`` — matches existing ``ingest.py`` pattern.

    Returns ``None`` when none are set. Caller treats that as a
    configuration error (no silent default).
    """
    for var in (
        "ATLAS_SHADOW_DOC_RESOLVER_DB_URL",
        "ATLAS_ADMIN_DB_URL",
        "ATLAS_DB_URL",
    ):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _statement_timeout_ms() -> int:
    """Return the configured statement timeout (default 10000ms)."""
    raw = os.environ.get("ATLAS_SHADOW_DOC_RESOLVER_QUERY_TIMEOUT_MS")
    if not raw:
        return 10000
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 10000
    return value if value > 0 else 10000


# ---------------------------------------------------------------------------
# Excerpt canonicalization (mirror of core/work_packets/qna_receipts.py
# `excerpt_canonical` — must stay byte-identical to keep sha256s aligned).
# ---------------------------------------------------------------------------


def _excerpt_canonical(body: str, indent: str = "") -> str:
    """Canonicalize a body for sha256 (matches core's `excerpt_canonical`).

    Steps:
      1. Dedent each line that starts with ``indent`` (no-op when
         ``indent=""``).
      2. rstrip per-line whitespace.
      3. Ensure trailing newline.

    Inlined here because T4a may not ``import core.*`` (plan §3 T4a DB
    boundary). The algorithm is short and stable; a regression in core's
    version would also fail the receipt verifier upstream so the drift
    would surface at packet authoring time, not in T4a.
    """
    if indent:
        lines = body.splitlines()
        dedented: list[str] = []
        for line in lines:
            if line.startswith(indent):
                dedented.append(line[len(indent):])
            else:
                dedented.append(line)
        body = "\n".join(dedented)
    body = "\n".join(line.rstrip() for line in body.split("\n"))
    if not body.endswith("\n"):
        body += "\n"
    return body


def _sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Source-lines range parsing
# ---------------------------------------------------------------------------

_LINE_RANGE_RE = re.compile(
    r"^\s*(?P<start>\d+)\s*(?:[-–]\s*(?P<end>\d+))?\s*$"
)


def _parse_line_range(s: Optional[str]) -> Optional[tuple[int, int]]:
    """Parse ``"<start>"`` or ``"<start>-<end>"`` (1-indexed, both inclusive).

    Returns ``None`` for empty / malformed input; caller treats that as
    "no slice known" and skips the git_receipt_snapshot path.
    """
    if not s:
        return None
    m = _LINE_RANGE_RE.match(s)
    if not m:
        return None
    start = int(m.group("start"))
    end_str = m.group("end")
    end = int(end_str) if end_str else start
    if start < 1 or end < start:
        return None
    return start, end


# ---------------------------------------------------------------------------
# DB-side primary lookup
# ---------------------------------------------------------------------------


def _build_doc_id(repo: str, commit: str, path: str) -> str:
    """Build the canonical doc_id shape used by Path-A / SCIP-path doc
    ingest (``"<repo>@<commit>:<path>"``)."""
    return f"{repo}@{commit}:{path}"


_ATLAS_LEAF_PREFIX = "products/tandem/packages/python/atlas/"


def _doc_path_variants(path: str) -> list[str]:
    """Return conservative repo-relative variants for a receipt doc path.

    Packet receipts sometimes cite paths from the Atlas leaf perspective,
    e.g. ``Atlas/docs/specs/instruction-memory-v1.md``. Shadow doc ingest
    stores the same file at the repo-root relpath
    ``products/tandem/packages/python/atlas/docs/specs/...``. Try the
    receipt path first for exact compatibility, then deterministic aliases.
    """
    raw = (path or "").strip()
    if not raw:
        return []

    variants: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip().lstrip("/")
        if candidate and candidate not in variants:
            variants.append(candidate)

    add(raw)
    if raw.startswith("Atlas/"):
        add(_ATLAS_LEAF_PREFIX + raw.removeprefix("Atlas/"))
    if raw.startswith(_ATLAS_LEAF_PREFIX):
        add("Atlas/" + raw.removeprefix(_ATLAS_LEAF_PREFIX))
    return variants


def _pick_chunk_for_lines(
    chunks: list[dict[str, Any]],
    oracle_excerpt: str,
) -> Optional[dict[str, Any]]:
    """Best-effort chunk selection.

    The receipt cites a line range; chunks carry byte/char offsets, not
    line numbers. We pick the chunk whose ``raw_text`` contains the
    longest matching prefix from ``oracle_excerpt`` (substring match) —
    that's the chunk most likely to be the cited content. If no chunk
    has any overlap, return the first chunk so the grader still has
    something to compare against (rather than blanking the receipt).

    Returns ``None`` when ``chunks`` is empty.
    """
    if not chunks:
        return None
    needle = (oracle_excerpt or "").strip()
    if not needle:
        return chunks[0]
    # Try progressively shorter prefixes of the oracle excerpt; the first
    # non-empty match wins. Stop short of single-char matches to avoid
    # accidental hits on common chars.
    for take in (200, 100, 60, 30):
        head = needle[:take]
        if len(head) < 8:
            continue
        for chunk in chunks:
            if head in (chunk.get("raw_text") or ""):
                return chunk
    return chunks[0]


def _primary_lookup_db(
    *,
    org_id: str,
    repo: str,
    commit: str,
    path: str,
    oracle_excerpt: str,
    _connect: Callable[..., Any],
    db_url: str,
) -> Optional[dict[str, Any]]:
    """Run the primary DB lookup. Return None when no artifact row exists.

    Errors (connection refused, statement timeout, etc.) are raised; the
    caller catches them and turns them into ``unresolved_source_ref``.
    """
    doc_id = _build_doc_id(repo, commit, path)
    timeout_ms = _statement_timeout_ms()
    conn = _connect(
        db_url,
        connect_timeout=5,
        options=f"-c statement_timeout={timeout_ms}",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT artifact_id, metadata FROM artifacts "
                "WHERE org_id = %s AND doc_id = %s",
                (org_id, doc_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            artifact_id, metadata = row
            metadata = metadata or {}
            chunk_headings = (
                metadata.get("chunk_headings") if isinstance(metadata, dict) else {}
            ) or {}
            cur.execute(
                "SELECT chunk_id, chunk_index, raw_text, start_offset, end_offset "
                "FROM artifact_chunks "
                "WHERE org_id = %s AND artifact_id = %s "
                "ORDER BY chunk_index",
                (org_id, str(artifact_id)),
            )
            rows = cur.fetchall()
            chunks: list[dict[str, Any]] = []
            for r in rows:
                chunks.append({
                    "chunk_id": str(r[0]),
                    "chunk_index": int(r[1]),
                    "raw_text": r[2] or "",
                    "start_offset": r[3],
                    "end_offset": r[4],
                })
        return {
            "artifact_id": str(artifact_id),
            "chunk_headings": chunk_headings,
            "chunks": chunks,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# git_receipt_snapshot fallback
# ---------------------------------------------------------------------------


def _git_show_file(
    repo_path: Path,
    commit: str,
    path: str,
    *,
    _subprocess_run: Callable = subprocess.run,
    timeout: int = 30,
) -> Optional[str]:
    """Materialize ``<commit>:<path>`` from the local clone. Return None
    on any failure (missing repo, unknown commit, file not present at that
    commit). 30s subprocess timeout (§13).
    """
    if not repo_path.exists():
        return None
    proc = _subprocess_run(
        ["git", "-C", str(repo_path), "show", f"{commit}:{path}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _slice_lines(body: str, line_range: tuple[int, int]) -> str:
    """Return the inclusive ``[start, end]`` 1-indexed line slice of ``body``.

    The slice preserves intermediate empty lines but does not include a
    trailing newline (canonicalization adds it).
    """
    start, end = line_range
    lines = body.splitlines()
    sliced = lines[start - 1:end]
    return "\n".join(sliced)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_doc_receipt(
    receipt,
    *,
    org_id: str,
    repo: str,
    repo_path: Path,
    _connect: Optional[Callable[..., Any]] = None,
    _subprocess_run: Callable = subprocess.run,
) -> DocResolverResult:
    """Resolve a doc-anchored :class:`PacketReceipt`.

    Args:
      receipt: a ``PacketReceipt`` whose ``source_path`` extension is one
        of the doc extensions (caller verified via T4 routing).
      org_id: tenant scope. Resolver REQUIRES this — no defaulting.
        Caller pulls from ``cfg.continuous_shadow_org_id``.
      repo: GitHub-style ``owner/name`` (e.g. ``tandemstream/core``).
        Used to build ``doc_id`` for the primary lookup. Caller pulls
        from the PR event's ``repository.full_name``.
      repo_path: local clone path for the git-receipt-snapshot fallback
        (e.g. ``cfg.core_repo_path``).
      _connect: injectable psycopg-connect for testing. Defaults to
        ``psycopg2.connect``.
      _subprocess_run: injectable subprocess.run for testing the git
        fallback path.

    Returns:
      A :class:`DocResolverResult`. ``status`` is one of
      :data:`STATUS_DB_COMMIT_SCOPED` / :data:`STATUS_GIT_RECEIPT_SNAPSHOT`
      / :data:`STATUS_UNRESOLVED`. The grader treats unresolved as an
      ``atlas_not_found``-shaped grade.
    """
    if not org_id:
        raise ValueError("resolve_doc_receipt requires org_id (no defaulting)")
    if not repo:
        raise ValueError("resolve_doc_receipt requires repo (full_name)")

    src_path = receipt.source_path
    commit = receipt.source_commit
    warnings: list[str] = []

    if not src_path or not commit:
        warnings.append(
            "missing source_path or source_commit on doc receipt"
        )
        return DocResolverResult(
            status=STATUS_UNRESOLVED,
            revision_binding=BINDING_NONE,
            warnings=warnings,
        )

    path_variants = _doc_path_variants(src_path)

    # ---- Tier 1: primary DB lookup -----------------------------------------
    if _connect is None:
        try:
            import psycopg2  # type: ignore
        except ImportError:
            psycopg2 = None
        _connect = psycopg2.connect if psycopg2 else None  # type: ignore
    db_url = _resolve_db_url()
    if _connect is None or not db_url:
        warnings.append(
            "primary DB lookup skipped: "
            + ("psycopg2 not installed" if _connect is None else "no DB URL configured")
        )
    else:
        lookup = None
        lookup_path = src_path
        for candidate_path in path_variants:
            try:
                lookup = _primary_lookup_db(
                    org_id=org_id,
                    repo=repo,
                    commit=commit,
                    path=candidate_path,
                    oracle_excerpt=receipt.oracle_excerpt or "",
                    _connect=_connect,
                    db_url=db_url,
                )
            except Exception as exc:
                # Per plan: psycopg errors -> unresolved + warning. No retry.
                warnings.append(f"db_error: {type(exc).__name__}: {exc}")
                lookup = None
                break
            if lookup is not None:
                lookup_path = candidate_path
                if lookup_path != src_path:
                    warnings.append(f"path_alias_resolved:{src_path}->{lookup_path}")
                break
        if lookup is not None:
            chunk = _pick_chunk_for_lines(
                lookup["chunks"], receipt.oracle_excerpt or ""
            )
            heading_meta = {}
            if chunk is not None:
                heading_meta = (
                    lookup["chunk_headings"].get(str(chunk["chunk_index"]))
                    or lookup["chunk_headings"].get(str(chunk.get("chunk_index", "")))
                    or {}
                )
            heading_path = heading_meta.get("heading_path") if isinstance(heading_meta, dict) else None
            heading_level = heading_meta.get("heading_level") if isinstance(heading_meta, dict) else None
            return DocResolverResult(
                status=STATUS_DB_COMMIT_SCOPED,
                revision_binding=BINDING_DB_COMMIT_SCOPED,
                artifact_id=lookup["artifact_id"],
                chunk_id=chunk["chunk_id"] if chunk else None,
                path=lookup_path,
                heading_path=list(heading_path) if heading_path else None,
                heading_level=int(heading_level) if isinstance(heading_level, int) else None,
                raw_text=chunk["raw_text"] if chunk else "",
                start_offset=chunk["start_offset"] if chunk else None,
                end_offset=chunk["end_offset"] if chunk else None,
                warnings=warnings,
            )

    # ---- Tier 2: git_receipt_snapshot fallback ----------------------------
    body = None
    git_path = src_path
    for candidate_path in path_variants:
        body = _git_show_file(
            repo_path,
            commit,
            candidate_path,
            _subprocess_run=_subprocess_run,
        )
        if body is not None:
            git_path = candidate_path
            if git_path != src_path:
                warnings.append(f"path_alias_resolved:{src_path}->{git_path}")
            break
    if body is None:
        warnings.append("git_show_failed_or_repo_missing")
        return DocResolverResult(
            status=STATUS_UNRESOLVED,
            revision_binding=BINDING_NONE,
            path=src_path,
            warnings=warnings,
        )
    line_range = _parse_line_range(receipt.source_lines)
    if line_range is None:
        # No line range — verify against the whole file body and trust the
        # SHA only when it matches a whole-file canonicalization. (Most
        # receipts have line ranges; whole-file is the corner case.)
        sliced = body
    else:
        sliced = _slice_lines(body, line_range)
    expected_sha = receipt.excerpt_sha256
    if expected_sha:
        canon = _excerpt_canonical(sliced, indent="")
        actual_sha = _sha256_of(canon)
        if actual_sha != expected_sha:
            warnings.append(
                f"excerpt_sha256_mismatch: expected={expected_sha[:12]} actual={actual_sha[:12]}"
            )
            return DocResolverResult(
                status=STATUS_UNRESOLVED,
                revision_binding=BINDING_NONE,
                path=git_path,
                warnings=warnings,
            )
    else:
        warnings.append("no_excerpt_sha256_on_receipt")
    # Snapshot resolved. Return the cited slice as raw_text.
    return DocResolverResult(
        status=STATUS_GIT_RECEIPT_SNAPSHOT,
        revision_binding=BINDING_GIT_RECEIPT_SNAPSHOT,
        path=git_path,
        raw_text=sliced,
        warnings=warnings,
    )
