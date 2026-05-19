"""pr_comment — PR comment generator + GitHub Issues-API integration (T8).

The grader posts (or updates) a single comment per PR with a per-receipt
Markdown table. The comment carries a marker
(``<!-- atlas-shadow-grading -->``) so subsequent runs find and update the
prior comment rather than appending a new one each time.

Two responsibilities:

  1. :func:`build_comment_markdown` — pure function turning a grading
     summary into Markdown text. Doc receipts gain a ``revision_binding``
     column showing how T4a resolved the citation
     (``db_commit_scoped`` / ``git_receipt_snapshot`` /
     ``unresolved_source_ref``).
  2. :func:`post_or_update_pr_comment` — find-or-create against the GitHub
     Issues API. Reuses the ``http_request`` transport + auth from
     :mod:`gh_check`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import gh_check as gh_check_mod


COMMENT_MARKER = "<!-- atlas-shadow-grading -->"


@dataclass(frozen=True)
class ReceiptGradingRow:
    """One row in the PR comment table (one per receipt).

    Grading-bookkeeping fields (PR #14 — clean-denominator grading):
      - ``grade`` is the raw grader verdict — kept narrow
        (full_match | partial_match | no_match | atlas_not_found) so
        downstream code branching on grade values doesn't need a fifth
        case. Per Codex's PR #14 design note, ``skipped_receipt_stale``
        is NOT a grade value.
      - ``score_status`` is the new bookkeeping field that says how this
        row should be counted. ``counted`` contributes to both
        ``raw_pass_pct`` and ``clean_pass_pct``;
        ``skipped_receipt_stale`` (or any non-``counted`` value)
        contributes to ``raw_pass_pct`` only — ``clean_pass_pct``
        removes the row from BOTH numerator and denominator so the
        operator score reflects retrieval performance, not receipt
        drift.
      - ``clean_excluded_reason`` carries the why for ``score_status``,
        machine-readable. Today's only value is ``receipt_stale``
        (cited anchor doesn't exist at run commit — from PR #13's
        ``source_snapshot_status == git_source_missing`` paired with
        grade ``no_match``). Reserved for future expansion:
        ``docs_work_excluded_by_design`` (PR #277 exclusion),
        ``non_repo_evidence`` (``external_tool_docs`` / ``user_context``
        / ``absence_search`` receipts).
      - ``lane`` is the inferred retrieval surface this receipt was
        scored on: ``explicit_source_fast_path`` (find_code w/ a
        ``source_path + source_lines`` anchor — PR #426 fast-path
        eligible), ``fuzzy_find_code`` (find_code w/o eligible
        anchor), ``scan_search``, or ``doc_resolver``. Inferred at
        row-construction time so the per-row classification persists
        in the artifact JSON.
    """

    question_id: str
    question: str
    grade: str          # full_match | partial_match | no_match | atlas_not_found
    confidence: float
    rationale: str
    tool: str           # find_code | scan_search | doc_resolver
    revision_binding: Optional[str] = None  # only for doc receipts
    artifact_id: Optional[str] = None       # T4a populates
    chunk_id: Optional[str] = None
    heading_path: Optional[list[str]] = None
    warnings: list[str] = field(default_factory=list)
    # PR #17: receipt's authoring intent. Preserved on the row so
    # downstream classifiers can chart non-retrieval categories
    # (external_tool_docs / user_context / absence_search) separately
    # from source_excerpt receipts. None for legacy artifacts.
    evidence_type: Optional[str] = None
    # PR #20: command-snapshot lane diagnostics. Populated when a
    # receipt's ``command_text`` (or a synthesized command from
    # source_path/source_lines) resolves through
    # :mod:`command_snapshot`. ``status`` is one of the
    # ``command_snapshot.STATUS_*`` constants. ``hash_match`` /
    # ``sha256`` mirror the source_snapshot fields' semantics.
    # ``output_head`` is capped at 2,000 chars for artifact-size
    # control.
    command_snapshot_status: Optional[str] = None
    command_snapshot_hash_match: Optional[bool] = None
    command_snapshot_sha256: Optional[str] = None
    command_snapshot_head: Optional[str] = None
    command_snapshot_exit_code: Optional[int] = None
    atlas_answer_len: int = 0
    atlas_returncode: Optional[int] = None
    atlas_exception: Optional[str] = None
    atlas_stderr_head: Optional[str] = None
    # PR #16: raw retrieval diagnostics from workspace_atlas_query.
    # ``atlas_retrieval_plan`` is the raw dict atlas emits — keys like
    # ``lanes_run`` / ``lanes_skipped`` / ``boosts`` /
    # ``lane_quotas_applied`` / ``path_anchors`` / ``symbol_anchors``.
    # Persisted untouched so downstream consumers (and future
    # ground-truthed lane inference) can analyze it directly.
    # ``atlas_citation_locations`` is the compact "path:line_start-line_end"
    # form of the citations list, truncated to the first 20 to keep
    # artifact JSONs tight. ``atlas_citation_count`` records the full
    # untruncated total so consumers can tell when the head was sampled.
    # The reranker_* pair are summary signals for fuzzy retrieval —
    # how many candidates the reranker considered and the top_k cap.
    atlas_retrieval_plan: Optional[dict[str, Any]] = None
    atlas_citation_locations: list[str] = field(default_factory=list)
    atlas_citation_count: Optional[int] = None
    atlas_reranker_candidates_considered: Optional[int] = None
    atlas_reranker_top_k_count: Optional[int] = None
    source_snapshot_status: Optional[str] = None
    source_snapshot_hash_match: Optional[bool] = None
    source_snapshot_sha256: Optional[str] = None
    # PR #15: parallel snapshot at the grading run_commit (not the
    # receipt's pinned source_commit). Lets `_derive_score_status`
    # distinguish "Atlas missed" from "the file moved between receipt
    # commit and grading commit." Values come from
    # ``code_snapshot.STATUS_RUN_COMMIT_*`` (e.g. ``run_commit_hash_match``).
    # ``None`` when no run_commit was available (e.g. doc_resolver
    # receipts or receipts without source_path+source_lines).
    run_snapshot_status: Optional[str] = None
    run_snapshot_hash_match: Optional[bool] = None
    run_snapshot_sha256: Optional[str] = None
    # PR #14: lane + clean-denominator bookkeeping.
    lane: Optional[str] = None  # explicit_source_fast_path|fuzzy_find_code|scan_search|doc_resolver
    score_status: str = "counted"  # counted | skipped_receipt_stale | skipped_run_commit_line_drift | …
    clean_excluded_reason: Optional[str] = None  # receipt_stale | run_commit_line_drift | …


@dataclass(frozen=True)
class GradingSummary:
    """Aggregate of one packet's grading run."""

    packet_id: str
    code_revision_id: Optional[str]
    base_sha: str
    threshold_pct: int
    rows: list[ReceiptGradingRow]

    @property
    def pass_count(self) -> int:
        return sum(
            1 for r in self.rows
            if r.grade in ("full_match", "partial_match")
        )

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def pass_pct(self) -> int:
        """Raw pass percentage — denominator includes excluded rows.

        Kept as the legacy operator score for trend continuity. New
        consumers should prefer :attr:`clean_pass_pct` once the daemon
        has been emitting ``score_status`` long enough that
        comparisons are apples-to-apples.
        """
        if not self.rows:
            return 0
        return int(round(self.pass_count * 100 / self.total))

    @property
    def passed(self) -> bool:
        return self.pass_pct >= self.threshold_pct

    # ── PR #14: clean-denominator score (excludes rows that aren't
    # ── actually measuring Atlas retrieval — receipt-stale anchors
    # ── and future similar bookkeeping skips).
    @property
    def excluded_count(self) -> int:
        """Rows excluded from the clean denominator. Counted by
        ``score_status != "counted"`` rather than by inspecting
        ``clean_excluded_reason`` so the boolean stays true even if a
        future ``score_status`` value lacks an explicit reason.
        """
        return sum(1 for r in self.rows if r.score_status != "counted")

    @property
    def skipped_receipt_stale_count(self) -> int:
        """Convenience: how many rows in this packet were stale-receipt
        skips? Surfaced separately because operators often want to
        track receipt-drift independently of other exclusions."""
        return sum(
            1 for r in self.rows
            if r.score_status == "skipped_receipt_stale"
        )

    @property
    def skipped_run_commit_line_drift_count(self) -> int:
        """PR #15: how many rows in this packet were skipped because the
        cited line range still renders at the receipt commit but the
        file was edited between receipt commit and grading run commit?
        Surfaced separately so operators can chart receipt-anchor drift
        vs. grading-commit drift independently — they hint at different
        upstream fixes (receipt-authoring discipline vs. fast-moving
        codebases that need run-commit-aware anchors).
        """
        return sum(
            1 for r in self.rows
            if r.score_status == "skipped_run_commit_line_drift"
        )

    # ── PR #17: four new pre-atlas skip categories.
    @property
    def skipped_non_repo_evidence_count(self) -> int:
        """Rows whose ``evidence_type`` is ``external_tool_docs`` or
        ``user_context`` — receipts that cite information outside the
        shadow corpus's repo entirely (Claude Code docs, user-provided
        empirical data). Atlas can't be expected to retrieve these.
        """
        return sum(
            1 for r in self.rows
            if r.score_status == "skipped_non_repo_evidence"
        )

    @property
    def skipped_absence_search_count(self) -> int:
        """Rows whose ``evidence_type`` is ``absence_search`` —
        receipts claiming "X does NOT exist in the repo." find_code /
        scan_search can't prove a negative; absence checks need a
        deterministic grep tool, not an LLM-grader-friendly retrieval
        surface. Surfaced separately because the upstream fix is
        different from corpus completeness or receipt-authoring.
        """
        return sum(
            1 for r in self.rows
            if r.score_status == "skipped_absence_search"
        )

    @property
    def skipped_unavailable_source_ref_count(self) -> int:
        """Rows where the receipt's cited source can't be materialized
        at ``source_commit`` (commit not in repo / file path doesn't
        exist at that commit). Parallel to ``skipped_receipt_stale``
        but covers doc receipts too. Atlas isn't being tested fairly
        when the receipt's anchor itself is unreachable.
        """
        return sum(
            1 for r in self.rows
            if r.score_status == "skipped_unavailable_source_ref"
        )

    @property
    def skipped_doc_corpus_excluded_count(self) -> int:
        """Rows whose ``source_path`` is under ``docs/work/**`` —
        PR #277 deliberately excludes these from shadow doc ingest to
        prevent grading ground-truth leakage. Counted separately so
        operators can confirm the exclusion is working as designed.
        """
        return sum(
            1 for r in self.rows
            if r.score_status == "skipped_doc_corpus_excluded"
        )

    @property
    def skipped_command_snapshot_count(self) -> int:
        """PR #20: rows whose ``command_text`` (or synthesized
        path/line equivalent) was resolved as a deterministic source
        check rather than an atlas query. Routed through
        :mod:`command_snapshot`. Surfaced separately so operators can
        chart the command-lane volume independently — its upstream
        fix (extend the command shape whitelist or improve parsers)
        is distinct from receipt staleness or doc corpus completeness.
        """
        return sum(
            1 for r in self.rows
            if r.score_status == "skipped_command_snapshot"
        )

    @property
    def by_evidence_type(self) -> dict[str, dict[str, Any]]:
        """Breakdown of receipts by ``evidence_type``.

        Returns a dict keyed by evidence_type bucket. The four buckets
        match :mod:`grader_service`'s routing distinctions:
        ``source_excerpt`` (default for legacy receipts whose
        ``evidence_type`` is None or missing), ``external_tool_docs``,
        ``user_context``, ``absence_search``. Any unexpected future
        value lands in ``other`` so adding a new evidence_type
        upstream doesn't silently corrupt counts.

        Each bucket carries ``receipts`` / ``correct`` / ``excluded``
        / ``clean_total`` / ``clean_pct``. ``clean_pct`` is ``None``
        when ``clean_total == 0`` (mirrors :attr:`clean_pass_pct`).

        Surfaced so the dashboard can answer the standing question
        "where is Atlas actually being measured?" — non-retrieval
        buckets should drop to zero clean_total once PR #17 + #19
        skip-routing land, leaving ``source_excerpt`` as the only
        bucket carrying a real clean denominator.
        """
        # Match the routing constants in :mod:`grader_service` — order
        # is deterministic so JSON rendering is stable across runs.
        buckets = [
            "source_excerpt",
            "external_tool_docs",
            "user_context",
            "absence_search",
        ]
        out: dict[str, dict[str, Any]] = {
            b: {"receipts": 0, "correct": 0, "excluded": 0} for b in buckets
        }
        out["other"] = {"receipts": 0, "correct": 0, "excluded": 0}
        for row in self.rows:
            et = (row.evidence_type or "").strip()
            if not et:
                # Legacy receipts and explicit-source_excerpt collapse
                # together — they share routing semantics in
                # :func:`grader_service._derive_score_status`.
                key = "source_excerpt"
            elif et in out:
                key = et
            else:
                key = "other"
            out[key]["receipts"] += 1
            if row.grade in ("full_match", "partial_match"):
                out[key]["correct"] += 1
            if row.score_status != "counted":
                out[key]["excluded"] += 1
        for vals in out.values():
            clean_total = vals["receipts"] - vals["excluded"]
            vals["clean_total"] = clean_total
            vals["clean_pct"] = (
                round(vals["correct"] * 100 / clean_total, 1)
                if clean_total > 0 else None
            )
        return out

    @property
    def clean_total(self) -> int:
        """Denominator for :attr:`clean_pass_pct`."""
        return self.total - self.excluded_count

    @property
    def clean_pass_pct(self) -> Optional[int]:
        """Pass percentage with excluded rows removed from BOTH
        numerator and denominator. ``None`` when ``clean_total == 0``
        (every row was excluded — score is undefined, not zero).
        """
        if self.clean_total <= 0:
            return None
        # ``pass_count`` filters by grade — passing rows are by
        # definition ``score_status='counted'`` (we never emit
        # skipped+pass tuples), so it remains the clean-numerator
        # without further filtering.
        return int(round(self.pass_count * 100 / self.clean_total))


def build_comment_markdown(summary: GradingSummary) -> str:
    """Render the per-PR Markdown comment for a single packet.

    Backward-compatible single-packet wrapper around
    :func:`build_comment_markdown_for_summaries` for callers that only
    have one summary in hand (the common case).
    """
    return build_comment_markdown_for_summaries([summary])


def build_comment_markdown_for_summaries(
    summaries: list[GradingSummary],
) -> str:
    """Render a multi-packet Markdown comment.

    Multi-packet PRs (rare but legal — a PR may touch more than one
    ``02-qna-log.md``) get a single comment with one section per packet
    plus an overall pass/fail badge. The single-packet case renders
    identically to the prior ``build_comment_markdown(summary)`` shape
    (one section, no aggregate header) so existing tests + downstream
    parsers don't need to change.

    Codex review on impl PR (2026-05-15) caught that posting one comment
    per packet using the same marker would have the marker-based update
    in :func:`post_or_update_pr_comment` PATCH-overwrite each prior
    section — final comment would only contain the last packet's rows
    even though the commit status aggregates all packets. Building ONE
    comment from all summaries fixes that.
    """
    if not summaries:
        return COMMENT_MARKER + "\n\n_No packet receipts found in this PR._\n"

    sections: list[str] = [COMMENT_MARKER]
    if len(summaries) > 1:
        all_passed = all(s.passed for s in summaries)
        total_pass = sum(s.pass_count for s in summaries)
        total = sum(s.total for s in summaries)
        overall_pct = int(round(total_pass * 100 / total)) if total else 0
        overall_badge = "PASS" if all_passed else "FAIL"
        sections.append(
            f"## atlas-shadow grading — {len(summaries)} packets\n\n"
            f"**Overall:** **{overall_badge}** "
            f"({total_pass}/{total} = {overall_pct}% across all packets)\n"
        )

    for summary in summaries:
        sections.append(_render_single_packet_section(summary))

    return "\n".join(sections) + "\n"


def _render_single_packet_section(summary: GradingSummary) -> str:
    """Render one packet's section (header + table + warnings).

    Used by both the single-packet wrapper and the multi-packet renderer.
    """
    has_doc = any(r.revision_binding for r in summary.rows)
    badge = "PASS" if summary.passed else "FAIL"
    rev = summary.code_revision_id or "(no code_revision_id — base SHA not yet ingested)"
    header = [
        "## atlas-shadow grading",
        "",
        f"**Packet:** `{summary.packet_id}`",
        f"**Base SHA:** `{summary.base_sha[:12]}`",
        f"**Pinned revision:** `{rev}`",
        f"**Threshold:** {summary.threshold_pct}%  "
        f"**Result:** **{badge}** "
        f"({summary.pass_count}/{summary.total} = {summary.pass_pct}%)",
        "",
    ]
    if has_doc:
        cols = (
            "| qid | question | tool | grade | conf | revision_binding | "
            "rationale |"
        )
        sep = "| --- | --- | --- | --- | --- | --- | --- |"
    else:
        cols = "| qid | question | tool | grade | conf | rationale |"
        sep = "| --- | --- | --- | --- | --- | --- |"
    lines = [cols, sep]
    for row in summary.rows:
        q = _escape_cell(row.question, max_len=80)
        rationale = _escape_cell(row.rationale, max_len=160)
        conf = f"{row.confidence:.2f}"
        if has_doc:
            binding = row.revision_binding or ""
            lines.append(
                f"| {row.question_id} | {q} | {row.tool} | {row.grade} | "
                f"{conf} | {binding} | {rationale} |"
            )
        else:
            lines.append(
                f"| {row.question_id} | {q} | {row.tool} | {row.grade} | "
                f"{conf} | {rationale} |"
            )
    body = "\n".join(header + lines)
    warnings = sorted({
        w for row in summary.rows for w in (row.warnings or []) if w
    })
    if warnings:
        body += "\n\n### Resolver warnings\n\n"
        body += "\n".join(f"- `{w}`" for w in warnings)
    return body


def _escape_cell(s: str, *, max_len: int) -> str:
    """Make a string safe for a Markdown table cell.

    Replaces newlines + pipes; truncates long values with an ellipsis.
    Backticks are left alone (we want inline code rendering).
    """
    if not s:
        return ""
    t = s.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t


# ---------------------------------------------------------------------------
# GitHub Issues API integration
# ---------------------------------------------------------------------------


def list_issue_comments(
    *,
    repo_full_name: str,
    pr_number: int,
    github_token: str,
    _http: Callable = gh_check_mod.http_request,
) -> list[dict[str, Any]]:
    """GET /repos/{owner}/{repo}/issues/{number}/comments — page 1 only.

    Returns the parsed JSON (a list). For most PRs page 1 is sufficient
    (GitHub returns 30 per page by default; grader runs are typically
    the first or second comment thread item). Pagination is intentionally
    not implemented for v1 — if it becomes a problem in practice (very
    long PRs), the grader's marker-based update still works on later
    pages too (it'll just create a duplicate comment on the first call
    for that PR and the second call onwards will find and update it).
    """
    url = (
        f"{gh_check_mod.GITHUB_API_BASE}/repos/{repo_full_name}/issues/"
        f"{pr_number}/comments?per_page=100"
    )
    resp = _http(
        method="GET",
        url=url,
        headers=gh_check_mod._auth_headers(github_token),
    )
    if not (200 <= resp.status < 300):
        raise RuntimeError(
            f"list_issue_comments failed: status={resp.status} "
            f"body={resp.body[:500]!r}"
        )
    data = resp.json() or []
    return data if isinstance(data, list) else []


def post_or_update_pr_comment(
    *,
    repo_full_name: str,
    pr_number: int,
    body: str,
    github_token: str,
    _http: Callable = gh_check_mod.http_request,
    marker: str = COMMENT_MARKER,
) -> dict[str, Any]:
    """Post (or update) the atlas-shadow grading comment.

    Strategy: list the PR's existing comments, look for one carrying
    ``marker`` (an HTML comment we always include in :func:`build_comment_markdown`).
    If found, PATCH that comment with the new body. Otherwise POST a
    new comment.

    Returns the parsed JSON response (which carries the comment id +
    html_url).
    """
    existing = list_issue_comments(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        github_token=github_token,
        _http=_http,
    )
    target_id: Optional[int] = None
    for c in existing:
        if not isinstance(c, dict):
            continue
        if marker in (c.get("body") or ""):
            target_id = c.get("id")
            break

    headers = gh_check_mod._auth_headers(github_token)
    payload = {"body": body}
    payload_bytes = __import__("json").dumps(payload).encode("utf-8")

    if target_id is not None:
        url = (
            f"{gh_check_mod.GITHUB_API_BASE}/repos/{repo_full_name}/issues/"
            f"comments/{target_id}"
        )
        resp = _http(method="PATCH", url=url, body=payload_bytes, headers=headers)
    else:
        url = (
            f"{gh_check_mod.GITHUB_API_BASE}/repos/{repo_full_name}/issues/"
            f"{pr_number}/comments"
        )
        resp = _http(method="POST", url=url, body=payload_bytes, headers=headers)
    if not (200 <= resp.status < 300):
        raise RuntimeError(
            f"post_or_update_pr_comment failed: status={resp.status} "
            f"body={resp.body[:500]!r}"
        )
    return resp.json() or {}
