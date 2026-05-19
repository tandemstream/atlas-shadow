"""code_snapshot — git-backed diagnostics for code-anchored receipts.

This module is deliberately diagnostic-only. It does not replace Atlas
retrieval output and it does not decide grades. It answers two control
questions every shadow retrieval failure needs to distinguish:

    1. Does the cited source span exist at the receipt's pinned commit,
       and does its canonical sha256 match what the receipt claims?
       (``resolve_code_receipt_snapshot``)

    2. Does the same path/line-range still render the same canonical
       sha256 at the **grading run commit**? (PR #15:
       ``resolve_code_receipt_run_snapshot``)

(1) separates "Atlas missed retrievable evidence" from "the packet cited
a source state that the local clone cannot materialize." (2) separates
"Atlas missed retrievable evidence at the grading commit" from
"the file was edited between receipt commit and grading commit — those
line numbers now point at different code." Without (2), a
``no_match + git_source_hash_match`` row collapses two distinct failure
modes the daemon cannot tell apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import doc_resolver as doc_resolver_mod


# Receipt-commit-pinned snapshot statuses (existing).
STATUS_MATCH = "git_source_hash_match"
STATUS_MISMATCH = "git_source_hash_mismatch"
STATUS_SOURCE_MISSING = "git_source_missing"
STATUS_NO_LINE_RANGE = "no_line_range"
STATUS_NOT_APPLICABLE = "not_applicable"

# Run-commit snapshot statuses (PR #15). Parallel to the receipt-commit
# constants above but namespaced so a row consumer can tell which
# snapshot a status came from without inspecting the field name.
STATUS_RUN_COMMIT_MATCH = "run_commit_hash_match"
STATUS_RUN_COMMIT_MISMATCH = "run_commit_hash_mismatch"
STATUS_RUN_COMMIT_SOURCE_MISSING = "run_commit_source_missing"
# ``no_line_range`` and ``not_applicable`` are commit-agnostic preconditions;
# we reuse the existing constants when the run-commit check skips for
# the same reasons.


@dataclass(frozen=True)
class CodeSnapshotResult:
    status: str
    path: Optional[str] = None
    commit: Optional[str] = None
    source_lines: Optional[str] = None
    resolved_sha256: Optional[str] = None
    expected_sha256: Optional[str] = None
    hash_match: Optional[bool] = None
    raw_text_len: int = 0
    raw_text_head: str = ""
    warnings: list[str] = field(default_factory=list)


def _resolve_at_commit(
    *,
    path: Optional[str],
    commit: Optional[str],
    source_lines: Optional[str],
    expected_sha256: Optional[str],
    repo_path: Path,
    match_status: str,
    mismatch_status: str,
    source_missing_status: str,
    _subprocess_run: Callable,
) -> CodeSnapshotResult:
    """Shared snapshot-at-commit machinery.

    Slices ``<commit>:<path>`` at ``source_lines``, computes the
    canonical sha256, and compares to ``expected_sha256``. Status
    constants are parameterized so the receipt-commit and run-commit
    resolvers can return distinct status namespaces from the same logic
    path.
    """
    if not path or not commit:
        return CodeSnapshotResult(status=STATUS_NOT_APPLICABLE)

    line_range = doc_resolver_mod._parse_line_range(source_lines)
    if line_range is None:
        return CodeSnapshotResult(
            status=STATUS_NO_LINE_RANGE,
            path=path,
            commit=commit,
            source_lines=source_lines,
        )

    body = doc_resolver_mod._git_show_file(
        Path(repo_path),
        commit,
        path,
        _subprocess_run=_subprocess_run,
    )
    if body is None:
        return CodeSnapshotResult(
            status=source_missing_status,
            path=path,
            commit=commit,
            source_lines=source_lines,
        )

    sliced = doc_resolver_mod._slice_lines(body, line_range)
    canonical = doc_resolver_mod._excerpt_canonical(sliced)
    resolved = doc_resolver_mod._sha256_of(canonical)
    hash_match = bool(expected_sha256 and resolved == expected_sha256)
    return CodeSnapshotResult(
        status=match_status if hash_match else mismatch_status,
        path=path,
        commit=commit,
        source_lines=source_lines,
        resolved_sha256=resolved,
        expected_sha256=expected_sha256,
        hash_match=hash_match,
        raw_text_len=len(canonical),
        raw_text_head=canonical[:500],
    )


def resolve_code_receipt_snapshot(
    receipt,
    *,
    repo_path: Path,
    _subprocess_run: Callable = doc_resolver_mod.subprocess.run,
) -> CodeSnapshotResult:
    """Materialize the cited code span at the receipt's pinned commit
    and compare its sha256 to ``receipt.excerpt_sha256``.

    Returns ``STATUS_NOT_APPLICABLE`` for receipts without source_path or
    source_commit, and ``STATUS_NO_LINE_RANGE`` for receipts whose source
    line range cannot be parsed. Both are expected for some non-source
    receipts and should not be treated as operational errors.
    """
    return _resolve_at_commit(
        path=receipt.source_path,
        commit=receipt.source_commit,
        source_lines=receipt.source_lines,
        expected_sha256=receipt.excerpt_sha256,
        repo_path=repo_path,
        match_status=STATUS_MATCH,
        mismatch_status=STATUS_MISMATCH,
        source_missing_status=STATUS_SOURCE_MISSING,
        _subprocess_run=_subprocess_run,
    )


def resolve_code_receipt_run_snapshot(
    receipt,
    *,
    repo_path: Path,
    run_commit: Optional[str],
    _subprocess_run: Callable = doc_resolver_mod.subprocess.run,
) -> CodeSnapshotResult:
    """PR #15: parallel snapshot at the **grading run commit**.

    Where :func:`resolve_code_receipt_snapshot` answers "is the receipt
    self-consistent at authoring time?", this answers "do the same
    path/lines still render the same canonical sha256 at the run commit
    we're grading against?" A mismatch here when the receipt-commit
    snapshot matched is the signal for *run_commit_line_drift* — the
    file was edited between receipt commit and run commit so the cited
    line numbers now point at different code.

    Returns ``STATUS_NOT_APPLICABLE`` when no run_commit was supplied
    (typically the case for callers that don't know the grading commit,
    or for receipts that don't carry path+lines anchors).
    """
    if not run_commit:
        return CodeSnapshotResult(status=STATUS_NOT_APPLICABLE)
    return _resolve_at_commit(
        path=receipt.source_path,
        commit=run_commit,
        source_lines=receipt.source_lines,
        expected_sha256=receipt.excerpt_sha256,
        repo_path=repo_path,
        match_status=STATUS_RUN_COMMIT_MATCH,
        mismatch_status=STATUS_RUN_COMMIT_MISMATCH,
        source_missing_status=STATUS_RUN_COMMIT_SOURCE_MISSING,
        _subprocess_run=_subprocess_run,
    )
