"""grader_service — PR grading orchestrator.

This module is the entry point for atlas-shadow's pre-merge grading gate
(packet 2026-05-14-atlas-shadow-pre-merge-grading-gate-v1). It coordinates
across receiver (T1), packet-tag detection (T2), receipt parsing (T3),
receipt -> Atlas-query translation (T4), doc-anchored resolution (T4a, in
``doc_resolver.py``), the existing offline ``grader.grade`` rubric, revision
pinning (T6), GitHub Checks API (T7), the PR comment generator (T8), and
the durable artifact writer (T9).

Tasks colocated here:

  * T2 — :func:`detect_packet_qna_log` (PR file-presence check).
  * T3 — :func:`parse_packet_receipts` (canonical bullet-list receipt
    parser; also extracts the optional ``grading_threshold_pct:`` header).
  * T4 — :func:`translate_receipt_to_query` (CODE-anchored heuristic +
    ``query_hint:`` override + doc-extension routing to T4a).
  * T5 — :func:`run_pr_grading` / :func:`handle_pr_event` (the orchestrator).
  * T6 — pin lifecycle helpers (:func:`acquire_pin` / :func:`release_pin`).

T4a (doc resolver) lives in a separate module to keep its direct psycopg
dependency contained.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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

# The threshold header — accept either `**Grading threshold pct:** N` or
# a raw `grading_threshold_pct: N` line. Anywhere before the first `## `
# section heading (preamble convention; not a frontmatter requirement).
_THRESHOLD_HEADER_RE = re.compile(
    r"^\s*(?:\*\*)?grading[ _-]threshold[ _-]pct(?:\*\*)?\s*[:=]\s*(?P<value>\d{1,3})\s*$",
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
        m = _THRESHOLD_HEADER_RE.match(raw)
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
    return CodeQuery(tool=tool, question=receipt.question, receipt=receipt)
