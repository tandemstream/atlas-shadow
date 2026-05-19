"""code_snapshot — git-backed diagnostics for code-anchored receipts.

This module is deliberately diagnostic-only. It does not replace Atlas
retrieval output and it does not decide grades. It answers the control
question every shadow retrieval failure needs:

    Does the cited source span exist at the receipt's commit, and does its
    canonical sha256 match the receipt?

That separates "Atlas missed retrievable evidence" from "the packet cited a
source state that the local clone cannot materialize."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import doc_resolver as doc_resolver_mod


STATUS_MATCH = "git_source_hash_match"
STATUS_MISMATCH = "git_source_hash_mismatch"
STATUS_SOURCE_MISSING = "git_source_missing"
STATUS_NO_LINE_RANGE = "no_line_range"
STATUS_NOT_APPLICABLE = "not_applicable"


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


def resolve_code_receipt_snapshot(
    receipt,
    *,
    repo_path: Path,
    _subprocess_run: Callable = doc_resolver_mod.subprocess.run,
) -> CodeSnapshotResult:
    """Materialize the cited code span from git and compare sha256.

    Returns ``STATUS_NOT_APPLICABLE`` for receipts without source_path or
    source_commit, and ``STATUS_NO_LINE_RANGE`` for receipts whose source
    line range cannot be parsed. Both are expected for some non-source
    receipts and should not be treated as operational errors.
    """
    path = receipt.source_path
    commit = receipt.source_commit
    if not path or not commit:
        return CodeSnapshotResult(status=STATUS_NOT_APPLICABLE)

    line_range = doc_resolver_mod._parse_line_range(receipt.source_lines)
    if line_range is None:
        return CodeSnapshotResult(
            status=STATUS_NO_LINE_RANGE,
            path=path,
            commit=commit,
            source_lines=receipt.source_lines,
        )

    body = doc_resolver_mod._git_show_file(
        Path(repo_path),
        commit,
        path,
        _subprocess_run=_subprocess_run,
    )
    if body is None:
        return CodeSnapshotResult(
            status=STATUS_SOURCE_MISSING,
            path=path,
            commit=commit,
            source_lines=receipt.source_lines,
        )

    sliced = doc_resolver_mod._slice_lines(body, line_range)
    canonical = doc_resolver_mod._excerpt_canonical(sliced)
    resolved = doc_resolver_mod._sha256_of(canonical)
    expected = receipt.excerpt_sha256
    hash_match = bool(expected and resolved == expected)
    return CodeSnapshotResult(
        status=STATUS_MATCH if hash_match else STATUS_MISMATCH,
        path=path,
        commit=commit,
        source_lines=receipt.source_lines,
        resolved_sha256=resolved,
        expected_sha256=expected,
        hash_match=hash_match,
        raw_text_len=len(canonical),
        raw_text_head=canonical[:500],
    )
