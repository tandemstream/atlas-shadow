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


REGRESSION_THRESHOLD_PP = 10  # >=10pp drop from prior run flags as regression
IMPROVEMENT_THRESHOLD_PP = 10  # >=10pp gain from prior run flags as improvement
OVERALL_MD_NAME = "overall-summary.md"
OVERALL_JSON_NAME = "overall-summary.json"


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
    runs.sort(key=lambda r: r.get("started_at", ""))
    return runs


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


def regenerate(shadow_runs_root: Path) -> tuple[Path, Path]:
    """Rebuild ``overall-summary.md`` and ``overall-summary.json``
    from every ``<shadow_runs_root>/baseline-*/manifest.json``.

    Returns the two paths written. Caller can log them.

    Atomic: each file is written via tempfile → fsync → rename so a
    mid-write crash leaves the prior content intact.
    """
    runs = load_run_manifests(shadow_runs_root)
    regressions, improvements = _compute_callouts(runs)
    md_path = shadow_runs_root / OVERALL_MD_NAME
    json_path = shadow_runs_root / OVERALL_JSON_NAME
    _atomic_write_text(md_path, _format_md(runs, regressions, improvements))
    _atomic_write_text(json_path, _format_json(runs, regressions, improvements))
    return md_path, json_path
