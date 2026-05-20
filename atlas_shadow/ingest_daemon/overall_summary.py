"""overall_summary — cross-run dashboard for shadow-mode grading.

After every batch grading run (or PR-event run, in the future), this
module rewrites two files at the root of ``shadow-runs/``:

- ``overall-summary.md`` — human-readable executive summary. Two
  sections: a regression/improvement callout (any packet that moved
  >=10pp from the previous run), then a flat "Run history" table
  listing every batch with aggregate totals + delta vs prior run.
  Deliberately NO per-packet detail at this level — drilling into a
  packet's history lives in ``baseline-<date>/summary.md`` and the
  per-receipt JSON.

- ``overall-summary.json`` — machine-readable mirror of the same data,
  for downstream tooling (dashboards, alerting, diff views).

The regeneration is pure: it reads every ``baseline-*/manifest.json``
on disk, sorts by ``started_at``, and rewrites both files atomically
(tempfile → fsync → rename). A crash mid-write does not corrupt the
dashboard. Re-running the regen is idempotent.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import counted_misses as counted_misses_mod


REGRESSION_THRESHOLD_PP = 10  # >=10pp drop from prior run flags as regression
IMPROVEMENT_THRESHOLD_PP = 10  # >=10pp gain from prior run flags as improvement
OVERALL_MD_NAME = "overall-summary.md"
OVERALL_JSON_NAME = "overall-summary.json"

# Canonical bucket order for the by-evidence-type breakdown. Kept in
# sync with :data:`atlas_shadow.ingest_daemon.grade_batch.EVIDENCE_TYPE_BUCKETS`
# — both modules render the same column order so a chart consumer
# reading the JSON doesn't have to detect order.
_EVIDENCE_TYPE_RENDER_ORDER: tuple[str, ...] = (
    "source_excerpt",
    "external_tool_docs",
    "user_context",
    "absence_search",
    "other",
)

# Canonical bucket order for the by-lane breakdown. Kept in sync with
# :data:`atlas_shadow.ingest_daemon.grade_batch.LANE_BUCKETS`. Same
# rationale as _EVIDENCE_TYPE_RENDER_ORDER — both modules render the
# same column order so a chart consumer reading the JSON doesn't have
# to detect order.
_LANE_RENDER_ORDER: tuple[str, ...] = (
    "explicit_source_fast_path",
    "fuzzy_find_code",
    "scan_search",
    "doc_resolver",
    "non_retrieval",
    "other",
)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via tempfile → fsync → rename.

    Mirrors the pattern atlas-shadow uses for ``state_file._atomic_write_json``.
    A crash between fsync and rename leaves the original file untouched;
    a crash after rename is fine because the new content is durable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in the same dir so the rename is on the same
    # filesystem (atomic). delete=False because we close + rename.
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except Exception:
        # Best-effort cleanup if we never got to rename.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def load_run_manifests(shadow_runs_root: Path) -> list[dict[str, Any]]:
    """Walk ``<shadow_runs_root>/baseline-*/manifest.json``.

    Returns a list of manifest dicts sorted by ``started_at`` ascending
    (oldest first). Manifests that can't be parsed (corrupted JSON,
    missing keys) are skipped with a quiet stderr warning rather than
    aborting the regen — partial data is better than no dashboard.

    **De-duplication:** when two directories carry the same ``run_name``
    field (a common shape after archive/backup — e.g. someone copies
    ``baseline-2026-05-15/`` to ``baseline-2026-05-15-broken-no-workspace-py/``
    without updating the JSON), the canonical row wins and the others
    are dropped with a warning. The selection rule prefers (in order):

      1. The directory whose name matches ``run_name`` exactly. This
         is the convention :func:`grade_batch.write_manifest` writes
         under, so the canonical run picks itself out.
      2. Otherwise, the latest ``started_at``. Without a directory-
         name match there's no way to tell which copy is canonical;
         "later finished" is the least-bad heuristic.

    The dropped entries get a stderr warning naming the directory so
    the operator can rename or move it.
    """
    if not shadow_runs_root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for run_dir in sorted(shadow_runs_root.glob("baseline-*")):
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            import sys
            print(
                f"[overall_summary] WARN: skipping {manifest_path}: {exc}",
                file=sys.stderr,
            )
            continue
        # Codex r7 P2: a manifest that's valid JSON but not a dict (e.g.
        # `[]` from a bad manual edit, or `null` from a partial write)
        # would crash the `_dir` assignment below. Since every batch run
        # calls regenerate(), one bad historical manifest would break
        # every subsequent batch — so we skip-on-shape too, not just
        # skip-on-parse-error.
        if not isinstance(manifest, dict):
            import sys
            print(
                f"[overall_summary] WARN: skipping {manifest_path}: "
                f"manifest is not a JSON object (got {type(manifest).__name__})",
                file=sys.stderr,
            )
            continue
        manifest["_dir"] = run_dir.name  # for cross-reference
        runs.append(manifest)
    runs = _dedupe_by_run_name(runs)
    runs.sort(key=lambda r: r.get("started_at", ""))
    return runs


def _dedupe_by_run_name(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse runs sharing the same ``run_name`` to one canonical row.

    Selection rule (matches the docstring on
    :func:`load_run_manifests`):

      1. Directory name matches ``run_name`` exactly → canonical.
      2. Otherwise, the latest ``started_at`` wins.

    Runs lacking a ``run_name`` (very old / malformed manifests) are
    grouped by their directory name instead — they can't collide with
    each other since directory names are unique.
    """
    import sys
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        # Fall back to dir name when run_name is missing — that means
        # the run is unique by directory anyway.
        key = r.get("run_name") or r.get("_dir") or ""
        grouped.setdefault(key, []).append(r)

    kept: list[dict[str, Any]] = []
    for key, candidates in grouped.items():
        if len(candidates) == 1:
            kept.append(candidates[0])
            continue
        # Prefer the candidate whose directory name matches run_name.
        canonical = next(
            (
                c for c in candidates
                if c.get("_dir") and c.get("_dir") == c.get("run_name")
            ),
            None,
        )
        if canonical is None:
            # No directory-name match — pick the latest started_at.
            canonical = max(
                candidates,
                key=lambda c: c.get("started_at", ""),
            )
        kept.append(canonical)
        dropped = [
            c.get("_dir") or "<no _dir>" for c in candidates if c is not canonical
        ]
        if dropped:
            print(
                f"[overall_summary] WARN: run_name={key!r} appears in multiple "
                f"directories; keeping {canonical.get('_dir')!r}, "
                f"dropping {dropped!r} (rename or move them out of "
                f"baseline-*/)",
                file=sys.stderr,
            )
    return kept


def _compute_callouts(
    runs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare the latest run to the previous run; return
    (regressions, improvements) lists.

    Each entry: ``{packet_slug, prev_pct, curr_pct, delta_pp}``. Only
    packets present in BOTH runs are diffed; a packet that's brand-new
    or has disappeared is not a regression (it's a structural change).
    """
    if len(runs) < 2:
        return [], []
    prev = runs[-2]
    curr = runs[-1]
    prev_pkts = prev.get("per_packet_pct", {})
    curr_pkts = curr.get("per_packet_pct", {})
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for slug, curr_data in curr_pkts.items():
        if slug not in prev_pkts:
            continue
        prev_pct = prev_pkts[slug].get("pct", 0.0)
        curr_pct = curr_data.get("pct", 0.0)
        delta = round(curr_pct - prev_pct, 1)
        entry = {
            "packet_slug": slug,
            "prev_pct": prev_pct,
            "curr_pct": curr_pct,
            "delta_pp": delta,
            "prev_run": prev.get("run_name", prev.get("_dir")),
            "curr_run": curr.get("run_name", curr.get("_dir")),
        }
        if delta <= -REGRESSION_THRESHOLD_PP:
            regressions.append(entry)
        elif delta >= IMPROVEMENT_THRESHOLD_PP:
            improvements.append(entry)
    # Sort regressions by biggest drop first, improvements by biggest gain.
    regressions.sort(key=lambda e: e["delta_pp"])
    improvements.sort(key=lambda e: e["delta_pp"], reverse=True)
    return regressions, improvements


def _format_md(
    runs: list[dict[str, Any]],
    regressions: list[dict[str, Any]],
    improvements: list[dict[str, Any]],
) -> str:
    """Render the overall-summary.md content.

    Order: header, regressions, improvements, run history. The
    regression callout is at the top because that's the action-
    oriented signal — open this file and immediately see what got
    worse, then scroll down for context.
    """
    lines: list[str] = ["# Atlas shadow-mode — overall", ""]
    if runs:
        latest = runs[-1]
        lines.append(f"_Last updated: {latest.get('finished_at', 'unknown')}_")
        lines.append(f"_Total runs on disk: {len(runs)}_")
    else:
        lines.append("_No runs on disk yet._")
    lines.append("")

    if regressions:
        lines.append(f"## \U0001F534 Regressions (latest run dropped >={REGRESSION_THRESHOLD_PP}pp vs prior)")
        lines.append("")
        for r in regressions:
            lines.append(
                f"- **{r['packet_slug']}**: "
                f"{r['prev_pct']:.1f}% → {r['curr_pct']:.1f}% "
                f"(**{r['delta_pp']:+.1f}pp**) "
                f"_{r['prev_run']} → {r['curr_run']}_"
            )
        lines.append("")

    if improvements:
        lines.append(f"## ✅ Improvements (latest run gained >={IMPROVEMENT_THRESHOLD_PP}pp vs prior)")
        lines.append("")
        for r in improvements:
            lines.append(
                f"- **{r['packet_slug']}**: "
                f"{r['prev_pct']:.1f}% → {r['curr_pct']:.1f}% "
                f"(**{r['delta_pp']:+.1f}pp**) "
                f"_{r['prev_run']} → {r['curr_run']}_"
            )
        lines.append("")

    # Per-evidence-type breakdown for the LATEST run only. The dashboard
    # answers "where is Atlas actually being measured?" — non-retrieval
    # buckets (external_tool_docs / user_context / absence_search)
    # should show zero clean_total once #17 + #19 routing is fully
    # working, leaving source_excerpt as the only bucket carrying a
    # real clean denominator. We render only the latest run because a
    # per-run breakdown would balloon the markdown; the JSON exposes
    # every run for downstream charting.
    if runs:
        latest_breakdown = runs[-1].get("total_by_evidence_type")
        if isinstance(latest_breakdown, dict) and any(
            (latest_breakdown.get(b) or {}).get("receipts", 0)
            for b in _EVIDENCE_TYPE_RENDER_ORDER
        ):
            lines.append("## Latest run — by evidence type")
            lines.append("")
            lines.append(
                "| Evidence type | Receipts | Excluded | Clean total | "
                "Correct | Clean % |"
            )
            lines.append("|---|---|---|---|---|---|")
            for bucket in _EVIDENCE_TYPE_RENDER_ORDER:
                vals = latest_breakdown.get(bucket) or {}
                receipts = int(vals.get("receipts", 0) or 0)
                # Suppress all-zero rows so the table stays scannable.
                # The full breakdown is always in the JSON.
                if receipts == 0:
                    continue
                excluded = int(vals.get("excluded", 0) or 0)
                clean_total = int(vals.get("clean_total", 0) or 0)
                correct = int(vals.get("correct", 0) or 0)
                clean_pct = vals.get("clean_pct")
                clean_pct_str = (
                    f"{clean_pct:.1f}%" if isinstance(clean_pct, (int, float))
                    else "—"
                )
                lines.append(
                    f"| `{bucket}` | {receipts} | {excluded} | "
                    f"{clean_total} | {correct} | {clean_pct_str} |"
                )
            lines.append("")
            lines.append(
                "_Non-retrieval buckets (`external_tool_docs`, "
                "`user_context`, `absence_search`) should show "
                "`clean_total=0` once skip routing is fully working "
                "— their rows aren't testing Atlas retrieval. "
                "`source_excerpt` carries the real tuning denominator._"
            )
            lines.append("")

    # Per-lane breakdown for the LATEST run only — sibling section of
    # the by-evidence-type breakdown above. Surfaces which retrieval
    # surface the clean denominator is dominated by, so when a lane-
    # specific fix lands (e.g. a doc_resolver improvement) its impact
    # can be attributed to that lane's clean_pct movement without the
    # operator having to drill into per-receipt rows. Latest-run-only
    # for the same reason as the evidence-type section: a per-run
    # breakdown would balloon the markdown; the JSON exposes every
    # run.
    if runs:
        latest_lane_breakdown = runs[-1].get("total_by_lane")
        if isinstance(latest_lane_breakdown, dict) and any(
            (latest_lane_breakdown.get(b) or {}).get("receipts", 0)
            for b in _LANE_RENDER_ORDER
        ):
            lines.append("## Latest run — by retrieval lane")
            lines.append("")
            lines.append(
                "| Lane | Receipts | Excluded | Clean total | "
                "Correct | Clean % |"
            )
            lines.append("|---|---|---|---|---|---|")
            for bucket in _LANE_RENDER_ORDER:
                vals = latest_lane_breakdown.get(bucket) or {}
                receipts = int(vals.get("receipts", 0) or 0)
                # Suppress all-zero rows for readability; full
                # breakdown is in the JSON.
                if receipts == 0:
                    continue
                excluded = int(vals.get("excluded", 0) or 0)
                clean_total = int(vals.get("clean_total", 0) or 0)
                correct = int(vals.get("correct", 0) or 0)
                clean_pct = vals.get("clean_pct")
                clean_pct_str = (
                    f"{clean_pct:.1f}%" if isinstance(clean_pct, (int, float))
                    else "—"
                )
                lines.append(
                    f"| `{bucket}` | {receipts} | {excluded} | "
                    f"{clean_total} | {correct} | {clean_pct_str} |"
                )
            lines.append("")
            lines.append(
                "_Lane = the retrieval surface this receipt was "
                "scored on. ``explicit_source_fast_path`` is "
                "PR #426 fast-path eligible (find_code with "
                "path+lines anchor); ``fuzzy_find_code`` is "
                "find_code without an anchor; ``doc_resolver`` "
                "is the docs-RAG path; ``non_retrieval`` is the "
                "pre-Atlas skip path (command_snapshot / non-repo "
                "evidence / unavailable source ref). A lane-specific "
                "fix should move that lane's `clean_pct` without "
                "disturbing the others._"
            )
            lines.append("")

    # Latest run — counted misses by fix layer. Surfaces the next
    # tuning queue right on the dashboard so an operator doesn't have
    # to know about the standalone ``shadow-counted-misses`` CLI.
    # Only rendered when the latest run actually has a counted-misses
    # report (i.e., per-receipt artifacts exist — modern instrumented
    # baselines). Legacy runs and runs with zero counted misses both
    # suppress the section.
    if runs:
        latest = runs[-1]
        latest_cm = latest.get("_counted_misses")
        if isinstance(latest_cm, dict) and int(latest_cm.get("total_misses", 0)) > 0:
            lines.append("## Latest run — counted misses by fix layer")
            lines.append("")
            total = int(latest_cm.get("total_misses", 0))
            by_layer = latest_cm.get("by_fix_layer") or {}
            by_lane_cm = latest_cm.get("by_lane") or {}
            lines.append(f"**{total} counted misses** across "
                         f"{sum(by_lane_cm.values())} rows in "
                         f"{len(by_lane_cm)} lanes.")
            lines.append("")
            lines.append("| Fix layer | Misses |")
            lines.append("|---|---:|")
            # Sort by miss count descending so the largest queue is at top.
            for layer, count in sorted(
                by_layer.items(), key=lambda kv: (-int(kv[1] or 0), kv[0])
            ):
                lines.append(f"| `{layer}` | {count} |")
            lines.append("")
            # Relative link works because overall-summary.md lives at
            # <shadow_runs_root>/overall-summary.md and counted-misses.md
            # lives at <shadow_runs_root>/<run-dir>/_counted_misses/.
            latest_dir = latest.get("_dir") or latest.get("run_name")
            if latest_dir:
                cm_md_link = f"{latest_dir}/_counted_misses/counted-misses.md"
                lines.append(
                    f"_Per-row detail: [{cm_md_link}]({cm_md_link})._"
                )
            lines.append(
                "_Fix layer is a triage label, not a grader verdict. "
                "`receipt_anchor_mismatch` / `receipt_snapshot_mismatch` "
                "→ receipt re-authoring. `explicit_span_*` → narrow span "
                "in receipt or expansion in Atlas. `fuzzy_retrieval` → "
                "real ranking work. `receipt_or_command_routing` → "
                "consider `command_text` or `evidence_type` change. "
                "`doc_resolver` / `empty_atlas_response` → drill into "
                "the row to decide._"
            )
            lines.append("")

    lines.append("## Run history")
    lines.append("")
    # PR #14: dashboard now shows raw + clean pct side by side. Legacy
    # runs (no clean_overall_pct in their manifest) render the clean
    # column as `n/a`, so the table stays back-compatible.
    lines.append(
        "| Run | Code SHA | Packets | Receipts | Correct | Raw % | Clean % | "
        "Excluded | Δ raw |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    prev_pct: Optional[float] = None
    for run in runs:
        run_name = run.get("run_name") or run.get("_dir") or "unknown"
        commit_sha = run.get("commit_sha", "?")
        short_sha = commit_sha[:7] if commit_sha else "?"
        n_pkts = run.get("total_packets", 0)
        n_recs = run.get("total_receipts", 0)
        n_correct = run.get("total_correct", 0)
        pct = run.get("overall_pct", 0.0)
        clean_pct = run.get("clean_overall_pct")
        excluded = run.get("total_excluded", 0)
        clean_str = (
            f"{clean_pct:.1f}%" if isinstance(clean_pct, (int, float))
            else "n/a"
        )
        if prev_pct is None:
            delta_str = "—"
        else:
            d = round(pct - prev_pct, 1)
            arrow = "↑" if d > 0 else ("↓" if d < 0 else "→")
            delta_str = f"{d:+.1f}pp {arrow}"
        lines.append(
            f"| {run_name} | `{short_sha}` | {n_pkts} | {n_recs} | "
            f"{n_correct} | {pct:.1f}% | {clean_str} | {excluded} | {delta_str} |"
        )
        prev_pct = pct
    lines.append("")
    lines.append(
        "_**Raw %** counts every receipt in the denominator (legacy score). "
        "**Clean %** excludes rows the daemon flagged as not measuring "
        "Atlas retrieval: receipt-stale anchors "
        "(`score_status=skipped_receipt_stale`) and (PR #15) run-commit "
        "line drift (`score_status=skipped_run_commit_line_drift` — the "
        "file moved between receipt commit and grading commit so the cited "
        "line numbers now point at different code). The two breakouts are "
        "available per-run in `baseline-<date>/manifest.json`. Pre-PR-14 "
        "baselines render Clean % as `n/a`._"
    )
    lines.append("")
    lines.append(
        "_Drill into a specific run via "
        "`baseline-<date>/summary.md` (per-packet) or "
        "`baseline-<date>/packets/<packet>.json` (per-receipt)._"
    )
    lines.append("")
    return "\n".join(lines)


def _format_json(
    runs: list[dict[str, Any]],
    regressions: list[dict[str, Any]],
    improvements: list[dict[str, Any]],
) -> str:
    """Render the overall-summary.json content."""
    payload = {
        "runs": [
            {
                "run_name": r.get("run_name") or r.get("_dir"),
                "commit_sha": r.get("commit_sha"),
                "code_revision_id": r.get("code_revision_id"),
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "total_packets": r.get("total_packets", 0),
                "total_receipts": r.get("total_receipts", 0),
                "total_correct": r.get("total_correct", 0),
                "overall_pct": r.get("overall_pct", 0.0),
                # PR #14: clean-denominator score (None on legacy runs).
                "clean_overall_pct": r.get("clean_overall_pct"),
                "clean_total": r.get("clean_total"),
                "total_excluded": r.get("total_excluded", 0),
                "total_skipped_receipt_stale": r.get(
                    "total_skipped_receipt_stale", 0
                ),
                # PR #15: run-commit drift breakout. Legacy manifests
                # render zero; consumers can chart receipt-stale vs.
                # run-drift independently.
                "total_skipped_run_commit_line_drift": r.get(
                    "total_skipped_run_commit_line_drift", 0
                ),
                # PR #17: four non-retrieval skip totals — each has a
                # distinct upstream fix so consumers can chart them
                # independently. Legacy manifests render zero.
                "total_skipped_non_repo_evidence": r.get(
                    "total_skipped_non_repo_evidence", 0
                ),
                "total_skipped_absence_search": r.get(
                    "total_skipped_absence_search", 0
                ),
                "total_skipped_unavailable_source_ref": r.get(
                    "total_skipped_unavailable_source_ref", 0
                ),
                "total_skipped_doc_corpus_excluded": r.get(
                    "total_skipped_doc_corpus_excluded", 0
                ),
                # PR #20: command-snapshot lane total.
                "total_skipped_command_snapshot": r.get(
                    "total_skipped_command_snapshot", 0
                ),
                # Per-evidence-type breakdown (run level). None on legacy
                # manifests that pre-date the rollup — consumers should
                # treat None as "no breakdown available" rather than
                # "all buckets at zero."
                "total_by_evidence_type": r.get("total_by_evidence_type"),
                # Per-lane breakdown (run level). Same back-compat
                # contract as total_by_evidence_type.
                "total_by_lane": r.get("total_by_lane"),
                # PR atlas-shadow-query-cache-v1: cache observability
                # totals per run. Three counters answer "did this
                # run get faster because Atlas improved, or because
                # the cache hid the work?" Legacy manifests get
                # zero (the fields didn't exist pre-cache).
                "total_atlas_cache_hits": r.get("total_atlas_cache_hits", 0),
                "total_atlas_cache_misses": r.get("total_atlas_cache_misses", 0),
                "total_atlas_cache_disabled": r.get(
                    "total_atlas_cache_disabled", 0
                ),
                # Counted-misses summary (run level). ``None`` on legacy
                # runs that don't have per-receipt artifacts to read.
                # Carries ``total_misses`` + ``by_lane`` + ``by_fix_layer``
                # so consumers can chart the tuning queue across runs
                # without re-reading per-row artifacts.
                "counted_misses": (
                    {
                        "total_misses": r["_counted_misses"].get(
                            "total_misses", 0
                        ),
                        "by_lane": r["_counted_misses"].get("by_lane") or {},
                        "by_fix_layer": (
                            r["_counted_misses"].get("by_fix_layer") or {}
                        ),
                    }
                    if isinstance(r.get("_counted_misses"), dict)
                    else None
                ),
                "grader_backend": r.get("grader_backend"),
                "grader_model": r.get("grader_model"),
            }
            for r in runs
        ],
        "regressions": regressions,
        "improvements": improvements,
        "regression_threshold_pp": REGRESSION_THRESHOLD_PP,
        "improvement_threshold_pp": IMPROVEMENT_THRESHOLD_PP,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


_COUNTED_MISSES_DIR_NAME = "_counted_misses"


def _ensure_counted_misses_report(run_dir: Path) -> Optional[dict[str, Any]]:
    """Build (or refresh) ``<run_dir>/_counted_misses/counted-misses.{md,json}``
    if the run has artifacts to read. Returns the parsed JSON payload, or
    ``None`` when the run lacks artifacts (legacy / pre-instrumentation
    baselines).

    Failure modes are non-fatal: any exception during build_report /
    write_reports is logged to stderr and ``None`` is returned. The
    dashboard regen prefers degraded rendering over a crash because
    counted-misses depends on per-receipt artifacts that older
    baselines never wrote — and a corrupted artifact in one run
    shouldn't prevent the rest of the dashboard from updating.
    """
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir():
        # Legacy / aggregate-only baselines didn't write per-receipt
        # artifacts. counted_misses.build_report would just produce an
        # empty report; skipping is faster and avoids creating empty
        # _counted_misses/ directories under historical runs.
        return None
    cm_dir = run_dir / _COUNTED_MISSES_DIR_NAME
    try:
        report = counted_misses_mod.build_report(run_dir)
        _, json_path = counted_misses_mod.write_reports(report, cm_dir)
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        import sys
        print(
            f"[overall_summary] WARN: counted_misses regen failed for "
            f"{run_dir.name!r}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


def regenerate(shadow_runs_root: Path) -> tuple[Path, Path]:
    """Rebuild ``overall-summary.md`` and ``overall-summary.json``
    from every ``<shadow_runs_root>/baseline-*/manifest.json``.

    Also refreshes each run's ``_counted_misses/counted-misses.{md,json}``
    via :func:`_ensure_counted_misses_report` so the operator always
    has a current per-run failure worklist alongside the dashboard.

    Returns the two paths written. Caller can log them.

    Atomic: each file is written via tempfile → fsync → rename so a
    mid-write crash leaves the prior content intact. Counted-misses
    sub-reports are written via the (non-atomic) counted_misses
    module — those are derived data and a crash mid-write leaves a
    stale-but-readable previous version.
    """
    runs = load_run_manifests(shadow_runs_root)
    # Refresh each run's counted-misses sub-report. Stash the parsed
    # payload on the run dict (under a leading underscore so it doesn't
    # collide with manifest keys) so the MD/JSON formatters can render
    # the by-fix-layer breakdown without re-reading disk.
    for run in runs:
        dir_name = run.get("_dir") or run.get("run_name")
        if not dir_name:
            continue
        cm_payload = _ensure_counted_misses_report(shadow_runs_root / dir_name)
        if cm_payload is not None:
            run["_counted_misses"] = cm_payload
    regressions, improvements = _compute_callouts(runs)
    md_path = shadow_runs_root / OVERALL_MD_NAME
    json_path = shadow_runs_root / OVERALL_JSON_NAME
    _atomic_write_text(md_path, _format_md(runs, regressions, improvements))
    _atomic_write_text(json_path, _format_json(runs, regressions, improvements))
    return md_path, json_path
