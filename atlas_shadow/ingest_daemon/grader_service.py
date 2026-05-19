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
    _runner_run_one: Optional[Callable] = None,
    _doc_resolver: Callable = doc_resolver_mod.resolve_doc_receipt,
    _grader_grade: Optional[Callable] = None,
) -> "pr_comment_mod.ReceiptGradingRow":
    """Translate, dispatch to runner/doc_resolver, and grade one receipt.

    Injection seams (``_runner_run_one``, ``_doc_resolver``, ``_grader_grade``)
    keep this testable without standing up Atlas / a real DB / the
    Anthropic SDK.
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
    source_snapshot = None
    tool_label = ""

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
            code_revision_id=code_revision_id,
        )
        atlas_response = shadow_response.atlas_response
        atlas_answer_text = atlas_response.answer_text or ""
        atlas_returncode = atlas_response.returncode
        atlas_exception = atlas_response.exception
        atlas_stderr_head = (atlas_response.stderr or "")[:1000] or None
        source_snapshot = code_snapshot_mod.resolve_code_receipt_snapshot(
            receipt,
            repo_path=cfg.core_repo_path,
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
            source_snapshot_status=(
                source_snapshot.status if source_snapshot is not None else None
            ),
            source_snapshot_hash_match=(
                source_snapshot.hash_match if source_snapshot is not None else None
            ),
            source_snapshot_sha256=(
                source_snapshot.resolved_sha256 if source_snapshot is not None else None
            ),
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
        source_snapshot_status=(
            source_snapshot.status if source_snapshot is not None else None
        ),
        source_snapshot_hash_match=(
            source_snapshot.hash_match if source_snapshot is not None else None
        ),
        source_snapshot_sha256=(
            source_snapshot.resolved_sha256 if source_snapshot is not None else None
        ),
    )


def run_pr_grading(
    cfg,
    event,
    *,
    github_token: str,
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
                "pass_pct": summary.pass_pct,
                "pass_count": summary.pass_count,
                "total": summary.total,
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
        "source_snapshot_status": row.source_snapshot_status,
        "source_snapshot_hash_match": row.source_snapshot_hash_match,
        "source_snapshot_sha256": row.source_snapshot_sha256,
    }
