"""grader_service — PR grading orchestrator.

This module is the entry point for atlas-shadow's pre-merge grading gate
(packet 2026-05-14-atlas-shadow-pre-merge-grading-gate-v1). It coordinates
across receiver (T1), packet-tag detection (T2), receipt parsing (T3),
receipt -> Atlas-query translation (T4), doc-anchored resolution (T4a, in
``doc_resolver.py``), the existing offline ``grader.grade`` rubric, revision
pinning (T6, helpers in ``state_file`` + ``ledger``), GitHub Checks API
(T7, in ``gh_check.py``), the PR comment generator (T8, in
``pr_comment.py``), and the durable artifact writer (T9, here).

Tasks colocated here:

  * T2 — :func:`detect_packet_qna_log` (PR file-presence check).
  * T3 — :func:`parse_packet_receipts` (canonical bullet-list receipt
    parser; also extracts the optional ``grading_threshold_pct:`` header).
  * T4 — :func:`translate_receipt_to_query` (CODE-anchored heuristic +
    ``query_hint:`` override + doc-extension routing to T4a).
  * T5 — :func:`run_pr_grading` / :func:`handle_pr_event` (the orchestrator).
  * T9 — :func:`write_grading_artifact` (durable JSON dump).

T4a (doc resolver) lives in :mod:`atlas_shadow.ingest_daemon.doc_resolver`
to keep its direct psycopg dependency contained.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# T2 — Packet-tag detection
# ---------------------------------------------------------------------------

# Match `<anything>/docs/work/<packet>/02-qna-log.md`. The packet slug is the
# directory between `docs/work/` and `/02-qna-log.md`; T3 uses it to find
# sibling planning docs. Anchored so the suffix is the exact filename.
_QNA_LOG_RE = re.compile(
    r"(?:^|/)docs/work/(?P<packet>[^/]+)/02-qna-log\.md$"
)


def detect_packet_qna_log(pr_files: list[str]) -> list[str]:
    """Return PR files that look like packet ``02-qna-log.md`` paths.

    A PR is "packet-tagged" (RQ-1) when it touches at least one
    ``<...>/docs/work/<packet>/02-qna-log.md`` file. The packet directory
    is ``Path(qna_log_path).parent`` — T3 uses that to locate the receipts.

    Args:
      pr_files: list of file paths from the GitHub PR-files API. Caller
        is responsible for filtering out deletions (status='removed').

    Returns:
      list of touched ``02-qna-log.md`` paths (zero, one, or more — most
      PRs touch one packet but multi-packet PRs are not refused).
    """
    return [p for p in pr_files if _QNA_LOG_RE.search(p)]


# ---------------------------------------------------------------------------
# T3 — Receipt parser integration (`02-qna-log.md` canonical bullet-list
# format) + `grading_threshold_pct:` header parse
# ---------------------------------------------------------------------------

# The atlas-shadow `parser.parse_qna_log_markdown` handles an earlier YAML-
# block-per-receipt convention. The schema-strict canonical format enforced
# by `core/work_packets/qna_receipts.py` (and used by all current
# packets, including this one) is the bullet-list format below — different
# enough that we parse it here rather than retrofit the offline parser.
# `parser.py` is in W2's may-not-touch list (I4 — preserve offline regression);
# this packet-format parser lives alongside the PR-grading orchestrator that
# consumes it.

# Match `### q1: ...` (allow 2 or 3 #'s; allow leading whitespace) as a
# receipt boundary. The trailing title becomes the `question` text.
_RECEIPT_HEADER_RE = re.compile(
    r"^\s*#{2,3}\s+(?P<qid>q\d+)\s*[:\-]?\s*(?P<title>.*?)\s*$"
)

# Match `- **Key:** value` (single-line). Sub-bullets (two-space-indented
# `- key: value`) are parsed separately within nested-key blocks.
_BULLET_KV_RE = re.compile(
    r"^\s*-\s+\*\*(?P<key>[^:*]+?):\*\*\s*(?P<value>.*?)\s*$"
)

# Match a sub-bullet under a parent block: `  - key: value`. Indentation
# is at least 2 spaces (any deeper is also accepted).
_SUB_BULLET_RE = re.compile(
    r"^\s{2,}-\s+(?P<key>[A-Za-z_][\w]*)\s*[:=]\s*(?P<value>.*?)\s*$"
)

# A code-fence boundary: ``` optionally followed by a language tag. Used to
# extract the body of the **Excerpt:** block (which is always a fenced block
# under the receipt's bullet list — see `qna_receipts.py` §8).
_CODE_FENCE_RE = re.compile(r"^\s*```")

# The threshold header — accept any of:
#   grading_threshold_pct: 60
#   **grading_threshold_pct:** 60     (colon inside bold)
#   **grading_threshold_pct**: 60     (colon outside bold)
#   Grading threshold pct: 60         (spaced / capitalized)
# Anywhere before the first `## ` section heading (preamble convention;
# not a frontmatter requirement). We strip the bold markers first so the
# regex doesn't have to enumerate every order.
_THRESHOLD_HEADER_RE = re.compile(
    r"^\s*grading[ _-]threshold[ _-]pct\s*:\s*(?P<value>\d{1,3})\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PacketReceipt:
    """A canonical packet receipt parsed from `02-qna-log.md`.

    Mirrors the bullet-list schema enforced by `check_qa_receipts.py` —
    every field is populated from the bullet body when present, defaulting
    to ``None`` / empty-string when absent. Whether a field is required is
    determined by the receipt's `status`:

      * `ok` / `redacted_ok` / `ambiguous` / `not_found` — require source_path,
        source_commit, excerpt, excerpt_sha256, command_text.
      * `ok_absence` — require source_commit + command_text only; no excerpt.
      * `assumption` / `user_provided` — require only the always-required
        fields (claim_supported, status, evidence_type).

    The grader (T5) reads ``oracle_claim`` + ``oracle_excerpt`` directly;
    the translator (T4) reads ``command_text`` (for heuristic routing) and
    ``query_hint`` (for per-receipt override); the doc resolver (T4a) reads
    ``source_path``, ``source_commit``, ``source_lines``, ``excerpt_sha256``.
    """

    question_id: str
    question: str
    oracle_claim: str
    oracle_excerpt: str
    status: Optional[str] = None
    evidence_type: Optional[str] = None
    source_path: Optional[str] = None
    source_lines: Optional[str] = None
    source_commit: Optional[str] = None
    source_tree_state: Optional[str] = None
    excerpt_sha256: Optional[str] = None
    command_text: Optional[str] = None
    command_exit_code: Optional[int] = None
    query_hint: Optional[str] = None
    extras: dict = field(default_factory=dict)


def parse_packet_receipts(
    qna_log_path: Path,
) -> tuple[list[PacketReceipt], Optional[int]]:
    """Parse a packet `02-qna-log.md` (canonical bullet-list format).

    Returns ``(receipts, grading_threshold_pct)``. The threshold is None
    when the file doesn't carry a ``grading_threshold_pct:`` header (caller
    falls back to the daemon default — 50 per RQ-4).

    The parser is intentionally permissive: malformed receipt blocks are
    skipped rather than aborting the whole file. The grader treats
    skipped blocks as if they weren't graded (the per-PR comment surfaces
    a count of unrecognized blocks so packet authors notice).
    """
    qna_log_path = Path(qna_log_path)
    if not qna_log_path.exists():
        raise FileNotFoundError(f"qna log not found: {qna_log_path}")

    text = qna_log_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    threshold = _parse_threshold_header(lines)
    blocks = _split_receipt_blocks(lines)
    receipts: list[PacketReceipt] = []
    for header, body in blocks:
        receipt = _parse_one_receipt(header, body)
        if receipt is not None:
            receipts.append(receipt)
    return receipts, threshold


def _parse_threshold_header(lines: list[str]) -> Optional[int]:
    """Scan the preamble (before the first `## ` heading) for a
    `grading_threshold_pct:` line. Return the int value, or None when
    absent / malformed.

    The check_qa_receipts.py linter only validates the bullet-list
    receipt schema; the threshold header is unique to this packet's
    grader and is intentionally not part of the schema-strict gate.
    """
    for raw in lines:
        stripped = raw.lstrip()
        if stripped.startswith("## "):
            break
        # Strip Markdown bold/italic markers so `**grading_threshold_pct:** 60`
        # and `grading_threshold_pct: 60` both match.
        cleaned = re.sub(r"\*+", "", raw).strip()
        m = _THRESHOLD_HEADER_RE.match(cleaned)
        if m:
            try:
                value = int(m.group("value"))
            except ValueError:
                return None
            if 0 <= value <= 100:
                return value
    return None


def _split_receipt_blocks(lines: list[str]) -> list[tuple[dict, list[str]]]:
    """Split the file body into per-receipt blocks.

    Each block is `(header, body_lines)` where header is
    ``{"qid": "q1", "title": "..."}`` and body_lines is the raw bullet-list
    body (everything between this `### qN:` header and the next one — or
    EOF). The terminating-separator `---` is included; the parser tolerates
    its presence.
    """
    blocks: list[tuple[dict, list[str]]] = []
    cur_header: Optional[dict] = None
    cur_body: list[str] = []
    for raw in lines:
        m = _RECEIPT_HEADER_RE.match(raw)
        if m:
            if cur_header is not None:
                blocks.append((cur_header, cur_body))
            cur_header = {"qid": m.group("qid"), "title": m.group("title")}
            cur_body = []
        else:
            if cur_header is not None:
                cur_body.append(raw)
    if cur_header is not None:
        blocks.append((cur_header, cur_body))
    return blocks


def _parse_one_receipt(header: dict, body_lines: list[str]) -> Optional[PacketReceipt]:
    """Parse the body of a single receipt block into a :class:`PacketReceipt`.

    Returns ``None`` on a block we can't recognize at all (no
    `Claim supported:` field). Permissive on missing optional fields.
    """
    claim: Optional[str] = None
    status: Optional[str] = None
    evidence_type: Optional[str] = None
    source: dict[str, str] = {}
    command: dict[str, str] = {}
    excerpt: Optional[str] = None
    query_hint: Optional[str] = None
    extras: dict = {}

    i = 0
    n = len(body_lines)
    current_subkey_block: Optional[str] = None  # e.g. "Source ref" / "Command"
    while i < n:
        raw = body_lines[i]
        m_kv = _BULLET_KV_RE.match(raw)
        if m_kv:
            key = m_kv.group("key").strip().lower()
            value = m_kv.group("value").strip()
            # Strip surrounding backticks / quotes from values.
            value = _strip_inline_markup(value)
            if key in ("claim supported", "claim_supported", "claim"):
                claim = value
                current_subkey_block = None
            elif key == "status":
                status = value
                current_subkey_block = None
            elif key in ("evidence type", "evidence_type"):
                evidence_type = value
                current_subkey_block = None
            elif key == "source ref":
                # The actual data is in sub-bullets that follow.
                current_subkey_block = "source"
            elif key == "command":
                current_subkey_block = "command"
            elif key == "excerpt":
                # The body is a fenced code block that follows.
                excerpt = _consume_fenced_block(body_lines, i + 1)
                # Advance i to past the closing fence so we don't re-scan.
                i = _find_end_of_fenced_block(body_lines, i + 1)
                current_subkey_block = None
            elif key in ("query hint", "query_hint"):
                query_hint = value
                current_subkey_block = None
            else:
                extras[key] = value
                current_subkey_block = None
            i += 1
            continue
        m_sub = _SUB_BULLET_RE.match(raw)
        if m_sub and current_subkey_block is not None:
            sub_key = m_sub.group("key").strip().lower()
            sub_val = _strip_inline_markup(m_sub.group("value").strip())
            if current_subkey_block == "source":
                source[sub_key] = sub_val
            elif current_subkey_block == "command":
                command[sub_key] = sub_val
            i += 1
            continue
        # Anything else (blank line, separator, unrelated content): skip.
        i += 1

    if claim is None:
        return None

    # Coerce command.exit_code if present.
    exit_code: Optional[int] = None
    raw_ec = command.get("exit_code")
    if raw_ec is not None:
        try:
            exit_code = int(raw_ec)
        except (TypeError, ValueError):
            exit_code = None

    return PacketReceipt(
        question_id=header.get("qid", ""),
        question=header.get("title", "").strip(),
        oracle_claim=claim,
        oracle_excerpt=excerpt or "",
        status=status,
        evidence_type=evidence_type,
        source_path=source.get("path"),
        source_lines=source.get("lines"),
        source_commit=source.get("commit"),
        source_tree_state=source.get("tree_state"),
        excerpt_sha256=source.get("excerpt_sha256"),
        command_text=command.get("text"),
        command_exit_code=exit_code,
        query_hint=query_hint,
        extras=extras,
    )


def _strip_inline_markup(value: str) -> str:
    """Strip leading/trailing markdown markup (backticks, quotes).

    Receipt bullet bodies frequently quote values with single backticks
    (e.g., ```ok```), double quotes, or markdown link syntax. Strip the
    common decorations so downstream consumers see the bare value.
    """
    v = value.strip()
    # Strip outer triple-quoted, then double, then single backticks.
    for marker in ('```', '"', '`', "'"):
        if v.startswith(marker) and v.endswith(marker) and len(v) > 2 * len(marker):
            v = v[len(marker):-len(marker)].strip()
            break
    return v


def _consume_fenced_block(body_lines: list[str], start: int) -> Optional[str]:
    """Return the body of the first fenced code block at-or-after ``start``.

    Yields ``None`` if no fence is found before the next bullet / heading.
    """
    in_block = False
    out: list[str] = []
    for idx in range(start, len(body_lines)):
        raw = body_lines[idx]
        if _CODE_FENCE_RE.match(raw):
            if in_block:
                return "\n".join(out)
            in_block = True
            continue
        if in_block:
            out.append(raw)
        elif raw.lstrip().startswith(("- ", "### ", "## ")):
            return None
    if in_block and out:
        return "\n".join(out)
    return None


def _find_end_of_fenced_block(body_lines: list[str], start: int) -> int:
    """Return the index of the line just past the closing ``` of the first
    fenced block at-or-after ``start`` (or ``len(body_lines)`` if no fence
    is found).
    """
    in_block = False
    for idx in range(start, len(body_lines)):
        if _CODE_FENCE_RE.match(body_lines[idx]):
            if in_block:
                return idx + 1
            in_block = True
    return len(body_lines)


# ---------------------------------------------------------------------------
# T4 — Receipt -> Atlas query translator (code-path heuristic + doc-extension
# routing to T4a)
# ---------------------------------------------------------------------------

# Doc-anchored receipts route to T4a (doc_resolver.py) — find_code doesn't
# emit doc citations today (D-P2-10). Lowercase suffix matched against
# ``source_ref.path``.
DOC_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".log"})

# Atlas-query tool names that the runner / workspace_atlas_query CLI accepts.
_VALID_TOOLS = frozenset({"find_code", "scan_search", "auto"})


@dataclass(frozen=True)
class CodeQuery:
    """A code-anchored translation routed through ``runner.run_one``.

    The orchestrator (T5) builds the ``workspace run atlas-query`` argv
    from these fields (plus the daemon's pinned ``code_revision_id``) and
    consumes the resulting ``ShadowResponse.atlas_response.answer_text``.
    """

    tool: str  # "find_code" | "scan_search" | "auto"
    question: str
    receipt: "PacketReceipt"


@dataclass(frozen=True)
class DocQuery:
    """A doc-anchored translation routed through T4a doc_resolver.

    The orchestrator hands this to ``doc_resolver.resolve_doc_receipt`` to
    produce the resolved chunk text the grader compares against the oracle.
    """

    receipt: "PacketReceipt"


def _is_doc_path(path: Optional[str]) -> bool:
    if not path:
        return False
    lower = path.lower()
    return any(lower.endswith(ext) for ext in DOC_EXTENSIONS)


def _classify_code_tool(receipt: "PacketReceipt") -> str:
    """Pick the Atlas tool for a CODE-anchored receipt.

    Order:
      1. Per-receipt ``query_hint:`` override (when one of the valid tool
         names; invalid hints log and fall through).
      2. ``command_text`` heuristic — ``sed-range`` -> find_code,
         ``grep``/``rg`` -> scan_search.
      3. Default: find_code (matches RQ-3's default-Atlas-tool decision).
    """
    hint = (receipt.query_hint or "").strip().lower()
    if hint in _VALID_TOOLS:
        return hint
    cmd = (receipt.command_text or "").lower()
    if "sed-range" in cmd:
        return "find_code"
    # Match either the canonical wrapper form (`qa_lookup.sh grep ...`)
    # or a bare `grep` / `rg` token; tolerate quoting around the verb.
    if "qa_lookup.sh grep" in cmd or "qa_lookup.sh rg" in cmd:
        return "scan_search"
    # Bare grep / rg as the first non-space token.
    tokens = cmd.split()
    if tokens and tokens[0] in ("grep", "rg"):
        return "scan_search"
    return "find_code"


def translate_receipt_to_query(receipt: "PacketReceipt"):
    """Route a receipt to a CODE-anchored or doc-anchored translation.

    Doc-anchored receipts (extension in :data:`DOC_EXTENSIONS`) return a
    :class:`DocQuery` regardless of ``query_hint``: T4a is the only path
    that can resolve doc citations until CodePack v1.1 ships
    (D-P2-10 + D-P2-14). CODE-anchored receipts return a :class:`CodeQuery`
    with the tool chosen by :func:`_classify_code_tool`.

    The translator does NOT execute the query — that's the orchestrator's
    job. Translator output is pure / cacheable.
    """
    if _is_doc_path(receipt.source_path):
        return DocQuery(receipt=receipt)
    tool = _classify_code_tool(receipt)
    return CodeQuery(tool=tool, question=_code_receipt_query_text(receipt), receipt=receipt)


def _code_receipt_query_text(receipt: "PacketReceipt") -> str:
    """Build code-query text from the receipt plus its strongest anchors."""
    parts = [receipt.question.strip()]
    if receipt.query_hint:
        parts.append(f"query_hint: {receipt.query_hint.strip()}")
    if receipt.source_path:
        parts.append(f"source_path: {receipt.source_path.strip()}")
    if receipt.source_lines:
        parts.append(f"source_lines: {receipt.source_lines.strip()}")
    if receipt.command_text:
        parts.append(f"command_text: {receipt.command_text.strip()}")
    return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# T5 — PR grading orchestrator
# ---------------------------------------------------------------------------
#
# `handle_pr_event` is the entry point the receiver hands off to via
# FastAPI BackgroundTasks (T1). It runs through:
#
#   1. Token resolution (GITHUB_ATLAS_SHADOW_TOKEN).
#   2. PR files fetch -> packet detection (T2).
#   3. GH check_run create (in_progress).
#   4. Parent code_revision_id lookup (T6 — ledger.find_by_commit_sha).
#   5. Pin acquisition (T6 — state_file.acquire_pin).
#   6. Per-packet: fetch qna_log content (Contents API); parse receipts
#      (T3); per-receipt translate (T4) -> runner.run_one OR
#      doc_resolver.resolve_doc_receipt (T4a) -> grader.grade.
#   7. Build GradingSummary; render PR comment (T8); write artifact (T9).
#   8. Update GH check_run to completed (success / failure per threshold).
#   9. Pin release (in finally:).
#
# Errors at any step are logged + the check_run is updated to a
# `neutral` conclusion so PR authors see the gate isn't blocking them
# silently (D-P2-5: soft-pass on operational errors; hard-fail is reserved
# for genuine receipt failures below threshold).

from . import gh_check as gh_check_mod  # noqa: E402
from . import ledger as ledger_mod  # noqa: E402
from . import pr_comment as pr_comment_mod  # noqa: E402
from . import state_file as state_file_mod  # noqa: E402
from . import doc_resolver as doc_resolver_mod  # noqa: E402
from . import code_snapshot as code_snapshot_mod  # noqa: E402
from . import command_snapshot as command_snapshot_mod  # noqa: E402  # PR #20


def _fetch_pr_files(
    *,
    repo_full_name: str,
    pr_number: int,
    github_token: str,
    _http: Callable = gh_check_mod.http_request,
) -> list[dict[str, Any]]:
    """GET /repos/{owner}/{repo}/pulls/{number}/files (page 1, per_page=100).

    Returns the parsed JSON. v1 doesn't paginate; PRs touching >100 files
    are rare enough that single-page is OK to start.
    """
    url = (
        f"{gh_check_mod.GITHUB_API_BASE}/repos/{repo_full_name}/pulls/"
        f"{pr_number}/files?per_page=100"
    )
    resp = _http(
        method="GET",
        url=url,
        headers=gh_check_mod._auth_headers(github_token),
    )
    if not (200 <= resp.status < 300):
        raise RuntimeError(
            f"_fetch_pr_files failed: status={resp.status} body={resp.body[:500]!r}"
        )
    data = resp.json() or []
    return data if isinstance(data, list) else []


def _fetch_file_at_ref(
    *,
    repo_full_name: str,
    path: str,
    ref: str,
    github_token: str,
    _http: Callable = gh_check_mod.http_request,
) -> Optional[str]:
    """GET /repos/{owner}/{repo}/contents/{path}?ref={sha}. Returns the
    decoded file body, or None on 404 / non-base64 / unexpected shape.
    """
    url = (
        f"{gh_check_mod.GITHUB_API_BASE}/repos/{repo_full_name}/contents/"
        f"{path}?ref={ref}"
    )
    resp = _http(
        method="GET",
        url=url,
        headers=gh_check_mod._auth_headers(github_token),
    )
    if resp.status == 404:
        return None
    if not (200 <= resp.status < 300):
        raise RuntimeError(
            f"_fetch_file_at_ref failed: status={resp.status} body={resp.body[:500]!r}"
        )
    payload = resp.json() or {}
    if not isinstance(payload, dict):
        return None
    if payload.get("encoding") != "base64":
        # Large files (>1MB) come back without content; we don't grade those.
        return None
    raw = payload.get("content") or ""
    try:
        return base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _grade_one_receipt(
    *,
    cfg,
    receipt: PacketReceipt,
    code_revision_id: Optional[str],
    repo_full_name: str,
    run_commit: Optional[str] = None,
    revision_pin_mode: str = "event-base",
    _runner_run_one: Optional[Callable] = None,
    _doc_resolver: Callable = doc_resolver_mod.resolve_doc_receipt,
    _grader_grade: Optional[Callable] = None,
    _classify_skip: Optional[Callable] = None,
) -> "pr_comment_mod.ReceiptGradingRow":
    """Translate, dispatch to runner/doc_resolver, and grade one receipt.

    Injection seams (``_runner_run_one``, ``_doc_resolver``, ``_grader_grade``)
    keep this testable without standing up Atlas / a real DB / the
    Anthropic SDK.

    PR #15: ``run_commit`` is the grading-anchor commit (typically
    ``event.base_sha``). When provided + the receipt carries
    source_path+source_lines, a parallel snapshot at this commit lets
    the daemon distinguish "Atlas missed" from
    "run-commit-line-drift." Pass ``None`` to skip the second snapshot
    (e.g. callers without a grading commit available).

    When ``revision_pin_mode == "receipt-source"``, code-path receipts query
    Atlas at the ``code_revision_id`` recorded for ``receipt.source_commit``
    and use that same commit for the run snapshot. This is the
    apples-to-apples baseline mode. The live PR gate keeps the default
    ``event-base`` behavior.
    """
    translation = translate_receipt_to_query(receipt)

    artifact_id: Optional[str] = None
    chunk_id: Optional[str] = None
    revision_binding: Optional[str] = None
    heading_path: Optional[list[str]] = None
    warnings: list[str] = []
    atlas_answer_text = ""
    atlas_returncode: Optional[int] = None
    atlas_exception: Optional[str] = None
    atlas_stderr_head: Optional[str] = None
    # PR #17: receipt-side snapshot resolves for ALL receipts (doc +
    # code) so the unavailable-source-ref skip can apply to both.
    # Resolver short-circuits internally on receipts without
    # source_path/source_commit, so this is safe for non-source
    # receipts too.
    source_snapshot = code_snapshot_mod.resolve_code_receipt_snapshot(
        receipt,
        repo_path=cfg.core_repo_path,
    )
    # PR #20: command_snapshot lane — runs a deterministic git-backed
    # verification of receipt.command_text (or a synthesized command
    # from source_path/source_lines) BEFORE atlas dispatch. When the
    # result is decisive (match / mismatch / source_missing /
    # absence-verified), `_classify_pre_atlas_skip` short-circuits and
    # we never call atlas. Unsupported / errored command_text falls
    # through to normal routing.
    command_snapshot = command_snapshot_mod.resolve_command_snapshot(
        receipt,
        repo_path=cfg.core_repo_path,
    )
    run_snapshot = None  # PR #15: still only resolved on the code path
    # PR #16: raw retrieval diagnostics. Initialized to the "no
    # raw_result available" shape so the doc_resolver path (which
    # doesn't go through the runner) lands well-defined Nones in the
    # row rather than missing fields.
    atlas_diagnostics: dict[str, Any] = _atlas_raw_result_diagnostics(None)
    tool_label = ""
    # The Atlas revision actually queried for this receipt. Live PR grading
    # keeps the event/base revision. Offline baselines can override these on
    # the code path to the receipt's own source_commit revision.
    effective_code_revision_id = code_revision_id
    effective_run_commit = run_commit
    effective_atlas_commit_sha = run_commit

    # PR #17: pre-atlas skip check. Routes non-retrieval receipts
    # (external_tool_docs / user_context / absence_search), receipts
    # under PR #277's docs/work/** exclusion, and receipts whose
    # source can't be materialized straight to a skipped row without
    # spending an atlas query. ``_classify_skip`` is the injection
    # seam — tests focused on post-atlas behavior can pass
    # ``lambda *_, **__: None`` to disable the skip and exercise the
    # full grading flow.
    classify_skip = _classify_skip or _classify_pre_atlas_skip
    pre_skip = classify_skip(
        receipt,
        source_snapshot=source_snapshot,
        translation=translation,  # PR #18 review: doc receipts bypass the snapshot pre-skip
        command_snapshot=command_snapshot,  # PR #20
    )
    if pre_skip is not None:
        return _build_pre_atlas_skip_row(
            receipt=receipt,
            source_snapshot=source_snapshot,
            command_snapshot=command_snapshot,
            score_status=pre_skip[0],
            clean_excluded_reason=pre_skip[1],
            atlas_code_revision_id=None,
            atlas_commit_sha=None,
        )

    if isinstance(translation, DocQuery):
        # T4a path. `repo` in the doc_id is whatever P1's SCIP-path
        # ingest passed to the CLI — that's `cfg.repo_url` (typically a
        # full https URL like "https://github.com/tandemstream/core"),
        # NOT the GitHub `owner/name` slug. Using `event.repo_full_name`
        # here would mint `tandemstream/core@<sha>:<path>` and miss every
        # doc artifact whose `doc_id` was written with the URL form
        # (codex r8 finding). Operators whose ingest used a different
        # `repo_url` configure that via shadow-config.yaml.
        result = _doc_resolver(
            receipt,
            org_id=cfg.continuous_shadow_org_id,
            repo=cfg.repo_url,
            repo_path=cfg.core_repo_path,
        )
        atlas_answer_text = result.raw_text or ""
        tool_label = "doc_resolver"
        revision_binding = result.revision_binding
        artifact_id = result.artifact_id
        chunk_id = result.chunk_id
        heading_path = result.heading_path
        warnings = list(result.warnings or [])
    else:
        # T4 code path — shell out via runner.run_one.
        from atlas_shadow import runner as runner_mod  # lazy import (heavy)
        from atlas_shadow.parser import Receipt as RunnerReceipt

        if revision_pin_mode == "receipt-source":
            if not receipt.source_commit:
                return _build_pre_atlas_skip_row(
                    receipt=receipt,
                    source_snapshot=source_snapshot,
                    command_snapshot=command_snapshot,
                    score_status="skipped_revision_not_indexed",
                    clean_excluded_reason="source_commit_missing",
                    atlas_code_revision_id=None,
                    atlas_commit_sha=None,
                )
            resolved_source_commit = _resolve_commit_for_ledger(
                cfg.core_repo_path,
                receipt.source_commit,
            )
            revision_row = ledger_mod.find_by_commit_sha(
                cfg.db_path,
                resolved_source_commit,
            )
            effective_code_revision_id = (
                str(revision_row.get("code_revision_id"))
                if revision_row and revision_row.get("code_revision_id")
                else None
            )
            effective_run_commit = resolved_source_commit
            effective_atlas_commit_sha = resolved_source_commit
            if effective_code_revision_id is None:
                return _build_pre_atlas_skip_row(
                    receipt=receipt,
                    source_snapshot=source_snapshot,
                    command_snapshot=command_snapshot,
                    score_status="skipped_revision_not_indexed",
                    clean_excluded_reason="revision_not_indexed",
                    atlas_code_revision_id=None,
                    atlas_commit_sha=resolved_source_commit,
                )
        elif revision_pin_mode != "event-base":
            raise ValueError(
                f"unknown revision_pin_mode={revision_pin_mode!r}; "
                "expected 'event-base' or 'receipt-source'"
            )

        runner_receipt = RunnerReceipt(
            question_id=receipt.question_id,
            question=translation.question,
            oracle_excerpt=receipt.oracle_excerpt,
            oracle_claim=receipt.oracle_claim,
            source_path=receipt.source_path,
            source_lines=receipt.source_lines,
            commit_sha=receipt.source_commit,
            class_label=None,
        )
        run_one = _runner_run_one or runner_mod.run_one
        shadow_response = run_one(
            runner_receipt,
            fixture_id="pr-packet",
            org_id=cfg.continuous_shadow_org_id,
            core_repo_path=cfg.core_repo_path,
            tool=translation.tool,
            principal_id=cfg.default_principal_id,
            domain_pack="code",
            code_revision_id=effective_code_revision_id,
        )
        atlas_response = shadow_response.atlas_response
        atlas_answer_text = atlas_response.answer_text or ""
        atlas_returncode = atlas_response.returncode
        atlas_exception = atlas_response.exception
        atlas_stderr_head = (atlas_response.stderr or "")[:1000] or None
        # PR #16: extract compact retrieval diagnostics from the raw
        # workspace_atlas_query JSON. Doc-resolver path keeps the
        # ``_atlas_raw_result_diagnostics(None)`` default initialized
        # above (it has no atlas raw_result to extract from).
        atlas_diagnostics = _atlas_raw_result_diagnostics(
            atlas_response.raw_result
        )
        # ``source_snapshot`` already resolved before the pre-atlas
        # skip check above. PR #15: still parallel-snapshot at the
        # grading run commit on the code path so the run-drift skip
        # can fire.
        run_snapshot = code_snapshot_mod.resolve_code_receipt_run_snapshot(
            receipt,
            repo_path=cfg.core_repo_path,
            run_commit=effective_run_commit,
        )
        tool_label = translation.tool

    # Grade
    from atlas_shadow import grader as grader_mod  # lazy import
    grade_fn = _grader_grade or grader_mod.grade
    try:
        grading = grade_fn(
            question=receipt.question,
            oracle_excerpt=receipt.oracle_excerpt,
            oracle_claim=receipt.oracle_claim,
            atlas_answer_text=atlas_answer_text,
            model=cfg.grader_model,
        )
    except Exception as exc:  # noqa: BLE001 — preserve Atlas diagnostics
        # The grader threw before producing a verdict. Grade is forced
        # to no_match per existing behavior, but lane + score_status
        # still apply — the receipt's anchor shape is known and both
        # snapshots were already resolved upstream.
        snap_status = (
            source_snapshot.status if source_snapshot is not None else None
        )
        run_snap_status = (
            run_snapshot.status if run_snapshot is not None else None
        )
        lane = _infer_lane(tool_label=tool_label, receipt=receipt)
        score_status, clean_reason = _derive_score_status(
            grade="no_match",
            source_snapshot_status=snap_status,
            run_snapshot_status=run_snap_status,
            revision_binding=revision_binding,
        )
        return pr_comment_mod.ReceiptGradingRow(
            question_id=receipt.question_id,
            question=receipt.question,
            grade="no_match",
            confidence=0.0,
            rationale=f"grading_error: {type(exc).__name__}: {exc}",
            tool=tool_label or "error",
            revision_binding=revision_binding,
            artifact_id=artifact_id,
            chunk_id=chunk_id,
            heading_path=heading_path,
            warnings=[*warnings, f"exception:{type(exc).__name__}"],
            atlas_answer_len=len(atlas_answer_text or ""),
            atlas_returncode=atlas_returncode,
            atlas_exception=atlas_exception,
            atlas_stderr_head=atlas_stderr_head,
            atlas_retrieval_plan=atlas_diagnostics["retrieval_plan"],
            atlas_citation_locations=atlas_diagnostics["citation_locations"],
            atlas_citation_count=atlas_diagnostics["citation_count"],
            atlas_reranker_candidates_considered=(
                atlas_diagnostics["reranker_candidates_considered"]
            ),
            atlas_reranker_top_k_count=atlas_diagnostics["reranker_top_k_count"],
            source_snapshot_status=snap_status,
            source_snapshot_hash_match=(
                source_snapshot.hash_match if source_snapshot is not None else None
            ),
            source_snapshot_sha256=(
                source_snapshot.resolved_sha256 if source_snapshot is not None else None
            ),
            run_snapshot_status=run_snap_status,
            run_snapshot_hash_match=(
                run_snapshot.hash_match if run_snapshot is not None else None
            ),
            run_snapshot_sha256=(
                run_snapshot.resolved_sha256 if run_snapshot is not None else None
            ),
            evidence_type=receipt.evidence_type,
            lane=lane,
            score_status=score_status,
            clean_excluded_reason=clean_reason,
            atlas_code_revision_id=effective_code_revision_id,
            atlas_commit_sha=effective_atlas_commit_sha,
        )

    snap_status = (
        source_snapshot.status if source_snapshot is not None else None
    )
    run_snap_status = (
        run_snapshot.status if run_snapshot is not None else None
    )
    lane = _infer_lane(tool_label=tool_label, receipt=receipt)
    score_status, clean_reason = _derive_score_status(
        grade=grading.grade,
        source_snapshot_status=snap_status,
        run_snapshot_status=run_snap_status,
        revision_binding=revision_binding,
    )
    return pr_comment_mod.ReceiptGradingRow(
        question_id=receipt.question_id,
        question=receipt.question,
        grade=grading.grade,
        confidence=float(grading.confidence),
        rationale=grading.rationale,
        tool=tool_label,
        revision_binding=revision_binding,
        artifact_id=artifact_id,
        chunk_id=chunk_id,
        heading_path=heading_path,
        warnings=warnings,
        atlas_answer_len=len(atlas_answer_text or ""),
        atlas_returncode=atlas_returncode,
        atlas_exception=atlas_exception,
        atlas_stderr_head=atlas_stderr_head,
        atlas_retrieval_plan=atlas_diagnostics["retrieval_plan"],
        atlas_citation_locations=atlas_diagnostics["citation_locations"],
        atlas_citation_count=atlas_diagnostics["citation_count"],
        atlas_reranker_candidates_considered=(
            atlas_diagnostics["reranker_candidates_considered"]
        ),
        atlas_reranker_top_k_count=atlas_diagnostics["reranker_top_k_count"],
        source_snapshot_status=snap_status,
        source_snapshot_hash_match=(
            source_snapshot.hash_match if source_snapshot is not None else None
        ),
        source_snapshot_sha256=(
            source_snapshot.resolved_sha256 if source_snapshot is not None else None
        ),
        run_snapshot_status=run_snap_status,
        run_snapshot_hash_match=(
            run_snapshot.hash_match if run_snapshot is not None else None
        ),
        run_snapshot_sha256=(
            run_snapshot.resolved_sha256 if run_snapshot is not None else None
        ),
        evidence_type=receipt.evidence_type,
        lane=lane,
        score_status=score_status,
        clean_excluded_reason=clean_reason,
        atlas_code_revision_id=effective_code_revision_id,
        atlas_commit_sha=effective_atlas_commit_sha,
        # PR #20: surface command_snapshot diagnostics even when the
        # row went through atlas (e.g. command_text was UNSUPPORTED or
        # ERROR — useful diagnostic so consumers can see "command lane
        # was tried but didn't apply").
        command_snapshot_status=(
            command_snapshot.status if command_snapshot is not None else None
        ),
        command_snapshot_hash_match=(
            command_snapshot.hash_match if command_snapshot is not None else None
        ),
        command_snapshot_sha256=(
            command_snapshot.resolved_sha256 if command_snapshot is not None else None
        ),
        command_snapshot_head=(
            (command_snapshot.output_head or None)
            if command_snapshot is not None else None
        ),
        command_snapshot_exit_code=(
            command_snapshot.exit_code if command_snapshot is not None else None
        ),
    )


def _atlas_raw_result_diagnostics(raw_result: Any) -> dict[str, Any]:
    """Extract compact retrieval diagnostics from the
    ``workspace_atlas_query`` JSON payload (PR #16).

    The runner already captures ``raw_result`` from the atlas-side
    response. We pull a small set of fields onto the row so downstream
    classifiers (and future ground-truthed lane inference) don't have
    to re-issue queries or parse rationale text:

      - ``retrieval_plan`` — atlas's plan dict (``lanes_run``,
        ``lanes_skipped``, ``boosts``, ``lane_quotas_applied``,
        ``path_anchors``, ``symbol_anchors``, …). Persisted untouched.
      - ``citation_locations`` — first 20 citations in compact
        ``"path:line_start-line_end"`` form (or bare path when no
        lines). The artifact stays human-readable + small.
      - ``citation_count`` — full untruncated total so consumers can
        tell when ``citation_locations`` was head-sampled.
      - ``reranker_candidates_considered`` — how many candidates the
        reranker scored. Useful for fuzzy-lane "candidate set was tiny"
        diagnostics.
      - ``reranker_top_k_count`` — how many of those made the top-k
        cut atlas applies after reranking.

    Robust to missing / malformed ``raw_result`` shapes: any field that
    isn't a dict / list of the expected shape collapses to None or an
    empty list rather than raising. The grader's main path catches
    exceptions anyway, but extracting diagnostics from a partial
    response shouldn't itself break grading.

    No interpretation here — the row carries the raw signal. Lane
    inference still happens in ``_infer_lane``; this helper just makes
    the data available for it to consult in a future PR.
    """
    if not isinstance(raw_result, dict):
        return {
            "retrieval_plan": None,
            "citation_locations": [],
            "citation_count": None,
            "reranker_candidates_considered": None,
            "reranker_top_k_count": None,
        }

    citations = raw_result.get("citations") or []
    citation_locations: list[str] = []
    citation_count: Optional[int] = None
    if isinstance(citations, list):
        citation_count = len(citations)
        for citation in citations[:20]:
            if not isinstance(citation, dict):
                continue
            file_path = str(citation.get("file_path") or "")
            line_start = citation.get("line_start")
            line_end = citation.get("line_end")
            if file_path and line_start and line_end:
                citation_locations.append(f"{file_path}:{line_start}-{line_end}")
            elif file_path:
                citation_locations.append(file_path)

    trace = raw_result.get("reranker_trace") or {}
    top_k = trace.get("top_k") if isinstance(trace, dict) else None
    retrieval_plan = raw_result.get("retrieval_plan")
    return {
        "retrieval_plan": (
            retrieval_plan if isinstance(retrieval_plan, dict) else None
        ),
        "citation_locations": citation_locations,
        "citation_count": citation_count,
        "reranker_candidates_considered": (
            trace.get("candidates_considered") if isinstance(trace, dict) else None
        ),
        "reranker_top_k_count": len(top_k) if isinstance(top_k, list) else None,
    }


def _resolve_commit_for_ledger(repo_path: Path, commit_ref: str) -> str:
    """Resolve a receipt's commit ref to a full SHA for ledger lookups.

    Historical receipts often store short SHAs such as ``408858a``. Git
    accepts those for ``git show``, but the shadow ingest ledger stores
    full 40-character SHAs. If resolution fails, return the original ref
    so the row is cleanly reported as revision-not-indexed.
    """
    ref = (commit_ref or "").strip()
    if not ref:
        return ref
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ref
    if proc.returncode != 0:
        return ref
    resolved = proc.stdout.strip()
    return resolved or ref


# ─── PR #17: pre-atlas skip classification ─────────────────────────────
#
# A receipt is "pre-atlas skipped" when it falls into a category the
# daemon shouldn't even ask Atlas about — either because the question
# is by-construction non-retrieval, the receipt's source can't be
# materialized, or the receipt's corpus is deliberately excluded by an
# upstream policy. Skipping BEFORE calling atlas saves the runner
# subprocess and gives the score_status a clean, machine-readable
# reason that the clean denominator can drop.

# Evidence types where the receipt cites information outside the
# shadow corpus's repo entirely. find_code / scan_search can't be
# expected to retrieve answers from Claude Code docs or user-provided
# empirical data.
_NON_REPO_EVIDENCE_TYPES = frozenset({"external_tool_docs", "user_context"})

# Evidence types claiming "X does NOT exist in the repo." LLM-grader-
# friendly retrieval surfaces can't prove a negative; absence checks
# need a deterministic grep tool. Surfaced as a distinct skip from
# `_NON_REPO_EVIDENCE_TYPES` because the upstream fix is different.
_ABSENCE_SEARCH_EVIDENCE_TYPES = frozenset({"absence_search"})

# Path prefix(es) the doc-ingest pipeline deliberately excludes from
# the shadow corpus (PR #277 — prevents grading ground-truth leakage).
# A doc_resolver miss on a path under this prefix is by-design, not a
# retrieval failure.
_DOCS_WORK_EXCLUDED_PREFIXES = ("docs/work/",)


def _classify_command_snapshot_outcome(
    command_snapshot: Optional[Any],
) -> Optional[tuple[str, str]]:
    """PR #20: translate a :class:`command_snapshot.CommandSnapshotResult`
    into a ``(score_status, clean_excluded_reason)`` pair.

    Returns ``None`` when command_snapshot didn't decisively classify
    the receipt — the caller should fall through to the existing
    pre-atlas skip logic / atlas dispatch.

    Mapping (per Codex's PR #20 brief):

      - ``command_snapshot_match`` /
        ``command_snapshot_no_match_expected_absent`` →
        ``("skipped_command_snapshot", "command_snapshot")``. Receipt
        verified by deterministic source check; atlas wasn't needed.
      - ``command_snapshot_mismatch`` /
        ``command_snapshot_found_but_expected_absent`` →
        ``("skipped_unavailable_source_ref", "unavailable_source_ref")``.
        Receipt contradicted or unresolvable at the pinned commit —
        atlas wasn't being tested fairly.
      - ``command_snapshot_source_missing`` /
        ``command_snapshot_unsupported`` / ``command_snapshot_error`` /
        ``None`` → fall through (None return). Source-missing is not
        decisive here because code_snapshot may resolve Atlas package
        aliases that command_snapshot cannot.
    """
    if command_snapshot is None:
        return None
    status = getattr(command_snapshot, "status", None)
    if status in (
        command_snapshot_mod.STATUS_MATCH,
        command_snapshot_mod.STATUS_NO_MATCH_EXPECTED_ABSENT,
    ):
        return ("skipped_command_snapshot", "command_snapshot")
    if status in (
        command_snapshot_mod.STATUS_MISMATCH,
        command_snapshot_mod.STATUS_FOUND_BUT_EXPECTED_ABSENT,
    ):
        return (
            "skipped_unavailable_source_ref",
            "unavailable_source_ref",
        )
    return None


def _classify_pre_atlas_skip(
    receipt: PacketReceipt,
    *,
    source_snapshot: Optional[Any] = None,
    translation: Optional[Any] = None,
    command_snapshot: Optional[Any] = None,
) -> Optional[tuple[str, str]]:
    """Decide whether this receipt should be skipped BEFORE calling
    atlas. PR #17.

    Returns ``(score_status, clean_excluded_reason)`` when the receipt
    qualifies for a pre-atlas skip; ``None`` when it should proceed
    through the normal routing path (find_code / scan_search /
    doc_resolver).

    Order matters — narrower categories first so a receipt that
    technically falls into multiple skip buckets gets the most
    specific status:

      1. ``skipped_doc_corpus_excluded`` — source_path in docs/work/**
         (PR #277 exclusion). Path-based policy, deterministic.
      2. ``skipped_non_repo_evidence`` — evidence_type in
         {external_tool_docs, user_context}. The receipt's claim lives
         outside the repo entirely.
      3. ``skipped_absence_search`` — evidence_type = absence_search.
         Receipt claims a negative.
      4. ``skipped_unavailable_source_ref`` — code receipt whose
         source can't be materialized via raw git
         (``source_snapshot_status=git_source_missing``).

    **PR #18 review fix (Codex):** the snapshot-based check only
    applies to receipts that would route to the CODE path
    (CodeQuery — find_code / scan_search). DocQuery receipts get
    deferred to doc_resolver so the DB-based + alias-aware
    resolution path can run first; their "unresolvable" classification
    happens post-resolver via ``_derive_score_status`` consulting
    ``revision_binding``. Without this gating, doc receipts whose raw
    receipt path doesn't render in git (e.g. ``Atlas/docs/...`` that's
    actually stored at ``products/tandem/packages/python/atlas/docs/...``
    in the corpus) would get pre-skipped even though doc_resolver
    could resolve them via DB or alias.

    Pass-grades never reach this helper — the call site invokes it
    BEFORE atlas dispatch, so by definition no grade exists yet.
    """
    source_path = (receipt.source_path or "").strip()
    if source_path:
        # docs/work/** wins first — path-based policy, applies
        # regardless of command_text / evidence_type / snapshot state.
        if any(prefix in source_path for prefix in _DOCS_WORK_EXCLUDED_PREFIXES):
            return ("skipped_doc_corpus_excluded", "doc_corpus_excluded")

    # PR #20: command_snapshot has higher priority than evidence_type-
    # based skips. For an absence_search receipt with a grep command,
    # we want ``skipped_command_snapshot`` (verified locally) rather
    # than the generic ``skipped_absence_search`` fallback. A
    # source_excerpt receipt with a sed-range command similarly
    # short-circuits before the snapshot-based skip below.
    cmd_outcome = _classify_command_snapshot_outcome(command_snapshot)
    if cmd_outcome is not None:
        return cmd_outcome

    evidence_type = (receipt.evidence_type or "").strip()
    if evidence_type in _NON_REPO_EVIDENCE_TYPES:
        return ("skipped_non_repo_evidence", "non_repo_evidence")
    if evidence_type in _ABSENCE_SEARCH_EVIDENCE_TYPES:
        return ("skipped_absence_search", "absence_search")

    # Source-materialization check: CODE-PATH ONLY. Doc receipts defer
    # to doc_resolver (DB-aware) — see PR #18 review note in docstring.
    is_doc_query = isinstance(translation, DocQuery)
    if not is_doc_query and source_snapshot is not None:
        snap_status = getattr(source_snapshot, "status", None)
        if snap_status == "git_source_missing":
            return (
                "skipped_unavailable_source_ref",
                "unavailable_source_ref",
            )

    return None


def _build_pre_atlas_skip_row(
    *,
    receipt: PacketReceipt,
    source_snapshot: Optional[Any],
    score_status: str,
    clean_excluded_reason: str,
    command_snapshot: Optional[Any] = None,
    atlas_code_revision_id: Optional[str] = None,
    atlas_commit_sha: Optional[str] = None,
) -> "pr_comment_mod.ReceiptGradingRow":
    """Construct a ``ReceiptGradingRow`` for a receipt that's being
    skipped before any atlas dispatch (PR #17).

    The row exists for accounting — it shows up in the per-packet JSON
    artifact + counts toward ``total`` so operators can see WHICH
    receipts were skipped and why — but ``score_status != "counted"``
    drops it from the clean denominator.

    Grading verdict policy:
      - ``grade`` is ``"atlas_not_found"`` (the narrowest existing
        enum value that semantically fits "atlas returned nothing,"
        which is true here — we didn't ask).
      - ``tool`` is ``"skipped"`` so downstream tool-distribution
        analyses don't conflate these with real find_code /
        scan_search / doc_resolver calls.
      - ``lane`` is ``"non_retrieval"`` — a new lane value for rows
        that never went through any retrieval surface.
      - ``rationale`` records the human-readable reason so the
        PR-comment markdown shows operators the skip cause.

    Per Codex's PR #17 spec: grade enum stays narrow; all the
    bookkeeping happens via ``score_status`` and
    ``clean_excluded_reason``.
    """
    rationale = (
        f"Skipped pre-atlas: {clean_excluded_reason} "
        f"(evidence_type={receipt.evidence_type or 'n/a'}, "
        f"source_path={receipt.source_path or 'n/a'})"
    )
    snap_status = (
        source_snapshot.status if source_snapshot is not None else None
    )
    snap_hash_match = (
        source_snapshot.hash_match if source_snapshot is not None else None
    )
    snap_sha256 = (
        source_snapshot.resolved_sha256 if source_snapshot is not None else None
    )
    # PR #20: surface command_snapshot fields if the skip was driven by
    # the command lane (or just happened to have run).
    cs_status = getattr(command_snapshot, "status", None)
    cs_hash_match = getattr(command_snapshot, "hash_match", None)
    cs_sha256 = getattr(command_snapshot, "resolved_sha256", None)
    cs_head = getattr(command_snapshot, "output_head", None) or None
    cs_exit = getattr(command_snapshot, "exit_code", None)
    return pr_comment_mod.ReceiptGradingRow(
        question_id=receipt.question_id,
        question=receipt.question,
        grade="atlas_not_found",
        confidence=1.0,
        rationale=rationale,
        tool="skipped",
        warnings=[],
        atlas_answer_len=0,
        atlas_returncode=None,
        atlas_exception=None,
        atlas_stderr_head=None,
        atlas_retrieval_plan=None,
        atlas_citation_locations=[],
        atlas_citation_count=None,
        atlas_reranker_candidates_considered=None,
        atlas_reranker_top_k_count=None,
        source_snapshot_status=snap_status,
        source_snapshot_hash_match=snap_hash_match,
        source_snapshot_sha256=snap_sha256,
        run_snapshot_status=None,  # not resolved on the skip path
        run_snapshot_hash_match=None,
        run_snapshot_sha256=None,
        evidence_type=receipt.evidence_type,
        lane="non_retrieval",
        score_status=score_status,
        clean_excluded_reason=clean_excluded_reason,
        atlas_code_revision_id=atlas_code_revision_id,
        atlas_commit_sha=atlas_commit_sha,
        # PR #20: command_snapshot diagnostics — populated when the
        # skip was driven by the command lane.
        command_snapshot_status=cs_status,
        command_snapshot_hash_match=cs_hash_match,
        command_snapshot_sha256=cs_sha256,
        command_snapshot_head=cs_head,
        command_snapshot_exit_code=cs_exit,
    )


def _infer_lane(
    *,
    tool_label: str,
    receipt: PacketReceipt,
) -> str:
    """Infer the retrieval lane this receipt was scored on.

    Per Codex's PR #14 design note, the lane is persisted on the row
    rather than re-inferred downstream. v1 inference uses:

      1. ``tool_label`` (the dispatch we made: doc_resolver /
         scan_search / find_code).
      2. The receipt's anchor shape — find_code w/ both
         ``source_path`` AND ``source_lines`` is PR #426 fast-path
         eligible.

    Returns one of:
      - ``"doc_resolver"``
      - ``"scan_search"``
      - ``"explicit_source_fast_path"`` — find_code receipt with
        path+lines anchor
      - ``"fuzzy_find_code"`` — find_code receipt w/o anchor (or
        tool_label fell through to error)

    A future iteration should consult the atlas response's
    ``retrieval_plan.explicit_source_fast_path`` flag for ground
    truth — that requires plumbing ``atlas_retrieval_plan`` from the
    runner first (separate PR).
    """
    if tool_label == "doc_resolver":
        return "doc_resolver"
    if tool_label == "scan_search":
        return "scan_search"
    has_path = bool((receipt.source_path or "").strip())
    has_lines = bool((receipt.source_lines or "").strip())
    if has_path and has_lines:
        return "explicit_source_fast_path"
    return "fuzzy_find_code"


def _derive_score_status(
    *,
    grade: str,
    source_snapshot_status: Optional[str],
    run_snapshot_status: Optional[str] = None,
    revision_binding: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Decide whether this row should count toward the clean denominator.

    Returns ``(score_status, clean_excluded_reason)``. Order of checks
    matters — narrower (receipt-stale) wins over broader (run-commit
    line drift) when both apply.

      - ``("skipped_receipt_stale", "receipt_stale")`` (PR #14): the
        cited path/lines don't exist at the receipt's pinned commit
        (``source_snapshot_status == "git_source_missing"``) AND the
        grader said ``no_match``. Atlas isn't being measured on a real
        receipt — the receipt itself drifted at authoring time.

      - ``("skipped_receipt_stale", "receipt_hash_mismatch")``: the
        cited path/lines render at the receipt's pinned commit, but the
        rendered bytes do not match the receipt's own excerpt hash
        (``source_snapshot_status == "git_source_hash_mismatch"``).
        This is the same clean-denominator class as source-missing
        receipt drift: the receipt anchor is internally inconsistent,
        so Atlas retrieval should not be penalized.

      - ``("skipped_run_commit_line_drift", "run_commit_line_drift")``
        (PR #15): the receipt-commit snapshot matched (receipt is
        internally consistent at authoring time), but the run-commit
        snapshot doesn't (the file was edited between receipt commit
        and run commit, so the cited line numbers now point at
        different code) AND the grader said ``no_match``. Atlas
        returned the right line range at the run commit — but those
        lines now contain different content than the receipt
        described. Not an Atlas miss; the receipt's line anchor is the
        moving target.

      - ``("counted", None)`` is the default — row participates in
        both ``raw_pass_pct`` and ``clean_pass_pct`` denominators.

    Future statuses (e.g. ``skipped_doc_corpus_excluded``,
    ``skipped_non_repo_evidence``) plug in here without touching the
    ``grade`` enum.

    Per Codex's PR #14 design note: ``grade`` stays narrow
    (full_match | partial_match | no_match | atlas_not_found). The
    skip is bookkeeping on a separate field, not a fifth grade value.

    Codex PR #16 review note: both ``no_match`` AND ``atlas_not_found``
    are failure grades that the skip paths should cover. In the real
    PR #15 probe, q12 came back as ``atlas_not_found`` (atlas's exact-
    source fast path returned an empty answer rather than a wrong-
    content answer), so a `grade != "no_match"` predicate left the
    drift skip dormant for exactly the case PR #15 was designed to
    catch. The fix treats both failure grades the same — pass-grades
    are still never flipped.
    """
    if grade not in {"no_match", "atlas_not_found"}:
        return ("counted", None)

    # PR #18 review fix: doc_resolver had the authoritative say on
    # whether the receipt's source could be resolved via DB + alias.
    # When it explicitly returned the "unresolved" binding, the
    # receipt was unreachable even after the alias path tried — this
    # is the doc-side analog of the code-side pre-atlas
    # ``skipped_unavailable_source_ref`` skip. Caught here (after
    # grading) because pre-atlas can't know what doc_resolver would
    # have done.
    #
    # PR #19 fix: ``doc_resolver`` emits ``BINDING_NONE = "none"`` (a
    # string sentinel) on its DocResolverResult.revision_binding field
    # when both the DB lookup AND the git fallback fail. We also
    # accept the longer ``"unresolved_source_ref"`` form for forward-
    # compatibility / direct testing.
    if revision_binding in ("none", "unresolved_source_ref"):
        return (
            "skipped_unavailable_source_ref",
            "unavailable_source_ref",
        )
    # Receipt-side stale takes precedence — atlas was never measured on
    # a renderable receipt commit. Code-path defense-in-depth: in
    # production code receipts hit the pre-atlas skip first and never
    # reach this branch. Doc receipts skip via the revision_binding
    # check above. PR #18 review: when ``revision_binding`` indicates
    # the doc_resolver DID resolve (``db_commit_scoped`` /
    # ``git_receipt_snapshot``), don't flip to receipt-stale just
    # because raw git couldn't materialize the alias path — that would
    # undo doc_resolver's alias-aware resolution.
    resolver_resolved = revision_binding in (
        "db_commit_scoped",
        "git_receipt_snapshot",
    )
    if source_snapshot_status == "git_source_missing" and not resolver_resolved:
        return ("skipped_receipt_stale", "receipt_stale")
    if source_snapshot_status == "git_source_hash_mismatch" and not resolver_resolved:
        return ("skipped_receipt_stale", "receipt_hash_mismatch")
    # Run-commit drift: receipt matched at authoring, but at the
    # grading commit the same path/lines either render different bytes
    # (``run_commit_hash_mismatch``) or the file/path is gone entirely
    # (``run_commit_source_missing`` — deleted, renamed, etc.). Both
    # are the same class of non-measurement: atlas isn't being graded
    # against the receipt as authored. Codex's PR #15 review note —
    # the classifier already buckets both as ``run_commit_line_drift``;
    # the scorer needs to match. We require an explicit receipt-snap
    # match before excluding so the receipt itself isn't the issue.
    if (
        source_snapshot_status == "git_source_hash_match"
        and run_snapshot_status in (
            "run_commit_hash_mismatch",
            "run_commit_source_missing",
        )
    ):
        return ("skipped_run_commit_line_drift", "run_commit_line_drift")
    return ("counted", None)


def run_pr_grading(
    cfg,
    event,
    *,
    github_token: str,
    revision_pin_mode: str = "event-base",
    _fetch_pr_files: Callable = _fetch_pr_files,
    _fetch_file_at_ref: Callable = _fetch_file_at_ref,
    _post_pending: Callable = gh_check_mod.post_pending_status,
    _post_final: Callable = gh_check_mod.post_final_status,
    _post_comment: Callable = pr_comment_mod.post_or_update_pr_comment,
    _grade_one: Callable = _grade_one_receipt,
    _now_iso: Callable = lambda: datetime.now(timezone.utc).isoformat(),
) -> dict[str, Any]:
    """Run the full pre-merge grading pipeline for one PR event.

    Returns a dict summarizing what happened — useful for tests + log
    enrichment. NEVER raises; operational failures surface as a
    ``status='error'`` field with the exception text.

    GH integration uses the Commit Statuses API (not Check Runs) because
    PATs can't create check_runs (T12 smoketest 2026-05-15: the Check
    Runs API returns 403 with ``You must authenticate via a GitHub App``).
    Commit statuses display in the same PR ``Checks`` UI tab and
    integrate with branch protection ``Settings -> Branches -> Required
    status checks`` via the ``atlas-shadow-grading`` context.

    ``revision_pin_mode``:
      - ``event-base`` (default): every receipt queries the event base SHA's
        code_revision_id. This preserves live PR-gate behavior.
      - ``receipt-source``: code receipts query the code_revision_id for the
        receipt's own ``source_commit``. This is the canonical offline
        baseline mode because Planner and Atlas evidence are scored against
        the same snapshot.
    """
    outcome: dict[str, Any] = {
        "pr_number": event.pr_number,
        "base_sha": event.base_sha,
        "head_sha": event.head_sha,
        "repo_full_name": event.repo_full_name,
        "summaries": [],
        "status_state": None,
        "status": "ok",
        "error": None,
    }
    pending_posted = False
    pin_acquired = False
    code_revision_id: Optional[str] = None
    try:
        pr_files = _fetch_pr_files(
            repo_full_name=event.repo_full_name,
            pr_number=event.pr_number,
            github_token=github_token,
        )
        touched = [
            f["filename"]
            for f in pr_files
            if isinstance(f, dict)
            and f.get("filename")
            and f.get("status") != "removed"
        ]
        qna_log_paths = detect_packet_qna_log(touched)
        outcome["packet_paths"] = qna_log_paths
        if not qna_log_paths:
            # Codex review r7 (2026-05-15): non-packet PRs must get a
            # terminal status when `atlas-shadow-grading` is configured
            # as a required status check on the repo's branch protection
            # rules — branch protection's "required status" is per-
            # context globally, not per-changed-files, so an absent
            # status leaves ordinary PRs unmergeable. Post a soft-pass
            # `state=success` so non-packet PRs aren't blocked by this
            # gate. The description makes the no-op grading explicit.
            outcome["status"] = "skipped_not_packet"
            try:
                _post_final(
                    repo_full_name=event.repo_full_name,
                    head_sha=event.head_sha,
                    state="success",
                    description="not a packet PR; nothing to grade",
                    github_token=github_token,
                )
                outcome["status_state"] = "success_not_packet"
            except Exception as exc:
                print(
                    f"[ingest-daemon] WARN: non-packet success status "
                    f"post failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            return outcome

        # Post pending commit status (signals "grading in flight").
        _post_pending(
            repo_full_name=event.repo_full_name,
            head_sha=event.head_sha,
            github_token=github_token,
        )
        pending_posted = True
        outcome["status_state"] = "pending"

        # Resolve parent code_revision_id
        parent_row = ledger_mod.find_by_commit_sha(cfg.db_path, event.base_sha)
        if parent_row:
            cr = parent_row.get("code_revision_id")
            code_revision_id = str(cr) if cr else None
        outcome["code_revision_id"] = code_revision_id

        # Codex review r5 (2026-05-15, I2 enforcement): when the daemon
        # hasn't ingested base.sha, `code_revision_id` resolves to None.
        # Running the runner with `code_revision_id=None` would make
        # atlas-query resolve "latest" instead of the pre-merge state,
        # defeating the apples-to-apples gate. Refuse to grade
        # unpinned; soft-pass per D-P2-5 (don't block merge on a
        # transient ingest lag) but make the situation explicit in the
        # status description + PR comment so operators know to re-fire
        # the gate after `make ingest-replay COMMIT=<base_sha>`.
        if code_revision_id is None:
            outcome["status"] = "revision_not_indexed"
            description = (
                f"(base SHA {event.base_sha[:7]} not indexed; "
                f"re-run via close/reopen after `make ingest-replay`)"
            )
            _post_final(
                repo_full_name=event.repo_full_name,
                head_sha=event.head_sha,
                state="success",
                description=description,
                github_token=github_token,
            )
            outcome["status_state"] = "success_revision_not_indexed"
            # Post a PR comment explaining the situation so reviewers
            # see WHY no per-receipt table appeared. Use the marker so
            # any subsequent (successful) run cleanly updates this
            # comment instead of stacking a duplicate.
            try:
                explainer_body = (
                    f"{pr_comment_mod.COMMENT_MARKER}\n"
                    f"## atlas-shadow grading\n\n"
                    f"**Packet(s):** {', '.join(qna_log_paths)}\n"
                    f"**Base SHA:** `{event.base_sha[:12]}`\n"
                    f"**Result:** _grading skipped — base SHA not yet "
                    f"ingested by atlas-shadow daemon_\n\n"
                    f"Run `make ingest-replay COMMIT={event.base_sha}` "
                    f"from atlas-shadow, then close+reopen this PR to "
                    f"re-trigger grading.\n"
                )
                _post_comment(
                    repo_full_name=event.repo_full_name,
                    pr_number=event.pr_number,
                    body=explainer_body,
                    github_token=github_token,
                )
            except Exception as exc:
                print(
                    f"[ingest-daemon] WARN: revision_not_indexed comment "
                    f"post failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            return outcome

        # Acquire pin (only when we have a real revision to pin to —
        # the None case above returned early).
        state_file_mod.acquire_pin(
            cfg.state_file,
            pr_number=event.pr_number,
            code_revision_id=code_revision_id,
        )
        pin_acquired = True

        # Per-packet grading
        for qna_log_path in qna_log_paths:
            body = _fetch_file_at_ref(
                repo_full_name=event.repo_full_name,
                path=qna_log_path,
                ref=event.head_sha,
                github_token=github_token,
            )
            if body is None:
                continue
            # Parse via a temp file (parser reads from disk)
            tmp = cfg.shadow_runs_dir / "_tmp" / f"pr-{event.pr_number}-{Path(qna_log_path).name}"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(body, encoding="utf-8")
            try:
                receipts, threshold_opt = parse_packet_receipts(tmp)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            threshold = threshold_opt if threshold_opt is not None else 50
            packet_id = Path(qna_log_path).parent.name

            rows: list[pr_comment_mod.ReceiptGradingRow] = []
            for receipt in receipts:
                try:
                    row = _grade_one(
                        cfg=cfg,
                        receipt=receipt,
                        code_revision_id=code_revision_id,
                        repo_full_name=event.repo_full_name,
                        # PR #15: the grading-anchor commit. For the
                        # PR-gate path this is the merge base; for
                        # grade-packet-batch it's --commit-sha
                        # (propagated through event.base_sha by the
                        # synthetic PrEvent in grade_batch).
                        run_commit=event.base_sha,
                        revision_pin_mode=revision_pin_mode,
                    )
                except Exception as exc:
                    rows.append(
                        pr_comment_mod.ReceiptGradingRow(
                            question_id=receipt.question_id,
                            question=receipt.question,
                            grade="no_match",
                            confidence=0.0,
                            rationale=f"grading_error: {type(exc).__name__}: {exc}",
                            tool="error",
                            warnings=[f"exception:{type(exc).__name__}"],
                            # PR #17: preserve the receipt's authoring
                            # intent on the grading-error row too —
                            # downstream classifiers may want to chart
                            # error rates by evidence_type.
                            evidence_type=receipt.evidence_type,
                        )
                    )
                    continue
                rows.append(row)

            summary = pr_comment_mod.GradingSummary(
                packet_id=packet_id,
                code_revision_id=code_revision_id,
                base_sha=event.base_sha,
                threshold_pct=threshold,
                rows=rows,
            )

            # Write per-packet artifact (one JSON per packet — filename
            # includes packet_id + a microsecond timestamp so multi-packet
            # PRs in the same second don't overwrite).
            artifact_path = write_grading_artifact(
                summary=summary,
                pr_number=event.pr_number,
                artifact_dir=cfg.shadow_runs_dir,
                base_sha=event.base_sha,
                head_sha=event.head_sha,
                repo_full_name=event.repo_full_name,
                now_iso=_now_iso,
            )

            outcome["summaries"].append({
                "packet_id": packet_id,
                "passed": summary.passed,
                # Raw score — denominator is total rows (legacy).
                "pass_pct": summary.pass_pct,
                "pass_count": summary.pass_count,
                "total": summary.total,
                # PR #14: clean-denominator score. Excludes rows whose
                # score_status != "counted" (today: receipt-stale skips
                # from PR #13's source_snapshot mismatch).
                "clean_pass_pct": summary.clean_pass_pct,
                "clean_total": summary.clean_total,
                "excluded_count": summary.excluded_count,
                "skipped_receipt_stale_count": summary.skipped_receipt_stale_count,
                # PR #15: distinct counter for run-commit line drift skips
                # (rows where the receipt-commit snapshot matched but the
                # run-commit snapshot mismatched).
                "skipped_run_commit_line_drift_count":
                    summary.skipped_run_commit_line_drift_count,
                # PR #17: four new non-retrieval skip counts. Each has
                # a distinct upstream fix so they're surfaced separately
                # rather than lumped.
                "skipped_non_repo_evidence_count":
                    summary.skipped_non_repo_evidence_count,
                "skipped_absence_search_count":
                    summary.skipped_absence_search_count,
                "skipped_unavailable_source_ref_count":
                    summary.skipped_unavailable_source_ref_count,
                "skipped_doc_corpus_excluded_count":
                    summary.skipped_doc_corpus_excluded_count,
                # PR #20: command-snapshot lane skip count.
                "skipped_command_snapshot_count":
                    summary.skipped_command_snapshot_count,
                "skipped_revision_not_indexed_count":
                    summary.skipped_revision_not_indexed_count,
                # Per-evidence-type breakdown (PR-evidence-breakdown).
                # Carried per-packet so the run aggregator can sum
                # across packets AND so per-packet drilldowns show
                # which evidence_type the receipts came from.
                "by_evidence_type": summary.by_evidence_type,
                # Per-lane breakdown — symmetric with by_evidence_type.
                # Surfaces which retrieval surface (doc_resolver /
                # explicit_source_fast_path / fuzzy_find_code /
                # scan_search / non_retrieval) the row was scored on.
                "by_lane": summary.by_lane,
                "artifact_path": str(artifact_path),
            })
            # Hold the live summary in a parallel list for the
            # single-comment build below.
            outcome.setdefault("_summary_objects", []).append(summary)

        # AFTER all packets are graded: render ONE PR comment from ALL
        # summaries. Codex review on impl PR (2026-05-15) caught that
        # posting one comment per packet (which we used to do here) would
        # have the marker-based update PATCH-overwrite each prior packet's
        # section — the final comment would contain only the last packet's
        # rows. Aggregating once fixes that.
        summary_objects = outcome.pop("_summary_objects", [])
        if summary_objects:
            body_md = pr_comment_mod.build_comment_markdown_for_summaries(
                summary_objects
            )
            _post_comment(
                repo_full_name=event.repo_full_name,
                pr_number=event.pr_number,
                body=body_md,
                github_token=github_token,
            )

        # Compute overall state across packets — all must pass.
        all_passed = all(s["passed"] for s in outcome["summaries"]) if outcome["summaries"] else True
        state = "success" if all_passed else "failure"
        description = _build_status_description(outcome, all_passed)
        _post_final(
            repo_full_name=event.repo_full_name,
            head_sha=event.head_sha,
            state=state,
            description=description,
            github_token=github_token,
        )
        outcome["status_state"] = state
        return outcome
    except Exception as exc:
        outcome["status"] = "error"
        outcome["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        # Soft-pass on operational errors (D-P2-5: don't block merge when
        # the grader itself is broken). Commit Status API has no
        # ``neutral`` equivalent; we map operational errors to ``success``
        # with a description that surfaces the problem to operators.
        if pending_posted:
            try:
                _post_final(
                    repo_full_name=event.repo_full_name,
                    head_sha=event.head_sha,
                    state="success",
                    description=(
                        f"(operational error: {type(exc).__name__}; see daemon log)"
                    ),
                    github_token=github_token,
                )
                outcome["status_state"] = "success_with_operational_error"
            except Exception as exc2:
                outcome["error"] += f"\n+ post_final_status: {exc2}"
        print(f"[ingest-daemon] run_pr_grading error: {outcome['error']}", file=sys.stderr)
        return outcome
    finally:
        if pin_acquired:
            try:
                state_file_mod.release_pin(cfg.state_file, pr_number=event.pr_number)
            except Exception as exc:
                print(
                    f"[ingest-daemon] release_pin failed for PR "
                    f"#{event.pr_number}: {exc}",
                    file=sys.stderr,
                )


def _build_status_description(outcome: dict[str, Any], all_passed: bool) -> str:
    """Render a 140-char-or-less commit-status description.

    GitHub truncates over-length descriptions in the API; we truncate
    here too so the rendering is deterministic. Format:

        "pass 12/12 (100%)"     # all green
        "fail 4/12 (33%)"       # some failures
        "no receipts in PR"     # empty-receipts edge case
    """
    summaries = outcome.get("summaries") or []
    if not summaries:
        return "no receipts in PR"
    total = sum(s["total"] for s in summaries)
    passed = sum(s["pass_count"] for s in summaries)
    pct = int(round(passed * 100 / total)) if total else 0
    badge = "pass" if all_passed else "fail"
    return f"{badge} {passed}/{total} ({pct}%)"


def handle_pr_event(cfg, event) -> None:
    """Receiver-facing entry: invoked by FastAPI BackgroundTasks.

    Resolves the GH token + delegates to :func:`run_pr_grading`. Soft-
    skips the entire run when the token is absent (logged to stderr so
    operators notice).
    """
    token = gh_check_mod.github_token_from_env()
    if not token:
        print(
            "[ingest-daemon] PR event received but GITHUB_ATLAS_SHADOW_TOKEN "
            "is unset; skipping grading. Set the token to enable the "
            "pre-merge grading gate.",
            file=sys.stderr,
        )
        return
    run_pr_grading(cfg, event, github_token=token)


# ---------------------------------------------------------------------------
# T9 — Durable artifact writer
# ---------------------------------------------------------------------------


def write_grading_artifact(
    *,
    summary: "pr_comment_mod.GradingSummary",
    pr_number: int,
    artifact_dir: Path,
    base_sha: str,
    head_sha: str,
    repo_full_name: str,
    now_iso: Optional[Callable[[], str]] = None,
) -> Path:
    """Serialize a :class:`GradingSummary` to
    ``shadow-runs/pr-<n>-<ts>-<packet_id>.json``.

    Returns the artifact path. Mirrors the schema the PR comment renders
    so post-merge analysis can replay either form.

    The filename includes ``packet_id`` and a microsecond-resolution
    timestamp so multi-packet PRs in the same second produce distinct
    artifacts (codex review on impl PR 2026-05-15 — the prior
    seconds-resolution-without-packet-id format collided on multi-packet
    runs).
    """
    if now_iso is None:
        now_iso = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # %f gives microseconds; strip to milliseconds for filename readability
    # while keeping enough resolution to avoid collisions in the same second.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    # Slugify the packet_id (already filesystem-safe in practice but
    # defend against weird inputs).
    packet_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", summary.packet_id or "unknown")
    out_path = artifact_dir / f"pr-{pr_number}-{ts}-{packet_slug}.json"
    payload = {
        "schema_version": "1.0",
        "produced_at": now_iso(),
        "pr_number": pr_number,
        "repo_full_name": repo_full_name,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "packet_id": summary.packet_id,
        "code_revision_id": summary.code_revision_id,
        "threshold_pct": summary.threshold_pct,
        "pass_count": summary.pass_count,
        "total": summary.total,
        "pass_pct": summary.pass_pct,
        "passed": summary.passed,
        "rows": [_serialize_row(row) for row in summary.rows],
    }
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


def _serialize_row(row: "pr_comment_mod.ReceiptGradingRow") -> dict[str, Any]:
    """Serialize one grading row to the per-packet JSON artifact.

    PR #14 adds ``lane`` / ``score_status`` / ``clean_excluded_reason``
    so downstream classifiers can apply the clean-denominator filter
    without re-inferring lane or stale-receipt status from rationale
    text.
    """
    return {
        "question_id": row.question_id,
        "question": row.question,
        "grade": row.grade,
        "confidence": float(row.confidence),
        "rationale": row.rationale,
        "tool": row.tool,
        "revision_binding": row.revision_binding,
        "artifact_id": row.artifact_id,
        "chunk_id": row.chunk_id,
        "heading_path": list(row.heading_path) if row.heading_path else None,
        "warnings": list(row.warnings or []),
        "atlas_answer_len": int(row.atlas_answer_len or 0),
        "atlas_returncode": row.atlas_returncode,
        "atlas_exception": row.atlas_exception,
        "atlas_stderr_head": row.atlas_stderr_head,
        # PR #16: raw retrieval diagnostics — atlas's plan dict, the
        # compact list of citation "path:lines" strings, and the
        # reranker summary signals. Doc_resolver rows carry these as
        # None / empty since they don't go through workspace_atlas_query.
        "atlas_retrieval_plan": row.atlas_retrieval_plan,
        "atlas_citation_locations": list(row.atlas_citation_locations or []),
        "atlas_citation_count": row.atlas_citation_count,
        "atlas_reranker_candidates_considered": row.atlas_reranker_candidates_considered,
        "atlas_reranker_top_k_count": row.atlas_reranker_top_k_count,
        "source_snapshot_status": row.source_snapshot_status,
        "source_snapshot_hash_match": row.source_snapshot_hash_match,
        "source_snapshot_sha256": row.source_snapshot_sha256,
        # PR #15: run-commit snapshot (None on non-code receipts or
        # when no run_commit was supplied to _grade_one_receipt).
        "run_snapshot_status": row.run_snapshot_status,
        "run_snapshot_hash_match": row.run_snapshot_hash_match,
        "run_snapshot_sha256": row.run_snapshot_sha256,
        # PR #14: lane + clean-denominator bookkeeping.
        "lane": row.lane,
        "score_status": row.score_status,
        "clean_excluded_reason": row.clean_excluded_reason,
        # Receipt-SHA pinning: actual Atlas revision/commit used for this row.
        "atlas_code_revision_id": row.atlas_code_revision_id,
        "atlas_commit_sha": row.atlas_commit_sha,
        # PR #17: receipt authoring intent (e.g. ``source_excerpt`` /
        # ``external_tool_docs`` / ``user_context`` / ``absence_search``).
        "evidence_type": row.evidence_type,
        # PR #20: command_snapshot lane diagnostics.
        "command_snapshot_status": row.command_snapshot_status,
        "command_snapshot_hash_match": row.command_snapshot_hash_match,
        "command_snapshot_sha256": row.command_snapshot_sha256,
        "command_snapshot_head": row.command_snapshot_head,
        "command_snapshot_exit_code": row.command_snapshot_exit_code,
    }
