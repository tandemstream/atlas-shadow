"""compare_runs — diff two ``shadow-runs/baseline-*`` directories.

Each shadow-mode grading run lands as ``shadow-runs/baseline-<date>/``
with a ``manifest.json`` (aggregate totals) and one
``packets/<slug>.json`` per packet (per-receipt detail). This module
loads two such runs, matches receipts by ``(packet_slug,
question_id)``, classifies the transitions, and emits both a
Markdown report and a JSON payload.

**Why it exists:** post-PR-#20 the clean denominator changes via
multiple distinct skip-routing PRs (#14/#15/#17/#19/#20 in
atlas-shadow, plus tuning PRs in core). When a probe re-runs, the
operator needs to know "which receipts moved, and how?" — not just
"clean % moved from X to Y." This module answers:

  - **Run level:** clean / raw delta, skip-category shifts,
    per-evidence-type clean-denominator shifts.
  - **Per packet:** per-packet clean-pct delta + a breakdown of how
    many receipts in each transition bucket.
  - **Per receipt:** the actual transitions (newly_passing /
    newly_failing / newly_skipped / un_skipped_* / etc.) — useful
    to spot specific receipts to drill into.

**Out of scope (v1):** no rendering inside the ingest daemon; this is
a manual CLI invocation via ``shadow-compare-runs``. No automatic
"diff against prior baseline" rebuild on every grade run — that would
balloon ``shadow-runs/`` storage and double-write under the
``overall_summary.regenerate`` path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ─── Transition taxonomy ──────────────────────────────────────────────


PASS_GRADES: frozenset[str] = frozenset({"full_match", "partial_match"})

# Receipt present in both runs, counted both sides:
TRANSITION_STILL_PASSING = "still_passing"
TRANSITION_STILL_FAILING = "still_failing"
# Counted-fail (or counted-pass) → counted-pass (or counted-fail):
TRANSITION_NEWLY_PASSING = "newly_passing"   # was counted-fail, now counted-pass
TRANSITION_NEWLY_FAILING = "newly_failing"   # was counted-pass, now counted-fail
# Counted ↔ skipped:
TRANSITION_NEWLY_SKIPPED = "newly_skipped"   # was counted (any grade), now skipped
TRANSITION_UN_SKIPPED_PASSING = "un_skipped_passing"   # was skipped, now counted-pass
TRANSITION_UN_SKIPPED_FAILING = "un_skipped_failing"   # was skipped, now counted-fail
TRANSITION_STILL_SKIPPED = "still_skipped"   # both skipped (status may differ)
# Receipt only in one side:
TRANSITION_APPEARED = "appeared"             # not in before
TRANSITION_DISAPPEARED = "disappeared"       # not in after


# Ordered for stable rendering in reports. Net-positive transitions
# (good for the clean score) come first, then neutral, then the
# regression categories operators most need to see.
TRANSITION_RENDER_ORDER: tuple[str, ...] = (
    TRANSITION_NEWLY_PASSING,
    TRANSITION_UN_SKIPPED_PASSING,
    TRANSITION_NEWLY_SKIPPED,
    TRANSITION_STILL_PASSING,
    TRANSITION_STILL_FAILING,
    TRANSITION_STILL_SKIPPED,
    TRANSITION_UN_SKIPPED_FAILING,
    TRANSITION_NEWLY_FAILING,
    TRANSITION_APPEARED,
    TRANSITION_DISAPPEARED,
)


# Skip-category fields surfaced in the run-level delta table. Order
# matches the dashboard's order so a reader fluent in one is fluent
# in the other.
SKIP_CATEGORIES: tuple[str, ...] = (
    "total_skipped_receipt_stale",
    "total_skipped_run_commit_line_drift",
    "total_skipped_non_repo_evidence",
    "total_skipped_absence_search",
    "total_skipped_unavailable_source_ref",
    "total_skipped_doc_corpus_excluded",
    "total_skipped_command_snapshot",
)


# Evidence-type buckets — kept in sync with
# :mod:`atlas_shadow.ingest_daemon.grade_batch.EVIDENCE_TYPE_BUCKETS`.
EVIDENCE_TYPE_BUCKETS: tuple[str, ...] = (
    "source_excerpt",
    "external_tool_docs",
    "user_context",
    "absence_search",
    "other",
)


# ─── Result dataclasses ───────────────────────────────────────────────


@dataclass(frozen=True)
class ReceiptTransition:
    """One receipt's before/after state + classified transition."""

    packet_slug: str
    question_id: str
    transition: str
    before_grade: Optional[str]
    after_grade: Optional[str]
    before_score_status: Optional[str]
    after_score_status: Optional[str]
    before_evidence_type: Optional[str]
    after_evidence_type: Optional[str]
    before_clean_excluded_reason: Optional[str] = None
    after_clean_excluded_reason: Optional[str] = None


@dataclass(frozen=True)
class PacketComparison:
    """Per-packet comparison: aggregate clean-pct delta + per-receipt
    transitions for the packet."""

    packet_slug: str
    before_receipts: int
    after_receipts: int
    before_clean_pct: Optional[float]
    after_clean_pct: Optional[float]
    clean_pct_delta_pp: Optional[float]
    transitions: list[ReceiptTransition] = field(default_factory=list)
    transition_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RunComparison:
    """Top-level result: run-level aggregates + per-packet detail."""

    before_run_name: str
    after_run_name: str
    before_raw_pct: float
    after_raw_pct: float
    raw_pct_delta_pp: float
    before_clean_pct: Optional[float]
    after_clean_pct: Optional[float]
    clean_pct_delta_pp: Optional[float]
    # ``{category: {"before": int, "after": int, "delta": int}}``
    skip_category_deltas: dict[str, dict[str, int]] = field(default_factory=dict)
    # ``{bucket: {"before_clean_pct", "after_clean_pct", "delta_pp",
    #             "before_receipts", "after_receipts"}}``
    by_evidence_type_delta: dict[str, dict[str, Any]] = field(default_factory=dict)
    per_packet: list[PacketComparison] = field(default_factory=list)
    transition_counts: dict[str, int] = field(default_factory=dict)


# ─── I/O ──────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict[str, Any]:
    """Read JSON or raise ``FileNotFoundError`` / ``ValueError``."""
    if not path.is_file():
        raise FileNotFoundError(f"missing JSON file: {path}")
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    """Read ``<run_dir>/manifest.json``."""
    return _load_json(run_dir / "manifest.json")


def _load_packet_artifacts(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Read every ``<run_dir>/packets/*.json``. Returns a dict keyed
    by ``packet_slug`` (the artifact carries it). Files that can't
    be parsed are skipped with a stderr warning, since partial
    coverage is better than no comparison.
    """
    out: dict[str, dict[str, Any]] = {}
    packets_dir = run_dir / "packets"
    if not packets_dir.is_dir():
        return out
    for f in sorted(packets_dir.glob("*.json")):
        try:
            data = _load_json(f)
        except (OSError, json.JSONDecodeError) as exc:
            import sys
            print(
                f"[compare_runs] WARN: skipping {f}: {exc}",
                file=sys.stderr,
            )
            continue
        slug = data.get("packet_slug") or f.stem
        out[slug] = data
    return out


def _extract_rows(packet_artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull per-receipt rows out of a packet artifact.

    A packet may carry multiple summaries (one per grader-mode); we
    flatten all rows together. Within a packet, ``question_id`` is
    expected to be unique across summaries — if a duplicate qid is
    encountered the LAST summary's row wins (matches the artifact
    write order).
    """
    rows_by_qid: dict[str, dict[str, Any]] = {}
    for summary in packet_artifact.get("summaries", []) or []:
        if not isinstance(summary, dict):
            continue
        artifact = summary.get("artifact") or {}
        rows = artifact.get("rows") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qid = row.get("question_id") or row.get("qid")
            if not qid:
                continue
            rows_by_qid[str(qid)] = row
    return list(rows_by_qid.values())


# ─── Classification ───────────────────────────────────────────────────


def _row_score_status(row: Optional[dict[str, Any]]) -> str:
    """Return ``score_status`` with the documented default. Pre-PR-#14
    rows lack the field — treat them as ``counted`` so a legacy
    baseline still compares against a modern one cleanly.
    """
    if not row:
        return "counted"
    raw = row.get("score_status")
    if raw is None or raw == "":
        return "counted"
    return str(raw)


def _row_grade(row: Optional[dict[str, Any]]) -> Optional[str]:
    if not row:
        return None
    return row.get("grade")


def _row_evidence_type(row: Optional[dict[str, Any]]) -> Optional[str]:
    if not row:
        return None
    return row.get("evidence_type")


def _row_clean_excluded_reason(row: Optional[dict[str, Any]]) -> Optional[str]:
    if not row:
        return None
    return row.get("clean_excluded_reason")


def classify_transition(
    before: Optional[dict[str, Any]],
    after: Optional[dict[str, Any]],
) -> str:
    """Classify the per-receipt before→after transition.

    Returns one of the ``TRANSITION_*`` constants. Treats absent
    ``score_status`` as ``counted`` (legacy back-compat).
    """
    if before is None and after is None:
        # Defensive: classify_transition shouldn't be called with both
        # None. Treat as no transition (still skipped is closest).
        return TRANSITION_STILL_SKIPPED
    if before is None:
        return TRANSITION_APPEARED
    if after is None:
        return TRANSITION_DISAPPEARED

    before_status = _row_score_status(before)
    after_status = _row_score_status(after)
    before_counted = before_status == "counted"
    after_counted = after_status == "counted"

    before_grade = _row_grade(before)
    after_grade = _row_grade(after)
    before_pass = before_counted and (before_grade in PASS_GRADES)
    after_pass = after_counted and (after_grade in PASS_GRADES)

    if not before_counted and not after_counted:
        return TRANSITION_STILL_SKIPPED
    if before_counted and not after_counted:
        return TRANSITION_NEWLY_SKIPPED
    if not before_counted and after_counted:
        return (
            TRANSITION_UN_SKIPPED_PASSING if after_pass
            else TRANSITION_UN_SKIPPED_FAILING
        )

    # Both counted.
    if before_pass and after_pass:
        return TRANSITION_STILL_PASSING
    if not before_pass and not after_pass:
        return TRANSITION_STILL_FAILING
    if not before_pass and after_pass:
        return TRANSITION_NEWLY_PASSING
    # before_pass and not after_pass
    return TRANSITION_NEWLY_FAILING


# ─── Aggregation ──────────────────────────────────────────────────────


def _empty_transition_counts() -> dict[str, int]:
    """Zero-filled counts across every transition bucket so consumers
    can index without KeyError."""
    return {t: 0 for t in TRANSITION_RENDER_ORDER}


def _compare_packet(
    slug: str,
    before_artifact: Optional[dict[str, Any]],
    after_artifact: Optional[dict[str, Any]],
    before_packet_summary: dict[str, Any],
    after_packet_summary: dict[str, Any],
) -> PacketComparison:
    """Build the per-packet comparison."""
    before_rows = (
        {r.get("question_id"): r for r in _extract_rows(before_artifact)}
        if before_artifact else {}
    )
    after_rows = (
        {r.get("question_id"): r for r in _extract_rows(after_artifact)}
        if after_artifact else {}
    )
    all_qids = sorted(set(before_rows) | set(after_rows))

    transitions: list[ReceiptTransition] = []
    counts = _empty_transition_counts()
    for qid in all_qids:
        if not qid:
            continue
        b = before_rows.get(qid)
        a = after_rows.get(qid)
        t = classify_transition(b, a)
        counts[t] += 1
        transitions.append(ReceiptTransition(
            packet_slug=slug,
            question_id=str(qid),
            transition=t,
            before_grade=_row_grade(b),
            after_grade=_row_grade(a),
            before_score_status=_row_score_status(b) if b is not None else None,
            after_score_status=_row_score_status(a) if a is not None else None,
            before_evidence_type=_row_evidence_type(b),
            after_evidence_type=_row_evidence_type(a),
            before_clean_excluded_reason=_row_clean_excluded_reason(b),
            after_clean_excluded_reason=_row_clean_excluded_reason(a),
        ))

    before_clean = before_packet_summary.get("clean_pct")
    after_clean = after_packet_summary.get("clean_pct")
    delta = None
    if isinstance(before_clean, (int, float)) and isinstance(after_clean, (int, float)):
        delta = round(float(after_clean) - float(before_clean), 1)

    return PacketComparison(
        packet_slug=slug,
        before_receipts=int(before_packet_summary.get("receipts", 0) or 0),
        after_receipts=int(after_packet_summary.get("receipts", 0) or 0),
        before_clean_pct=before_clean,
        after_clean_pct=after_clean,
        clean_pct_delta_pp=delta,
        transitions=transitions,
        transition_counts=counts,
    )


def _by_evidence_type_delta(
    before_breakdown: Optional[dict[str, Any]],
    after_breakdown: Optional[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compute per-bucket clean-pct delta. Returns empty dict if either
    side lacks the breakdown (pre-PR-evidence-breakdown manifest).
    """
    if not isinstance(before_breakdown, dict) or not isinstance(after_breakdown, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for bucket in EVIDENCE_TYPE_BUCKETS:
        b = before_breakdown.get(bucket) or {}
        a = after_breakdown.get(bucket) or {}
        b_cp = b.get("clean_pct")
        a_cp = a.get("clean_pct")
        delta = None
        if isinstance(b_cp, (int, float)) and isinstance(a_cp, (int, float)):
            delta = round(float(a_cp) - float(b_cp), 1)
        out[bucket] = {
            "before_receipts": int(b.get("receipts", 0) or 0),
            "after_receipts": int(a.get("receipts", 0) or 0),
            "before_clean_total": int(b.get("clean_total", 0) or 0),
            "after_clean_total": int(a.get("clean_total", 0) or 0),
            "before_clean_pct": b_cp,
            "after_clean_pct": a_cp,
            "delta_pp": delta,
        }
    return out


def _skip_category_deltas(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for key in SKIP_CATEGORIES:
        b = int(before.get(key, 0) or 0)
        a = int(after.get(key, 0) or 0)
        out[key] = {"before": b, "after": a, "delta": a - b}
    return out


def compare_runs(before_dir: Path, after_dir: Path) -> RunComparison:
    """Top-level entry point. Read both runs and return a
    ``RunComparison``."""
    before_manifest = _load_manifest(before_dir)
    after_manifest = _load_manifest(after_dir)

    before_packets = _load_packet_artifacts(before_dir)
    after_packets = _load_packet_artifacts(after_dir)

    before_per_packet = before_manifest.get("per_packet_pct", {}) or {}
    after_per_packet = after_manifest.get("per_packet_pct", {}) or {}
    all_packets = sorted(set(before_per_packet) | set(after_per_packet))

    per_packet: list[PacketComparison] = []
    run_transition_counts = _empty_transition_counts()
    for slug in all_packets:
        cmp = _compare_packet(
            slug,
            before_packets.get(slug),
            after_packets.get(slug),
            before_per_packet.get(slug, {}) or {},
            after_per_packet.get(slug, {}) or {},
        )
        per_packet.append(cmp)
        for t, n in cmp.transition_counts.items():
            run_transition_counts[t] = run_transition_counts.get(t, 0) + n

    before_raw = float(before_manifest.get("overall_pct", 0.0) or 0.0)
    after_raw = float(after_manifest.get("overall_pct", 0.0) or 0.0)
    before_clean = before_manifest.get("clean_overall_pct")
    after_clean = after_manifest.get("clean_overall_pct")
    clean_delta = None
    if isinstance(before_clean, (int, float)) and isinstance(after_clean, (int, float)):
        clean_delta = round(float(after_clean) - float(before_clean), 1)

    return RunComparison(
        before_run_name=str(before_manifest.get("run_name") or before_dir.name),
        after_run_name=str(after_manifest.get("run_name") or after_dir.name),
        before_raw_pct=before_raw,
        after_raw_pct=after_raw,
        raw_pct_delta_pp=round(after_raw - before_raw, 1),
        before_clean_pct=before_clean,
        after_clean_pct=after_clean,
        clean_pct_delta_pp=clean_delta,
        skip_category_deltas=_skip_category_deltas(
            before_manifest, after_manifest,
        ),
        by_evidence_type_delta=_by_evidence_type_delta(
            before_manifest.get("total_by_evidence_type"),
            after_manifest.get("total_by_evidence_type"),
        ),
        per_packet=per_packet,
        transition_counts=run_transition_counts,
    )


# ─── Renderers ────────────────────────────────────────────────────────


def _fmt_pct(v: Optional[float]) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"{float(v):.1f}%"


def _fmt_delta_pp(v: Optional[float]) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    arrow = "↑" if v > 0 else ("↓" if v < 0 else "→")
    return f"{float(v):+.1f}pp {arrow}"


_TRANSITION_DESCRIPTIONS: dict[str, str] = {
    TRANSITION_NEWLY_PASSING:
        "Atlas retrieval improved — was counted-fail, now counted-pass.",
    TRANSITION_UN_SKIPPED_PASSING:
        "Was excluded (skipped_*), now counted and passing — typically a "
        "receipt-staleness fix or alias resolution that rescued a real pass.",
    TRANSITION_NEWLY_SKIPPED:
        "Was counted, now excluded — denominator-cleaning. Inspect the "
        "after-side score_status to confirm intent.",
    TRANSITION_STILL_PASSING:
        "Stable wins.",
    TRANSITION_STILL_FAILING:
        "Persistent atlas misses. These are the real tuning targets.",
    TRANSITION_STILL_SKIPPED:
        "Stayed out of the clean denominator both runs.",
    TRANSITION_UN_SKIPPED_FAILING:
        "Was excluded, now counted-fail — skip rule loosened OR upstream "
        "resolved the staleness but atlas still misses. Worth investigating.",
    TRANSITION_NEWLY_FAILING:
        "REGRESSION — was counted-pass, now counted-fail.",
    TRANSITION_APPEARED:
        "Receipt is new in the after-run (added since the before-run).",
    TRANSITION_DISAPPEARED:
        "Receipt was in the before-run but is not in the after-run.",
}


def render_markdown(comparison: RunComparison) -> str:
    """Render a human-readable Markdown report."""
    lines: list[str] = ["# Atlas shadow-mode — probe comparison", ""]
    lines.append(f"**Before:** `{comparison.before_run_name}`")
    lines.append(f"**After:**  `{comparison.after_run_name}`")
    lines.append("")
    lines.append("## Run-level deltas")
    lines.append("")
    lines.append("| Metric | Before | After | Δ |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| Raw % | {comparison.before_raw_pct:.1f}% | "
        f"{comparison.after_raw_pct:.1f}% | "
        f"{_fmt_delta_pp(comparison.raw_pct_delta_pp)} |"
    )
    lines.append(
        f"| Clean % | {_fmt_pct(comparison.before_clean_pct)} | "
        f"{_fmt_pct(comparison.after_clean_pct)} | "
        f"{_fmt_delta_pp(comparison.clean_pct_delta_pp)} |"
    )
    lines.append("")

    # Skip-category shifts.
    lines.append("## Skip-category shifts")
    lines.append("")
    lines.append("| Category | Before | After | Δ |")
    lines.append("|---|---|---|---|")
    for cat in SKIP_CATEGORIES:
        d = comparison.skip_category_deltas.get(cat, {})
        b = d.get("before", 0)
        a = d.get("after", 0)
        delta = d.get("delta", 0)
        if b == 0 and a == 0:
            continue  # suppress zero rows
        sign = "+" if delta > 0 else ""
        # Strip the redundant "total_" prefix in the label.
        label = cat.removeprefix("total_")
        lines.append(f"| `{label}` | {b} | {a} | {sign}{delta} |")
    lines.append("")

    # By-evidence-type delta.
    if comparison.by_evidence_type_delta:
        lines.append("## Clean % by evidence type")
        lines.append("")
        lines.append(
            "| Evidence type | Before clean % | After clean % | Δ | "
            "Receipts (B/A) |"
        )
        lines.append("|---|---|---|---|---|")
        for bucket in EVIDENCE_TYPE_BUCKETS:
            row = comparison.by_evidence_type_delta.get(bucket, {})
            br = int(row.get("before_receipts", 0) or 0)
            ar = int(row.get("after_receipts", 0) or 0)
            if br == 0 and ar == 0:
                continue
            lines.append(
                f"| `{bucket}` | "
                f"{_fmt_pct(row.get('before_clean_pct'))} | "
                f"{_fmt_pct(row.get('after_clean_pct'))} | "
                f"{_fmt_delta_pp(row.get('delta_pp'))} | "
                f"{br} / {ar} |"
            )
        lines.append("")

    # Run-level transitions.
    lines.append("## Receipt transitions (run-level)")
    lines.append("")
    lines.append("| Transition | Count | Meaning |")
    lines.append("|---|---|---|")
    for t in TRANSITION_RENDER_ORDER:
        n = comparison.transition_counts.get(t, 0)
        if n == 0:
            continue
        lines.append(
            f"| `{t}` | {n} | {_TRANSITION_DESCRIPTIONS[t]} |"
        )
    lines.append("")

    # Per-packet detail. Sort by absolute clean-pct delta (largest
    # movers first) so the most informative packets are at the top.
    sorted_packets = sorted(
        comparison.per_packet,
        key=lambda p: abs(p.clean_pct_delta_pp or 0.0),
        reverse=True,
    )
    lines.append("## Per-packet detail")
    lines.append("")
    for p in sorted_packets:
        bp = _fmt_pct(p.before_clean_pct)
        ap = _fmt_pct(p.after_clean_pct)
        dp = _fmt_delta_pp(p.clean_pct_delta_pp)
        lines.append(
            f"### `{p.packet_slug}` — clean {bp} → {ap} ({dp})"
        )
        lines.append("")
        lines.append(
            f"_Receipts: before={p.before_receipts}, after={p.after_receipts}_"
        )
        lines.append("")
        # Surface the "informative" transitions inline: newly_passing,
        # newly_failing, newly_skipped, un_skipped_*. Suppress
        # still_* / appeared / disappeared inline — they're in the
        # JSON if needed.
        informative = [
            TRANSITION_NEWLY_PASSING,
            TRANSITION_UN_SKIPPED_PASSING,
            TRANSITION_NEWLY_SKIPPED,
            TRANSITION_UN_SKIPPED_FAILING,
            TRANSITION_NEWLY_FAILING,
        ]
        any_inline = False
        for t in informative:
            matches = [r for r in p.transitions if r.transition == t]
            if not matches:
                continue
            any_inline = True
            lines.append(f"**{t}** ({len(matches)}):")
            for r in matches:
                bits = []
                if r.before_grade or r.before_score_status:
                    bits.append(
                        f"before: grade={r.before_grade or '?'}, "
                        f"status={r.before_score_status or '?'}"
                    )
                if r.after_grade or r.after_score_status:
                    bits.append(
                        f"after: grade={r.after_grade or '?'}, "
                        f"status={r.after_score_status or '?'}"
                    )
                lines.append(f"- `{r.question_id}` — {' → '.join(bits)}")
            lines.append("")
        if not any_inline:
            # Show the count summary so an "all stable" packet isn't
            # rendered as a totally empty section.
            counts_str = ", ".join(
                f"{t}={n}" for t, n in p.transition_counts.items() if n > 0
            )
            lines.append(f"_No notable transitions. Counts: {counts_str or 'empty'}._")
            lines.append("")

    return "\n".join(lines)


def render_json(comparison: RunComparison) -> str:
    """Render the comparison as a JSON payload."""
    payload = {
        "before_run_name": comparison.before_run_name,
        "after_run_name": comparison.after_run_name,
        "before_raw_pct": comparison.before_raw_pct,
        "after_raw_pct": comparison.after_raw_pct,
        "raw_pct_delta_pp": comparison.raw_pct_delta_pp,
        "before_clean_pct": comparison.before_clean_pct,
        "after_clean_pct": comparison.after_clean_pct,
        "clean_pct_delta_pp": comparison.clean_pct_delta_pp,
        "skip_category_deltas": comparison.skip_category_deltas,
        "by_evidence_type_delta": comparison.by_evidence_type_delta,
        "transition_counts": comparison.transition_counts,
        "per_packet": [
            {
                "packet_slug": p.packet_slug,
                "before_receipts": p.before_receipts,
                "after_receipts": p.after_receipts,
                "before_clean_pct": p.before_clean_pct,
                "after_clean_pct": p.after_clean_pct,
                "clean_pct_delta_pp": p.clean_pct_delta_pp,
                "transition_counts": p.transition_counts,
                "transitions": [
                    {
                        "question_id": r.question_id,
                        "transition": r.transition,
                        "before_grade": r.before_grade,
                        "after_grade": r.after_grade,
                        "before_score_status": r.before_score_status,
                        "after_score_status": r.after_score_status,
                        "before_evidence_type": r.before_evidence_type,
                        "after_evidence_type": r.after_evidence_type,
                        "before_clean_excluded_reason":
                            r.before_clean_excluded_reason,
                        "after_clean_excluded_reason":
                            r.after_clean_excluded_reason,
                    }
                    for r in p.transitions
                ],
            }
            for p in comparison.per_packet
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def write_reports(
    comparison: RunComparison,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write both ``comparison-report.md`` and ``comparison-report.json``
    under ``output_dir``. Returns the two paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "comparison-report.md"
    json_path = output_dir / "comparison-report.json"
    md_path.write_text(render_markdown(comparison), encoding="utf-8")
    json_path.write_text(render_json(comparison), encoding="utf-8")
    return md_path, json_path
