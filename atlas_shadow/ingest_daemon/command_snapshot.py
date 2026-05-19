"""command_snapshot — deterministic source-check lane for receipts whose
``command_text`` is a safe, local source-inspection command (PR #20,
``atlas-shadow-command-snapshot-lane-v1``).

Receipts whose evidence is a *command output* (``ls``, ``grep``,
``sed -n <start>,<end>p``, ``find``, ``wc -l``) aren't actually testing
atlas retrieval — they're testing whether a deterministic local check
matches the receipt's claim. Routing them through find_code /
scan_search produces false negatives. This module executes the
equivalent git-backed operation against the receipt's pinned
``source_commit`` and reports back so :func:`grader_service.\
_grade_one_receipt` can drop the row from the clean denominator
without spending an atlas query.

**Safety:** The parser is a strict whitelist. We never shell out the
raw ``command_text``. Each supported shape is translated into a
``git`` operation against the local clone with a hard subprocess
timeout. Unsupported shapes return ``STATUS_UNSUPPORTED`` and the
caller falls through to the normal grading path.

**Supported command shapes (per Codex's PR #20 brief):**

  - ``scripts/qa_lookup.sh show-range <commit>:<path> <start> <end>``
  - ``scripts/qa_lookup.sh sed-range <path> <start> <end>``
  - ``scripts/qa_lookup.sh grep <pattern> <path...>``
  - ``scripts/qa_lookup.sh rg <pattern> <path...>``
  - ``ls <path>``
  - ``find <path> ...``
  - ``wc -l <path>``

**Synthesized commands** — when the receipt has no ``command_text`` but
carries a ``source_path``, the module synthesizes either:

  - ``sed-range`` when ``source_lines`` is present (equivalent to
    ``code_snapshot.resolve_code_receipt_snapshot`` but consumed
    through the command-snapshot lane so the row drops out of the
    clean denominator as ``skipped_command_snapshot``).
  - ``ls`` when only ``source_path`` is present (path-only receipts
    like ``lookup-subagent/q10`` Makefile or ``q12`` scripts/
    directory — Codex's brief explicitly calls these out as
    command/path-snapshot candidates).

**Outputs:** A :class:`CommandSnapshotResult` with the raw output's
canonical sha256 (compared against ``receipt.excerpt_sha256`` when
present), an output-head sample capped at 2,000 chars, exit code, and
one of the documented status values.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import doc_resolver as doc_resolver_mod


# ─── Status constants ─────────────────────────────────────────────────

# The command output matched the receipt's excerpt_sha256 — receipt is
# internally consistent with what's in source control.
STATUS_MATCH = "command_snapshot_match"

# Command output exists and is non-empty, but doesn't match the receipt's
# excerpt. Caller emits skipped_unavailable_source_ref or
# skipped_receipt_stale (drift between authoring + grading).
STATUS_MISMATCH = "command_snapshot_mismatch"

# Receipt's evidence_type is absence_search AND the command (e.g. grep,
# find) returned empty — claim of absence verified.
STATUS_NO_MATCH_EXPECTED_ABSENT = "command_snapshot_no_match_expected_absent"

# Receipt's evidence_type is absence_search but the command returned
# matches — claim of absence contradicted. Caller emits
# skipped_unavailable_source_ref (the receipt is contradicted by current
# repo state, so atlas wasn't being tested fairly).
STATUS_FOUND_BUT_EXPECTED_ABSENT = "command_snapshot_found_but_expected_absent"

# The command's source path can't be materialized at the pinned commit
# (commit not in repo / path not at that commit).
STATUS_SOURCE_MISSING = "command_snapshot_source_missing"

# Command text wasn't a whitelisted shape, OR there was no command at
# all and the receipt has no synthesize-able anchors. Caller falls
# through to the normal grading path.
STATUS_UNSUPPORTED = "command_snapshot_unsupported"

# A whitelisted shape was parsed but the subprocess raised
# (timeout, OS error, etc.). Caller treats this as a soft failure —
# falls through so the row still gets graded normally.
STATUS_ERROR = "command_snapshot_error"


_OUTPUT_HEAD_CHARS = 2_000
_SUBPROCESS_TIMEOUT_SECONDS = 30

# ``scripts/qa_lookup.sh`` is the canonical wrapper used by core packet
# authors. The leading prefix is optional in our whitelist so users
# typing the raw ``sed-range`` form also work.
_QA_LOOKUP_PREFIX = "scripts/qa_lookup.sh"


# ─── Result dataclass ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandSnapshotResult:
    status: str
    hash_match: Optional[bool] = None
    resolved_sha256: Optional[str] = None
    output_head: str = ""
    exit_code: Optional[int] = None
    # The parsed-out command shape (op + args). Useful for diagnostics
    # and tests.
    parsed: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ─── Parsing ──────────────────────────────────────────────────────────


def parse_command_text(command_text: Optional[str]) -> Optional[dict[str, Any]]:
    """Whitelist-parse a receipt's ``command_text`` into a structured
    op + args dict, or ``None`` if the shape isn't recognized.

    The parser is strict — it accepts a small set of patterns and
    rejects anything with shell metacharacters (pipes, redirects, ``&``,
    ``;``, ``$(...)``). Compound commands like ``ls foo && find bar``
    fail to parse and the caller falls back to ``STATUS_UNSUPPORTED``.
    """
    if not command_text:
        return None
    raw = command_text.strip()
    if not raw:
        return None

    # Reject anything with shell control characters — keeps execution
    # paths deterministic and avoids accidental command-injection.
    # Backticks are common in markdown wrapping; the receipt parser
    # should already have stripped those, but defense-in-depth.
    if any(ch in raw for ch in ("&", ";", "|", ">", "<", "`", "$")):
        return None

    # Tokenize. shlex.split handles quoted patterns like
    # ``grep "heading_path" core tests`` correctly.
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return None
    if not tokens:
        return None

    # Strip a leading ``scripts/qa_lookup.sh`` wrapper to reach the
    # actual op.
    if tokens[0] == _QA_LOOKUP_PREFIX and len(tokens) >= 2:
        tokens = tokens[1:]

    op = tokens[0]
    args = tokens[1:]

    if op == "show-range":
        # show-range <commit>:<path> <start> <end>
        if len(args) != 3:
            return None
        m = re.match(r"^([0-9a-f]{7,40}):(.+)$", args[0])
        if not m:
            return None
        commit, path = m.group(1), m.group(2)
        try:
            start, end = int(args[1]), int(args[2])
        except ValueError:
            return None
        return {
            "op": "show-range",
            "commit": commit,
            "path": path,
            "start": start,
            "end": end,
        }

    if op == "sed-range":
        # sed-range <path> <start> <end>
        if len(args) != 3:
            return None
        try:
            start, end = int(args[1]), int(args[2])
        except ValueError:
            return None
        return {
            "op": "sed-range",
            "path": args[0],
            "start": start,
            "end": end,
        }

    if op in ("grep", "rg"):
        # grep <pattern> <path...>
        if len(args) < 2:
            return None
        return {
            "op": "grep",
            "pattern": args[0],
            "paths": list(args[1:]),
        }

    if op == "ls":
        # ls <path>
        # Don't accept arbitrary flags (e.g. ``-la`` from q13's
        # compound command) — keeps semantics deterministic.
        if len(args) != 1 or args[0].startswith("-"):
            return None
        return {"op": "ls", "path": args[0]}

    if op == "find":
        # find <path>  — flags NOT accepted in v1.
        #
        # Earlier drafts whitelisted ``-type``, ``-name``, etc., but
        # ``_handle_find`` delegates to ``_handle_ls`` and ignores
        # filters. With filters accepted but not applied, an absence
        # receipt for ``find docs -type d -name agents`` could be
        # contradicted by *any* unrelated file under ``docs/`` — a
        # false ``found_but_expected_absent`` exclusion from the
        # clean denominator. v2 can implement real filter semantics;
        # for now reject filtered forms so they fall through to the
        # normal grading path.
        if len(args) != 1 or args[0].startswith("-"):
            return None
        return {"op": "find", "path": args[0], "extra": []}

    if op == "wc":
        # wc -l <path>
        if len(args) != 2 or args[0] != "-l":
            return None
        return {"op": "wc-l", "path": args[1]}

    return None


def synthesize_command(receipt) -> Optional[dict[str, Any]]:
    """Synthesize a command shape from a directory-listing receipt
    that has no ``command_text``.

    Scope is intentionally narrow — only **trailing-slash directory
    paths** get synthesized into ``ls <path>``. The signal: a receipt
    whose ``source_path`` ends with ``/`` is unambiguously asking
    "what's in this directory?" (Codex's ``lookup-subagent/q12``
    scripts/ target).

    Why not also synthesize for path-only file receipts (``q10``
    Makefile, ``q1`` persona-shared.md, etc.)? They look like
    candidates — their ``oracle_excerpt`` is usually ``ls -la``
    shell output — but the LLM grader has historically resolved
    them correctly via atlas's doc_resolver / find_code paths.
    Pre-empting them with command_snapshot would eat real atlas
    measurements without adding determinism (the LLM grader's
    judgment is what actually drove the grade). Authors who
    genuinely want command verification can put an explicit
    ``ls``, ``sed-range``, etc. in ``command_text`` — the
    whitelist parser handles that and the explicit-intent signal
    is unambiguous.

    Returns ``None`` for receipts that don't qualify (no
    source_path, source_path without trailing slash, OR
    source_path with source_lines).
    """
    source_path = (getattr(receipt, "source_path", None) or "").strip()
    if not source_path:
        return None
    # Path+lines stays on the atlas path.
    source_lines = (getattr(receipt, "source_lines", None) or "").strip()
    if source_lines:
        return None
    # Only directory listings (trailing slash) get synthesized. File
    # paths without explicit command_text route through atlas.
    if not source_path.endswith("/"):
        return None
    return {"op": "ls", "path": source_path, "synthesized": True}


# ─── Handlers (git-backed) ────────────────────────────────────────────


def _canonical(text: str) -> str:
    """Match doc_resolver / code_snapshot's canonicalization: collapse
    trailing newline. Keeps sha256 stable across CRLF / final-newline
    variations.
    """
    return doc_resolver_mod._excerpt_canonical(text)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _head(text: str) -> str:
    return text[:_OUTPUT_HEAD_CHARS]


def _git_show(
    repo_path: Path,
    commit: str,
    path: str,
    *,
    _subprocess_run: Callable,
) -> tuple[Optional[str], int]:
    """``git show <commit>:<path>`` → (body_text, exit_code).
    Returns (None, exit_code) when the path can't be materialized.
    """
    if not repo_path.exists():
        return None, 128  # git's "no such repo" exit shape
    try:
        proc = _subprocess_run(
            ["git", "-C", str(repo_path), "show", f"{commit}:{path}"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, 124  # conventional "timeout" exit code
    if proc.returncode != 0:
        return None, proc.returncode
    return proc.stdout, 0


def _git_ls_tree(
    repo_path: Path,
    commit: str,
    path: str,
    *,
    _subprocess_run: Callable,
) -> tuple[Optional[str], int]:
    """``git ls-tree -r --name-only <commit> -- <path>`` for directory
    listings + ``git cat-file -e <commit>:<path>`` for existence checks.

    For ``ls <path>``: we use ``ls-tree`` to enumerate all entries under
    that path. Single-file paths return a single-line listing; directory
    paths return all descendants. Empty result = path doesn't exist at
    this commit.
    """
    if not repo_path.exists():
        return None, 128
    try:
        proc = _subprocess_run(
            ["git", "-C", str(repo_path), "ls-tree", "-r", "--name-only",
             commit, "--", path],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, 124
    if proc.returncode != 0:
        return None, proc.returncode
    return proc.stdout, 0


def _git_grep(
    repo_path: Path,
    commit: str,
    pattern: str,
    paths: list[str],
    *,
    _subprocess_run: Callable,
) -> tuple[Optional[str], int]:
    """``git grep`` against the receipt's pinned commit, restricted to
    the supplied paths. Returns (output_text, exit_code). git grep
    returns exit_code=1 when there are no matches — we still surface
    the (empty) output so the caller can treat "no matches" as a real
    signal for absence_search receipts.
    """
    if not repo_path.exists():
        return None, 128
    try:
        proc = _subprocess_run(
            ["git", "-C", str(repo_path), "grep",
             "--fixed-strings",  # treat pattern literally — predictable
             "-n", pattern, commit, "--", *paths],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, 124
    # Exit 0 = matches found, 1 = no matches, 2+ = error. We surface
    # both 0 and 1 to the caller; only 2+ becomes "error."
    if proc.returncode >= 2:
        return None, proc.returncode
    return proc.stdout, proc.returncode


# ─── Op-level handlers ────────────────────────────────────────────────


def _handle_show_range(
    receipt,
    parsed: dict,
    *,
    repo_path: Path,
    _subprocess_run: Callable,
) -> CommandSnapshotResult:
    """``show-range`` carries its own commit in the command. Use that
    commit (not receipt.source_commit) so the receipt-author's intent
    is honored even if it differs from source_commit (rare but valid).
    """
    commit = parsed["commit"]
    body, exit_code = _git_show(repo_path, commit, parsed["path"],
                                _subprocess_run=_subprocess_run)
    if body is None:
        return CommandSnapshotResult(
            status=STATUS_SOURCE_MISSING,
            exit_code=exit_code,
            parsed=parsed,
        )
    return _hash_compare_lines(body, parsed["start"], parsed["end"],
                               receipt, parsed, exit_code=exit_code)


def _handle_sed_range(
    receipt,
    parsed: dict,
    *,
    repo_path: Path,
    _subprocess_run: Callable,
) -> CommandSnapshotResult:
    """``sed-range`` resolves against ``receipt.source_commit``."""
    commit = getattr(receipt, "source_commit", None)
    if not commit:
        return CommandSnapshotResult(
            status=STATUS_UNSUPPORTED,
            parsed=parsed,
        )
    body, exit_code = _git_show(repo_path, commit, parsed["path"],
                                _subprocess_run=_subprocess_run)
    if body is None:
        return CommandSnapshotResult(
            status=STATUS_SOURCE_MISSING,
            exit_code=exit_code,
            parsed=parsed,
        )
    return _hash_compare_lines(body, parsed["start"], parsed["end"],
                               receipt, parsed, exit_code=exit_code)


def _hash_compare_lines(
    body: str,
    start: int,
    end: int,
    receipt,
    parsed: dict,
    *,
    exit_code: int,
) -> CommandSnapshotResult:
    """Slice [start, end] inclusive, canonicalize, hash, compare to
    receipt's excerpt_sha256."""
    sliced = doc_resolver_mod._slice_lines(body, (start, end))
    canon = _canonical(sliced)
    resolved = _sha256(canon)
    expected = getattr(receipt, "excerpt_sha256", None)
    hash_match = bool(expected and resolved == expected)
    if expected and hash_match:
        return CommandSnapshotResult(
            status=STATUS_MATCH,
            hash_match=True,
            resolved_sha256=resolved,
            output_head=_head(canon),
            exit_code=exit_code,
            parsed=parsed,
        )
    if expected and not hash_match:
        return CommandSnapshotResult(
            status=STATUS_MISMATCH,
            hash_match=False,
            resolved_sha256=resolved,
            output_head=_head(canon),
            exit_code=exit_code,
            parsed=parsed,
        )
    # No expected hash — treat the materialized content as a pass-with-
    # caveat. The command did produce verifiable output; receipt didn't
    # pin an excerpt to compare against.
    return CommandSnapshotResult(
        status=STATUS_MATCH,
        hash_match=None,  # no comparison was possible
        resolved_sha256=resolved,
        output_head=_head(canon),
        exit_code=exit_code,
        parsed=parsed,
    )


def _handle_ls(
    receipt,
    parsed: dict,
    *,
    repo_path: Path,
    _subprocess_run: Callable,
) -> CommandSnapshotResult:
    """``ls <path>`` against receipt.source_commit. Output = newline-
    separated descendant paths. Empty result is meaningful — caller can
    interpret as path-absent for absence_search receipts.
    """
    commit = getattr(receipt, "source_commit", None)
    if not commit:
        return CommandSnapshotResult(status=STATUS_UNSUPPORTED, parsed=parsed)
    out, exit_code = _git_ls_tree(repo_path, commit, parsed["path"],
                                  _subprocess_run=_subprocess_run)
    if out is None:
        return CommandSnapshotResult(
            status=STATUS_SOURCE_MISSING,
            exit_code=exit_code,
            parsed=parsed,
        )
    canon = _canonical(out)
    resolved = _sha256(canon)
    return _classify_listing_output(canon, resolved, receipt, parsed,
                                    exit_code=exit_code)


def _handle_find(
    receipt,
    parsed: dict,
    *,
    repo_path: Path,
    _subprocess_run: Callable,
) -> CommandSnapshotResult:
    """``find <path>`` (no flags — see parser) is treated the same as
    ``ls <path>``: both enumerate entries under the path at the
    receipt's commit. Filtered forms are rejected at parse time so
    we don't silently ignore ``-type``/``-name`` and produce wrong
    absence-search verdicts.
    """
    return _handle_ls(receipt, parsed, repo_path=repo_path,
                      _subprocess_run=_subprocess_run)


def _handle_grep(
    receipt,
    parsed: dict,
    *,
    repo_path: Path,
    _subprocess_run: Callable,
) -> CommandSnapshotResult:
    """``grep <pattern> <path...>`` against receipt.source_commit.
    Empty output = absence (claim verified for absence_search).
    Non-empty output = pattern found.
    """
    commit = getattr(receipt, "source_commit", None)
    if not commit:
        return CommandSnapshotResult(status=STATUS_UNSUPPORTED, parsed=parsed)
    out, exit_code = _git_grep(repo_path, commit, parsed["pattern"],
                               parsed["paths"], _subprocess_run=_subprocess_run)
    if out is None:
        return CommandSnapshotResult(
            status=STATUS_ERROR,
            exit_code=exit_code,
            parsed=parsed,
        )
    canon = _canonical(out)
    resolved = _sha256(canon)
    return _classify_listing_output(canon, resolved, receipt, parsed,
                                    exit_code=exit_code)


def _handle_wc_l(
    receipt,
    parsed: dict,
    *,
    repo_path: Path,
    _subprocess_run: Callable,
) -> CommandSnapshotResult:
    """``wc -l <path>`` — get file body, count lines, format as the
    classic ``<count> <path>`` shape. Hashed + compared to
    receipt.excerpt_sha256 if present.
    """
    commit = getattr(receipt, "source_commit", None)
    if not commit:
        return CommandSnapshotResult(status=STATUS_UNSUPPORTED, parsed=parsed)
    body, exit_code = _git_show(repo_path, commit, parsed["path"],
                                _subprocess_run=_subprocess_run)
    if body is None:
        return CommandSnapshotResult(
            status=STATUS_SOURCE_MISSING,
            exit_code=exit_code,
            parsed=parsed,
        )
    # Count newlines — matches wc -l's "line count = newline count"
    # semantics (no trailing-newline-adds-line behavior).
    count = body.count("\n")
    output = f"{count} {parsed['path']}"
    canon = _canonical(output)
    resolved = _sha256(canon)
    expected = getattr(receipt, "excerpt_sha256", None)
    if expected:
        hash_match = resolved == expected
        return CommandSnapshotResult(
            status=STATUS_MATCH if hash_match else STATUS_MISMATCH,
            hash_match=hash_match,
            resolved_sha256=resolved,
            output_head=_head(canon),
            exit_code=exit_code,
            parsed=parsed,
        )
    return CommandSnapshotResult(
        status=STATUS_MATCH,
        hash_match=None,
        resolved_sha256=resolved,
        output_head=_head(canon),
        exit_code=exit_code,
        parsed=parsed,
    )


def _classify_listing_output(
    canon: str,
    resolved: str,
    receipt,
    parsed: dict,
    *,
    exit_code: int,
) -> CommandSnapshotResult:
    """Common classification logic for ls / find / grep — empty output
    is meaningful for absence_search receipts.
    """
    evidence_type = (getattr(receipt, "evidence_type", "") or "").strip()
    is_absence = evidence_type == "absence_search"
    is_empty = not canon.strip()

    if is_absence:
        if is_empty:
            return CommandSnapshotResult(
                status=STATUS_NO_MATCH_EXPECTED_ABSENT,
                hash_match=None,
                resolved_sha256=resolved,
                output_head=_head(canon),
                exit_code=exit_code,
                parsed=parsed,
            )
        return CommandSnapshotResult(
            status=STATUS_FOUND_BUT_EXPECTED_ABSENT,
            hash_match=None,
            resolved_sha256=resolved,
            output_head=_head(canon),
            exit_code=exit_code,
            parsed=parsed,
        )

    # Non-absence_search receipts: compare to receipt.excerpt_sha256
    # when present, otherwise treat any non-empty output as match.
    expected = getattr(receipt, "excerpt_sha256", None)
    if expected:
        hash_match = resolved == expected
        return CommandSnapshotResult(
            status=STATUS_MATCH if hash_match else STATUS_MISMATCH,
            hash_match=hash_match,
            resolved_sha256=resolved,
            output_head=_head(canon),
            exit_code=exit_code,
            parsed=parsed,
        )
    if is_empty:
        # Receipt has no excerpt_sha256 to compare AND the command
        # produced no output. For path-snapshot receipts (q10/q12
        # shape), this means the path doesn't exist at the commit —
        # source_missing is more accurate than mismatch.
        return CommandSnapshotResult(
            status=STATUS_SOURCE_MISSING,
            hash_match=None,
            resolved_sha256=resolved,
            output_head=_head(canon),
            exit_code=exit_code,
            parsed=parsed,
        )
    return CommandSnapshotResult(
        status=STATUS_MATCH,
        hash_match=None,
        resolved_sha256=resolved,
        output_head=_head(canon),
        exit_code=exit_code,
        parsed=parsed,
    )


# ─── Public entry point ───────────────────────────────────────────────


_HANDLERS = {
    "show-range": _handle_show_range,
    "sed-range": _handle_sed_range,
    "grep": _handle_grep,
    "ls": _handle_ls,
    "find": _handle_find,
    "wc-l": _handle_wc_l,
}


def resolve_command_snapshot(
    receipt,
    *,
    repo_path: Path,
    _subprocess_run: Callable = subprocess.run,
) -> CommandSnapshotResult:
    """Resolve the receipt's command_text (or a synthesized command) as
    a deterministic source check. Public entry point.

    Returns ``CommandSnapshotResult`` with one of the documented status
    values. ``STATUS_UNSUPPORTED`` is the signal the caller uses to
    fall through to the normal grading path.
    """
    command_text = getattr(receipt, "command_text", None)
    parsed = parse_command_text(command_text)
    if parsed is None:
        # Try synthesizing from receipt's source_path/source_lines.
        parsed = synthesize_command(receipt)
    if parsed is None:
        return CommandSnapshotResult(status=STATUS_UNSUPPORTED)

    handler = _HANDLERS.get(parsed["op"])
    if handler is None:
        return CommandSnapshotResult(
            status=STATUS_UNSUPPORTED, parsed=parsed
        )
    try:
        return handler(
            receipt, parsed,
            repo_path=Path(repo_path),
            _subprocess_run=_subprocess_run,
        )
    except Exception as exc:  # noqa: BLE001
        return CommandSnapshotResult(
            status=STATUS_ERROR,
            parsed=parsed,
            warnings=[f"{type(exc).__name__}: {exc}"],
        )
