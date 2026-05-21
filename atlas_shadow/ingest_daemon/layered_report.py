"""layered_report — Phase 1 pilot for workflow-level shadow scoring.

This module intentionally sits beside the existing Atlas-evidence grader
rather than replacing it.  It reads:

* one packet JSON emitted by ``grade-packet-batch``; and
* one typed oracle spec authored for that packet.

It then renders the layered scorecard Ray asked for:

* Evidence Oracle coverage
* Benchmark Confidence
* Planner Evidence
* Planner Synthesis
* Atlas Evidence
* Atlas Synthesis
* Cost

The first version is a pilot, not a universal schema migration.  The
contract is deliberately small and path-based so a single real packet can
prove whether synthesis oracles are useful before we generalize them.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


PASS_GRADES = frozenset({"full_match", "partial_match"})


@dataclass(frozen=True)
class OracleRow:
    qid: str
    oracle_bucket: str
    oracle_status: str
    planner_evidence_status: str
    claim_type: str = "legacy_prose"
    evidence_type: str = "source_excerpt"
    synthesis_role: str = "context_only"
    oracle_failure_type: Optional[str] = None
    planner_failure_type: Optional[str] = None
    required_point_ids: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class SynthesisSubcriterion:
    id: str
    text: str
    points: float
    supporting_qids: tuple[str, ...]
    required_point_id: Optional[str]
    planner_status: str
    atlas_status: str
    scoreable: Optional[str] = None
    planner_miss_class: Optional[str] = None
    atlas_miss_class: Optional[str] = None


@dataclass(frozen=True)
class SynthesisCriterion:
    id: str
    label: str
    points_possible: float
    planner_points: float
    atlas_points: float
    notes: str = ""
    planner_miss_class: Optional[str] = None
    atlas_miss_class: Optional[str] = None
    subcriteria: tuple[SynthesisSubcriterion, ...] = ()


@dataclass(frozen=True)
class LayeredSpec:
    packet_id: str
    title: str
    evidence_rows: tuple[OracleRow, ...]
    ideal_conclusion: str
    required_points: tuple[dict[str, Any], ...]
    forbidden_claims: tuple[dict[str, Any], ...]
    uncertainty_notes: tuple[dict[str, Any], ...]
    synthesis_criteria: tuple[SynthesisCriterion, ...]
    synthesis_status: str = "hand_authored"
    synthesis_score_source: str = "authored_static"
    cost: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LayeredReport:
    packet_id: str
    title: str
    oracle_verified: int
    oracle_unresolved: int
    context_verified: int
    command_verified: int
    benchmark_confidence: str
    planner_evidence_pass: int
    planner_evidence_total: int
    atlas_evidence_pass: int
    atlas_evidence_total: int
    planner_synthesis_points: float
    atlas_synthesis_points: float
    synthesis_points_possible: float
    failure_counts: dict[str, dict[str, int]]
    cost: dict[str, Any]
    rows: list[dict[str, Any]]
    synthesis_oracle: dict[str, Any]
    synthesis_warnings: list[dict[str, Any]]
    synthesis_score_source: str
    synthesis_required_points_total: int
    synthesis_supported_required_points: int

    @property
    def planner_evidence_pct(self) -> Optional[float]:
        return _pct(self.planner_evidence_pass, self.planner_evidence_total)

    @property
    def atlas_evidence_pct(self) -> Optional[float]:
        return _pct(self.atlas_evidence_pass, self.atlas_evidence_total)

    @property
    def planner_synthesis_pct(self) -> Optional[float]:
        return _pct(self.planner_synthesis_points, self.synthesis_points_possible)

    @property
    def atlas_synthesis_pct(self) -> Optional[float]:
        return _pct(self.atlas_synthesis_points, self.synthesis_points_possible)

    @property
    def synthesis_readiness_pct(self) -> Optional[float]:
        return _pct(
            self.synthesis_supported_required_points,
            self.synthesis_required_points_total,
        )


def load_spec(path: Path) -> LayeredSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"layered spec must be a mapping: {path}")

    rows = []
    for item in raw.get("evidence_oracle", {}).get("rows", []):
        rows.append(
            OracleRow(
                qid=str(item["qid"]),
                oracle_bucket=str(item.get("oracle_bucket", "evidence")),
                oracle_status=str(item.get("oracle_status", "unresolved")),
                planner_evidence_status=str(
                    item.get("planner_evidence_status", "not_scored")
                ),
                claim_type=str(item.get("claim_type", "legacy_prose")),
                evidence_type=str(item.get("evidence_type", "source_excerpt")),
                synthesis_role=str(item.get("synthesis_role", "context_only")),
                oracle_failure_type=_optional_str(item.get("oracle_failure_type")),
                planner_failure_type=_optional_str(item.get("planner_failure_type")),
                required_point_ids=tuple(
                    str(v) for v in item.get("required_point_ids", []) or []
                ),
                notes=str(item.get("notes", "")),
            )
        )

    synth = raw.get("synthesis_oracle") or {}
    criteria = []
    for item in synth.get("criteria", []):
        subcriteria = []
        for sub in item.get("subcriteria", []) or []:
            subcriteria.append(
                SynthesisSubcriterion(
                    id=str(sub["id"]),
                    text=str(sub.get("text", "")),
                    points=float(sub.get("points", 0)),
                    supporting_qids=tuple(
                        str(v) for v in sub.get("supporting_qids", []) or []
                    ),
                    required_point_id=_optional_str(sub.get("required_point_id")),
                    planner_status=str(sub.get("planner_status", "not_scored")),
                    atlas_status=str(sub.get("atlas_status", "not_scored")),
                    scoreable=_optional_str(sub.get("scoreable")),
                    planner_miss_class=_optional_str(sub.get("planner_miss_class")),
                    atlas_miss_class=_optional_str(sub.get("atlas_miss_class")),
                )
            )
        criteria.append(
            SynthesisCriterion(
                id=str(item["id"]),
                label=str(item["label"]),
                points_possible=float(item.get("points_possible", 0)),
                planner_points=float(item.get("planner_points", 0)),
                atlas_points=float(item.get("atlas_points", 0)),
                notes=str(item.get("notes", "")),
                planner_miss_class=_optional_str(item.get("planner_miss_class")),
                atlas_miss_class=_optional_str(item.get("atlas_miss_class")),
                subcriteria=tuple(subcriteria),
            )
        )

    return LayeredSpec(
        packet_id=str(raw["packet_id"]),
        title=str(raw.get("title") or raw["packet_id"]),
        evidence_rows=tuple(rows),
        ideal_conclusion=str(synth.get("ideal_conclusion", "")),
        required_points=tuple(synth.get("required_points", []) or []),
        forbidden_claims=tuple(synth.get("forbidden_claims", []) or []),
        uncertainty_notes=tuple(synth.get("uncertainty_notes", []) or []),
        synthesis_criteria=tuple(criteria),
        synthesis_status=str(synth.get("status") or "hand_authored"),
        synthesis_score_source=str(
            synth.get("score_source") or "authored_static"
        ),
        cost=dict(raw.get("cost") or {}),
    )


def build_report(*, spec_path: Path, packet_json_path: Path) -> LayeredReport:
    spec = load_spec(spec_path)
    packet = json.loads(packet_json_path.read_text(encoding="utf-8"))
    runtime_rows = _runtime_rows_by_qid(packet)

    oracle_verified = 0
    oracle_unresolved = 0
    context_verified = 0
    command_verified = 0
    planner_pass = 0
    planner_total = 0
    atlas_pass = 0
    atlas_total = 0
    failures: dict[str, Counter[str]] = {
        "oracle": Counter(),
        "planner_evidence": Counter(),
        "atlas_evidence": Counter(),
        "synthesis": Counter(),
    }
    detail_rows: list[dict[str, Any]] = []

    for row in spec.evidence_rows:
        runtime = runtime_rows.get(row.qid, {})
        oracle_ok = row.oracle_status == "verified"
        evidence_row = row.oracle_bucket == "evidence"
        context_row = row.oracle_bucket == "context"
        command_row = row.oracle_bucket == "command"
        planner_ok = row.planner_evidence_status == "pass"
        atlas_grade = str(runtime.get("grade") or "not_run")
        atlas_ok = atlas_grade in PASS_GRADES

        if oracle_ok:
            if evidence_row:
                oracle_verified += 1
                planner_total += 1
                atlas_total += 1
                if planner_ok:
                    planner_pass += 1
            elif context_row:
                context_verified += 1
            elif command_row:
                command_verified += 1
        else:
            oracle_unresolved += 1
            failures["oracle"][row.oracle_failure_type or row.oracle_status] += 1

        if evidence_row and oracle_ok and row.planner_evidence_status == "fail":
            failures["planner_evidence"][
                row.planner_failure_type or "planner_evidence_failed"
            ] += 1

        atlas_result = "not_scored"
        atlas_failure_type = None
        runtime_score_status = runtime.get("score_status")
        if evidence_row and oracle_ok and runtime_score_status and runtime_score_status != "counted":
            atlas_result = "fail"
            reason = str(
                runtime.get("clean_excluded_reason")
                or runtime_score_status
                or "runtime_excluded"
            )
            atlas_failure_type = reason
            failures["atlas_evidence"][reason] += 1
        elif evidence_row and oracle_ok:
            if atlas_ok:
                atlas_pass += 1
                atlas_result = "pass"
            else:
                atlas_result = "fail"
                atlas_failure_type = _atlas_failure_type(runtime)
                failures["atlas_evidence"][atlas_failure_type] += 1

        detail_rows.append(
            {
                "qid": row.qid,
                "oracle_bucket": row.oracle_bucket,
                "claim_type": row.claim_type,
                "synthesis_role": row.synthesis_role,
                "oracle_status": row.oracle_status,
                "planner_evidence_status": row.planner_evidence_status,
                "planner_failure_type": row.planner_failure_type,
                "atlas_result": atlas_result,
                "atlas_grade": atlas_grade,
                "atlas_failure_type": atlas_failure_type,
                "score_status": runtime.get("score_status"),
                "clean_excluded_reason": runtime.get("clean_excluded_reason"),
                "lane": runtime.get("lane"),
                "notes": row.notes,
            }
        )

    points_possible = sum(c.points_possible for c in spec.synthesis_criteria)
    planner_points = sum(c.planner_points for c in spec.synthesis_criteria)
    atlas_points = sum(c.atlas_points for c in spec.synthesis_criteria)
    for criterion in spec.synthesis_criteria:
        if criterion.planner_points < criterion.points_possible:
            failures["synthesis"][
                f"planner:{criterion.planner_miss_class or 'unclassified'}"
            ] += 1
        if criterion.atlas_points < criterion.points_possible:
            failures["synthesis"][
                f"atlas:{criterion.atlas_miss_class or 'unclassified'}"
            ] += 1

    synthesis_warnings = _synthesis_support_warnings(spec)
    required_points_total = _required_points_total(spec)

    return LayeredReport(
        packet_id=spec.packet_id,
        title=spec.title,
        oracle_verified=oracle_verified,
        oracle_unresolved=oracle_unresolved,
        context_verified=context_verified,
        command_verified=command_verified,
        benchmark_confidence=_benchmark_confidence(
            oracle_verified + context_verified + command_verified,
            oracle_unresolved,
        ),
        planner_evidence_pass=planner_pass,
        planner_evidence_total=planner_total,
        atlas_evidence_pass=atlas_pass,
        atlas_evidence_total=atlas_total,
        planner_synthesis_points=planner_points,
        atlas_synthesis_points=atlas_points,
        synthesis_points_possible=points_possible,
        failure_counts={
            layer: dict(counter)
            for layer, counter in failures.items()
            if counter
        },
        cost=spec.cost,
        rows=detail_rows,
        synthesis_oracle={
            "ideal_conclusion": spec.ideal_conclusion,
            "required_points": list(spec.required_points),
            "forbidden_claims": list(spec.forbidden_claims),
            "uncertainty_notes": list(spec.uncertainty_notes),
            "criteria": [
                {
                    "id": c.id,
                    "label": c.label,
                    "points_possible": c.points_possible,
                    "planner_points": c.planner_points,
                    "atlas_points": c.atlas_points,
                    "notes": c.notes,
                    "planner_miss_class": c.planner_miss_class,
                    "atlas_miss_class": c.atlas_miss_class,
                    "subcriteria": [
                        {
                            "id": s.id,
                            "text": s.text,
                            "points": s.points,
                            "supporting_qids": list(s.supporting_qids),
                            "required_point_id": s.required_point_id,
                            "planner_status": s.planner_status,
                            "atlas_status": s.atlas_status,
                            "scoreable": s.scoreable,
                            "planner_miss_class": s.planner_miss_class,
                            "atlas_miss_class": s.atlas_miss_class,
                        }
                        for s in c.subcriteria
                    ],
                }
                for c in spec.synthesis_criteria
            ],
            "status": spec.synthesis_status,
            "score_source": spec.synthesis_score_source,
        },
        synthesis_warnings=synthesis_warnings,
        synthesis_score_source=spec.synthesis_score_source,
        synthesis_required_points_total=required_points_total,
        synthesis_supported_required_points=(
            required_points_total - len(synthesis_warnings)
        ),
    )


def write_reports(report: LayeredReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "layered-shadow-report.md"
    json_path = output_dir / "layered-shadow-report.json"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(to_json(report), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return md_path, json_path


def write_run_summary(reports: list[LayeredReport], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "layered-summary.md"
    json_path = output_dir / "layered-summary.json"
    md_path.write_text(render_run_summary_markdown(reports), encoding="utf-8")
    json_path.write_text(
        json.dumps(run_summary_json(reports), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return md_path, json_path


def write_synthesis_audit(
    reports: list[LayeredReport],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write a run-level audit of every synthesis miss.

    The audit is intentionally derived from the hand-authored synthesis
    criteria. If a criterion loses points, it must carry a miss class; otherwise
    the audit marks it ``unclassified`` so the packet owner can fix the rubric
    before treating the synthesis score as actionable.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    for report in sorted(reports, key=lambda item: item.packet_id):
        for warning in report.synthesis_warnings:
            rows.append({
                "packet_id": report.packet_id,
                **warning,
            })
        for criterion in report.synthesis_oracle.get("criteria", []):
            for row in _synthesis_miss_rows_for_criterion(
                packet_id=report.packet_id,
                criterion=criterion,
            ):
                rows.append(row)
                class_counts[str(row.get("miss_class") or "unclassified")] += 1

    miss_rows = [row for row in rows if row.get("actor")]
    payload = {
        "total_misses": len(miss_rows),
        "class_counts": dict(sorted(class_counts.items())),
        "score_sources": dict(
            sorted(Counter(r.synthesis_score_source for r in reports).items())
        ),
        "manual_review": [
            row for row in miss_rows if row.get("manual_review")
        ],
        "support_warnings": [
            row for row in rows if row.get("warning")
        ],
        "rows": miss_rows,
    }
    payload["repair_queue"] = _synthesis_repair_queue(payload["support_warnings"])
    json_path = output_dir / "synthesis-audit.json"
    md_path = output_dir / "synthesis-audit.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_synthesis_audit_markdown(payload), encoding="utf-8")
    return md_path, json_path


def render_synthesis_audit_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Synthesis Audit",
        "",
        f"- **Total synthesis misses:** {payload.get('total_misses', 0)}",
        f"- **Score source:** {_score_source_summary(payload.get('score_sources') or {})}",
        "",
        "## Miss Classes",
        "",
        "| Class | Count |",
        "|---|---:|",
    ]
    counts = payload.get("class_counts") or {}
    if counts:
        for miss_class, count in sorted(counts.items()):
            lines.append(f"| `{miss_class}` | {count} |")
    else:
        lines.append("| - | 0 |")

    lines.extend(
        [
            "",
            "## Manual Review",
            "",
            "| Packet | Actor | Criterion | Class | Notes |",
            "|---|---|---|---|---|",
        ]
    )
    manual_rows = payload.get("manual_review") or []
    if manual_rows:
        for row in manual_rows:
            lines.append(
                f"| `{row.get('packet_id')}` | {row.get('actor')} | "
                f"{row.get('criterion_id')} | `{row.get('miss_class')}` | "
                f"{_md_cell(row.get('notes') or '')} |"
            )
    else:
        lines.append("| - | - | - | - | None |")

    lines.extend(
        [
            "",
            "## Support Warnings",
            "",
            "| Packet | Required Point | Warning | Supporting Rows |",
            "|---|---|---|---|",
        ]
    )
    support_rows = payload.get("support_warnings") or []
    if support_rows:
        for row in support_rows:
            qids = ", ".join(row.get("qids") or []) or "-"
            lines.append(
                f"| `{row.get('packet_id')}` | {row.get('required_point_id')} | "
                f"`{row.get('warning')}` | {_md_cell(qids)} |"
            )
    else:
        lines.append("| - | - | - | None |")

    lines.extend(
        [
            "",
            "## Repair Queue",
            "",
            "| Packet | Required Point | Warning | Recommended Action |",
            "|---|---|---|---|",
        ]
    )
    repair_rows = payload.get("repair_queue") or []
    if repair_rows:
        for row in repair_rows:
            lines.append(
                f"| `{row.get('packet_id')}` | {row.get('required_point_id')} | "
                f"`{row.get('warning')}` | {_md_cell(row.get('recommended_action'))} |"
            )
    else:
        lines.append("| - | - | - | None |")

    lines.extend(
        [
            "",
            "## All Misses",
            "",
            "| Packet | Actor | Criterion | Points | Class | Notes |",
            "|---|---|---|---:|---|---|",
        ]
    )
    rows = payload.get("rows") or []
    if rows:
        for row in rows:
            points = _points(
                float(row.get("points") or 0),
                float(row.get("points_possible") or 0),
            )
            criterion = row.get("criterion_id")
            if row.get("subcriterion_id"):
                criterion = row.get("subcriterion_id")
            lines.append(
                f"| `{row.get('packet_id')}` | {row.get('actor')} | "
                f"{criterion} | {points} | "
                f"`{row.get('miss_class')}` | {_md_cell(row.get('notes') or '')} |"
            )
    else:
        lines.append("| - | - | - | - | - | No synthesis misses |")
    return "\n".join(lines) + "\n"


def _synthesis_miss_rows_for_criterion(
    *,
    packet_id: str,
    criterion: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    subcriteria = criterion.get("subcriteria") or []
    if isinstance(subcriteria, list) and subcriteria:
        for sub in subcriteria:
            if not isinstance(sub, dict):
                continue
            possible = float(sub.get("points") or 0)
            if possible <= 0:
                continue
            for actor in ("planner", "atlas"):
                status = str(sub.get(f"{actor}_status") or "not_scored")
                if status == "covered":
                    continue
                miss_class = str(
                    sub.get(f"{actor}_miss_class")
                    or criterion.get(f"{actor}_miss_class")
                    or "unclassified"
                )
                rows.append({
                    "packet_id": packet_id,
                    "actor": actor,
                    "criterion_id": criterion.get("id"),
                    "criterion_label": criterion.get("label"),
                    "subcriterion_id": sub.get("id"),
                    "subcriterion_text": sub.get("text") or "",
                    "supporting_qids": list(sub.get("supporting_qids") or []),
                    "required_point_id": sub.get("required_point_id"),
                    "points": 0.0,
                    "points_possible": possible,
                    "miss_class": miss_class,
                    "notes": sub.get("notes") or criterion.get("notes") or "",
                    "manual_review": miss_class == "answer_shape_without_evidence",
                })
        return rows

    possible = float(criterion.get("points_possible") or 0)
    if possible <= 0:
        return rows
    for actor in ("planner", "atlas"):
        points = float(criterion.get(f"{actor}_points") or 0)
        if points >= possible:
            continue
        miss_class = str(
            criterion.get(f"{actor}_miss_class")
            or "unclassified"
        )
        rows.append({
            "packet_id": packet_id,
            "actor": actor,
            "criterion_id": criterion.get("id"),
            "criterion_label": criterion.get("label"),
            "points": points,
            "points_possible": possible,
            "miss_class": miss_class,
            "notes": criterion.get("notes") or "",
            "manual_review": miss_class == "answer_shape_without_evidence",
        })
    return rows


def to_json(report: LayeredReport) -> dict[str, Any]:
    return {
        "packet_id": report.packet_id,
        "title": report.title,
        "oracle": {
            "verified": report.oracle_verified,
            "context_verified": report.context_verified,
            "command_verified": report.command_verified,
            "unresolved": report.oracle_unresolved,
            "benchmark_confidence": report.benchmark_confidence,
        },
        "planner_evidence": {
            "pass": report.planner_evidence_pass,
            "total": report.planner_evidence_total,
            "pct": report.planner_evidence_pct,
        },
        "planner_synthesis": {
            "points": report.planner_synthesis_points,
            "possible": report.synthesis_points_possible,
            "pct": report.planner_synthesis_pct,
        },
        "atlas_evidence": {
            "pass": report.atlas_evidence_pass,
            "total": report.atlas_evidence_total,
            "pct": report.atlas_evidence_pct,
        },
        "atlas_synthesis": {
            "points": report.atlas_synthesis_points,
            "possible": report.synthesis_points_possible,
            "pct": report.atlas_synthesis_pct,
        },
        "synthesis_readiness": {
            "supported_required_points": report.synthesis_supported_required_points,
            "required_points": report.synthesis_required_points_total,
            "pct": report.synthesis_readiness_pct,
        },
        "failure_counts": report.failure_counts,
        "cost": report.cost,
        "rows": report.rows,
        "synthesis_oracle": report.synthesis_oracle,
        "synthesis_warnings": report.synthesis_warnings,
        "synthesis_score_source": report.synthesis_score_source,
    }


def run_summary_json(reports: list[LayeredReport]) -> dict[str, Any]:
    totals = {
        "packets": len(reports),
        "oracle_verified": sum(r.oracle_verified for r in reports),
        "context_verified": sum(r.context_verified for r in reports),
        "command_verified": sum(r.command_verified for r in reports),
        "oracle_unresolved": sum(r.oracle_unresolved for r in reports),
        "planner_evidence_pass": sum(r.planner_evidence_pass for r in reports),
        "planner_evidence_total": sum(r.planner_evidence_total for r in reports),
        "atlas_evidence_pass": sum(r.atlas_evidence_pass for r in reports),
        "atlas_evidence_total": sum(r.atlas_evidence_total for r in reports),
        "planner_synthesis_points": sum(r.planner_synthesis_points for r in reports),
        "atlas_synthesis_points": sum(r.atlas_synthesis_points for r in reports),
        "synthesis_points_possible": sum(r.synthesis_points_possible for r in reports),
        "synthesis_score_sources": dict(
            sorted(Counter(r.synthesis_score_source for r in reports).items())
        ),
        "synthesis_support_warning_count": sum(
            len(r.synthesis_warnings) for r in reports
        ),
        "synthesis_supported_required_points": sum(
            r.synthesis_supported_required_points for r in reports
        ),
        "synthesis_required_points_total": sum(
            r.synthesis_required_points_total for r in reports
        ),
    }
    totals["planner_evidence_pct"] = _pct(
        totals["planner_evidence_pass"], totals["planner_evidence_total"]
    )
    totals["atlas_evidence_pct"] = _pct(
        totals["atlas_evidence_pass"], totals["atlas_evidence_total"]
    )
    totals["planner_synthesis_pct"] = _pct(
        totals["planner_synthesis_points"], totals["synthesis_points_possible"]
    )
    totals["atlas_synthesis_pct"] = _pct(
        totals["atlas_synthesis_points"], totals["synthesis_points_possible"]
    )
    totals["synthesis_readiness_pct"] = _pct(
        totals["synthesis_supported_required_points"],
        totals["synthesis_required_points_total"],
    )
    return {
        "totals": totals,
        "packets": [
            {
                "packet_id": r.packet_id,
                "benchmark_confidence": r.benchmark_confidence,
                "oracle_verified": r.oracle_verified,
                "context_verified": r.context_verified,
                "command_verified": r.command_verified,
                "oracle_unresolved": r.oracle_unresolved,
                "planner_evidence": {
                    "pass": r.planner_evidence_pass,
                    "total": r.planner_evidence_total,
                    "pct": r.planner_evidence_pct,
                },
                "planner_synthesis": {
                    "points": r.planner_synthesis_points,
                    "possible": r.synthesis_points_possible,
                    "pct": r.planner_synthesis_pct,
                },
                "atlas_evidence": {
                    "pass": r.atlas_evidence_pass,
                    "total": r.atlas_evidence_total,
                    "pct": r.atlas_evidence_pct,
                },
                "atlas_synthesis": {
                    "points": r.atlas_synthesis_points,
                    "possible": r.synthesis_points_possible,
                    "pct": r.atlas_synthesis_pct,
                },
                "synthesis_readiness": {
                    "supported_required_points": r.synthesis_supported_required_points,
                    "required_points": r.synthesis_required_points_total,
                    "pct": r.synthesis_readiness_pct,
                },
                "failure_counts": r.failure_counts,
                "synthesis_warnings": r.synthesis_warnings,
                "synthesis_score_source": r.synthesis_score_source,
            }
            for r in sorted(reports, key=lambda item: item.packet_id)
        ],
    }


def render_run_summary_markdown(reports: list[LayeredReport]) -> str:
    payload = run_summary_json(reports)
    totals = payload["totals"]
    lines = [
        "# Layered Shadow Run Summary",
        "",
        "## Aggregate",
        "",
        "| Dimension | Score |",
        "|---|---:|",
        (
            "| Oracle Coverage | "
            f"{totals['oracle_verified']} evidence + "
            f"{totals['context_verified']} context + "
            f"{totals['command_verified']} command + "
            f"{totals['oracle_unresolved']} unresolved |"
        ),
        (
            "| Planner Evidence | "
            f"{_ratio(totals['planner_evidence_pass'], totals['planner_evidence_total'])} |"
        ),
        (
            "| Planner Synthesis | "
            f"{_points(totals['planner_synthesis_points'], totals['synthesis_points_possible'])} |"
        ),
        (
            "| Atlas Evidence | "
            f"{_ratio(totals['atlas_evidence_pass'], totals['atlas_evidence_total'])} |"
        ),
        (
            "| Atlas Synthesis | "
            f"{_points(totals['atlas_synthesis_points'], totals['synthesis_points_possible'])} |"
        ),
        (
            "| Synthesis Readiness | "
            f"{_ratio(totals['synthesis_supported_required_points'], totals['synthesis_required_points_total'])} |"
        ),
        "",
        "## Per Packet",
        "",
        "| Packet | Oracle Coverage | Confidence | Planner Evidence | Planner Synthesis | Atlas Evidence | Atlas Synthesis | Synthesis Readiness |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for report in sorted(reports, key=lambda item: item.packet_id):
        lines.append(
            f"| `{report.packet_id}` | "
            f"{report.oracle_verified} evidence + {report.context_verified} context + "
            f"{report.command_verified} command + {report.oracle_unresolved} unresolved | "
            f"{report.benchmark_confidence} | "
            f"{_ratio(report.planner_evidence_pass, report.planner_evidence_total)} | "
            f"{_points(report.planner_synthesis_points, report.synthesis_points_possible)} | "
            f"{_ratio(report.atlas_evidence_pass, report.atlas_evidence_total)} | "
            f"{_points(report.atlas_synthesis_points, report.synthesis_points_possible)} | "
            f"{_ratio(report.synthesis_supported_required_points, report.synthesis_required_points_total)} |"
        )
    lines.extend(["", "## Notes", ""])
    lines.append(
        "Synthesis points are currently "
        f"{_score_source_summary(totals.get('synthesis_score_sources') or {})}. "
        "Treat `authored_static` synthesis scores as rubric-pilot measurements, "
        "not runtime-computed model grades."
    )
    lines.append("")
    lines.append(
        f"Synthesis support warnings: "
        f"{totals.get('synthesis_support_warning_count', 0)}."
    )
    lines.append(
        "Synthesis readiness counts required points with verified evidence support. "
        "It does not change authored synthesis scores; it tells you how much of "
        "the rubric is safe to treat as an actionable scoring surface."
    )
    lines.append("")
    lines.append(
        "Historical sidecars with draft synthesis criteria show `0/0 (n/a)` for synthesis; "
        "that is intentional until a packet owner authors the synthesis oracle."
    )
    planner_pct = totals["planner_evidence_pct"]
    atlas_pct = totals["atlas_evidence_pct"]
    if (
        planner_pct is not None
        and atlas_pct is not None
        and atlas_pct > planner_pct
    ):
        lines.append("")
        lines.append(
            "Atlas Evidence is higher than Planner Evidence on this run. Treat "
            "that gap as an attribution signal, not a simple upper-bound claim: "
            "inspect Planner invalid evidence rows and the run's revision pinning "
            "mode before deciding whether the gap reflects authoring drift, "
            "receipt defects, or true retrieval quality."
        )
    return "\n".join(lines) + "\n"


def render_markdown(report: LayeredReport) -> str:
    lines = [
        f"# Layered Shadow Report - {report.packet_id}",
        "",
        "## Headline",
        "",
        "| Dimension | Score | Confidence | Notes |",
        "|---|---:|---|---|",
        (
            f"| Oracle Coverage | {report.oracle_verified} verified + "
            f"{report.context_verified} context + {report.command_verified} command + "
            f"{report.oracle_unresolved} unresolved | "
            f"{report.benchmark_confidence} | Evidence rows define the shared Planner/Atlas denominator |"
        ),
        (
            f"| Command/Context Oracle | {report.command_verified} command + "
            f"{report.context_verified} context | "
            f"{report.benchmark_confidence} | Verified non-retrieval substrate for synthesis |"
        ),
        (
            f"| Planner Evidence | {_ratio(report.planner_evidence_pass, report.planner_evidence_total)} | "
            f"{report.benchmark_confidence} | Planner receipts against verified oracle rows |"
        ),
        (
            f"| Planner Synthesis | {_points(report.planner_synthesis_points, report.synthesis_points_possible)} | "
            f"{report.benchmark_confidence} | {report.synthesis_score_source}; planner answer against synthesis oracle |"
        ),
        (
            f"| Atlas Evidence | {_ratio(report.atlas_evidence_pass, report.atlas_evidence_total)} "
            f"| {report.benchmark_confidence} | Atlas retrieval against the same verified evidence rows |"
        ),
        (
            f"| Atlas Synthesis | {_points(report.atlas_synthesis_points, report.synthesis_points_possible)} | "
            f"{report.benchmark_confidence} | {report.synthesis_score_source}; Atlas answer against synthesis oracle |"
        ),
        (
            f"| Synthesis Readiness | {_ratio(report.synthesis_supported_required_points, report.synthesis_required_points_total)} | "
            f"{report.benchmark_confidence} | Required points with verified evidence support |"
        ),
        f"| Cost | {_cost_summary(report.cost)} | - | Pilot cost fields from typed oracle spec |",
        "",
        "## Synthesis Oracle",
        "",
        f"**Ideal conclusion:** {report.synthesis_oracle['ideal_conclusion']}",
        "",
        "### Required Points",
        "",
        "| ID | Point |",
        "|---|---|",
    ]
    for item in report.synthesis_oracle["required_points"]:
        lines.append(f"| {item.get('id', '')} | {item.get('text', '')} |")

    lines.extend(["", "### Forbidden Claims", "", "| ID | Claim |", "|---|---|"])
    for item in report.synthesis_oracle["forbidden_claims"]:
        lines.append(f"| {item.get('id', '')} | {item.get('text', '')} |")

    lines.extend(["", "### Uncertainty Notes", "", "| ID | Note |", "|---|---|"])
    for item in report.synthesis_oracle["uncertainty_notes"]:
        lines.append(f"| {item.get('id', '')} | {item.get('text', '')} |")

    lines.extend(
        [
            "",
            "## Synthesis Scoring",
            "",
            "| Criterion | Planner | Atlas | Notes |",
            "|---|---:|---:|---|",
        ]
    )
    for item in report.synthesis_oracle["criteria"]:
        lines.append(
            f"| {item['label']} | {_points(item['planner_points'], item['points_possible'])} | "
            f"{_points(item['atlas_points'], item['points_possible'])} | {item.get('notes', '')} |"
        )

    lines.extend(
        [
            "",
            "## Synthesis Support Warnings",
            "",
            "| Required Point | Warning | Supporting Rows |",
            "|---|---|---|",
        ]
    )
    if report.synthesis_warnings:
        for warning in report.synthesis_warnings:
            qids = ", ".join(warning.get("qids") or []) or "-"
            lines.append(
                f"| {warning.get('required_point_id')} | "
                f"{warning.get('warning')} | {qids} |"
            )
    else:
        lines.append("| - | - | - |")

    lines.extend(["", "## Failure Counts", "", "| Layer | Failure Type | Count |", "|---|---|---:|"])
    if not report.failure_counts:
        lines.append("| - | - | 0 |")
    else:
        for layer, counts in sorted(report.failure_counts.items()):
            for failure_type, count in sorted(counts.items()):
                lines.append(f"| {layer} | {failure_type} | {count} |")

    planner_invalid = [
        row for row in report.rows
        if row["oracle_bucket"] == "evidence"
        and row["oracle_status"] == "verified"
        and row["planner_evidence_status"] == "fail"
    ]
    lines.extend(
        [
            "",
            "## Planner Invalid Evidence Rows",
            "",
            "| QID | Failure Type | Notes |",
            "|---|---|---|",
        ]
    )
    if planner_invalid:
        for row in planner_invalid:
            lines.append(
                f"| {row['qid']} | {row.get('planner_failure_type') or 'planner_evidence_failed'} | "
                f"{row.get('notes') or ''} |"
            )
    else:
        lines.append("| - | - | None |")

    lines.extend(
        [
            "",
            "## Receipt Detail",
            "",
            "| QID | Bucket | Oracle | Planner Evidence | Atlas Evidence | Lane | Notes |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in report.rows:
        atlas = row["atlas_result"]
        if row["atlas_result"] == "fail" and row.get("atlas_failure_type"):
            atlas = f"fail: {row['atlas_failure_type']}"
        lines.append(
            f"| {row['qid']} | {row['oracle_bucket']} | {row['oracle_status']} | "
            f"{row['planner_evidence_status']} | {atlas} | "
            f"{row.get('lane') or '-'} | {row.get('notes') or ''} |"
        )

    lines.extend(
        [
            "",
            "## Packet Takeaway",
            "",
            _takeaway(report),
            "",
        ]
    )
    return "\n".join(lines)


def _runtime_rows_by_qid(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for summary in packet.get("summaries", []):
        artifact = summary.get("artifact") or {}
        for row in artifact.get("rows", []):
            qid = row.get("question_id")
            if qid:
                rows[str(qid)] = dict(row)
    return rows


def _synthesis_support_warnings(spec: LayeredSpec) -> list[dict[str, Any]]:
    """Warn when synthesis required points lack verified evidence support.

    Phase-1 layered sidecars are hand-authored. The warning is deliberately
    non-blocking, but it catches the common trap where a rubric keeps requiring
    a point after the evidence layer has correctly retyped its receipts as
    unresolved, context-only, or command-only. Those points may still be useful
    for synthesis, but they should not be mistaken for clean Atlas-evidence
    misses.
    """

    if spec.synthesis_status == "draft":
        return []

    required_ids = [
        str(item.get("id"))
        for item in spec.required_points
        if item.get("id") is not None
    ]
    warnings: list[dict[str, Any]] = []
    for point_id in required_ids:
        supporting = [
            row for row in spec.evidence_rows
            if point_id in row.required_point_ids
        ]
        if not supporting:
            warnings.append({
                "required_point_id": point_id,
                "warning": "no_supporting_rows",
                "qids": [],
            })
            continue

        verified_evidence = [
            row for row in supporting
            if row.oracle_bucket == "evidence" and row.oracle_status == "verified"
        ]
        if verified_evidence:
            continue

        if any(row.oracle_status != "verified" for row in supporting):
            warning = "depends_on_unresolved_evidence"
        elif any(row.oracle_bucket == "context" for row in supporting):
            warning = "context_only_support"
        elif any(row.oracle_bucket == "command" for row in supporting):
            warning = "command_only_support"
        else:
            warning = "no_verified_evidence_support"
        warnings.append({
            "required_point_id": point_id,
            "warning": warning,
            "qids": [row.qid for row in supporting],
        })
    return warnings


def _synthesis_repair_queue(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = {
        "no_supporting_rows": (
            "Attach the point to verified evidence with required_point_ids, "
            "or remove it from scoreable synthesis."
        ),
        "depends_on_unresolved_evidence": (
            "Repair the source reference or keep the point non-scoreable until "
            "the backing evidence verifies."
        ),
        "context_only_support": (
            "Move the point to context/uncertainty scoring, or add verified "
            "repo evidence before counting it."
        ),
        "command_only_support": (
            "Keep as deterministic command context, or add scoreable evidence "
            "if Atlas should be accountable for it."
        ),
        "no_verified_evidence_support": (
            "Add a verified scoreable evidence row, or make this an explicit "
            "qualitative/context criterion."
        ),
    }
    rows = []
    for warning in warnings:
        warning_type = str(warning.get("warning") or "")
        rows.append(
            {
                "packet_id": warning.get("packet_id"),
                "required_point_id": warning.get("required_point_id"),
                "warning": warning_type,
                "qids": warning.get("qids") or [],
                "recommended_action": actions.get(
                    warning_type,
                    "Review the required point and align it with verified evidence.",
                ),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("packet_id") or ""),
            str(row.get("required_point_id") or ""),
        ),
    )


def _required_points_total(spec: LayeredSpec) -> int:
    if spec.synthesis_status == "draft":
        return 0
    return sum(1 for item in spec.required_points if item.get("id") is not None)


def _atlas_failure_type(runtime: dict[str, Any]) -> str:
    if not runtime:
        return "atlas_not_run"
    lane = runtime.get("lane") or "unknown_lane"
    grade = runtime.get("grade") or "unknown_grade"
    if runtime.get("score_status") != "counted":
        return str(runtime.get("clean_excluded_reason") or runtime.get("score_status"))
    if lane == "explicit_source_fast_path":
        return "explicit_source_miss"
    if lane == "fuzzy_find_code":
        return "fuzzy_retrieval_miss"
    if lane == "doc_resolver":
        return "doc_resolver_miss"
    return f"{lane}:{grade}"


def _benchmark_confidence(verified: int, unresolved: int) -> str:
    total = verified + unresolved
    if total <= 0:
        return "red"
    coverage = verified / total
    unresolved_ratio = unresolved / total
    if coverage >= 0.9 and unresolved_ratio <= 0.1:
        return "green"
    if coverage >= 0.7:
        return "caution"
    return "red"


def _pct(num: float, den: float) -> Optional[float]:
    if den <= 0:
        return None
    return round((num / den) * 100, 1)


def _ratio(num: int, den: int) -> str:
    pct = _pct(num, den)
    if pct is None:
        return f"{num}/{den} (n/a)"
    return f"{num}/{den} = {pct:.1f}%"


def _points(num: float, den: float) -> str:
    pct = _pct(num, den)
    num_s = _fmt_number(num)
    den_s = _fmt_number(den)
    if pct is None:
        return f"{num_s}/{den_s} (n/a)"
    return f"{num_s}/{den_s} = {pct:.1f}%"


def _fmt_number(v: float) -> str:
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.1f}"


def _cost_summary(cost: dict[str, Any]) -> str:
    if not cost:
        return "not recorded"
    parts = []
    for key in ("claim_count", "verified_claim_count", "tool_calls", "model_calls"):
        if key in cost:
            parts.append(f"{key}={cost[key]}")
    return ", ".join(parts) if parts else "recorded"


def _score_source_summary(score_sources: dict[str, int]) -> str:
    if not score_sources:
        return "unknown"
    return ", ".join(
        f"{name} ({count})"
        for name, count in sorted(score_sources.items())
    )


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.split()).replace("|", "\\|")


def _takeaway(report: LayeredReport) -> str:
    if report.benchmark_confidence != "green":
        return (
            "This packet is useful as a pilot but still has enough unresolved "
            "or context-only evidence that benchmark confidence is not green."
        )
    if report.atlas_evidence_total and report.atlas_evidence_pct is not None:
        return (
            "The packet is a high-confidence benchmark; remaining Atlas misses "
            "are the prioritized retrieval target list."
        )
    return "The packet is high confidence but has little Atlas-eligible evidence."


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text else None
