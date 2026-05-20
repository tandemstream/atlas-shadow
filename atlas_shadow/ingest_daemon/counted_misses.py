"""counted_misses — report clean-denominator failures for a shadow run.

The clean score is useful only if the next tuning queue is easy to see.
This module reads a ``shadow-runs/<run>/`` directory and extracts rows
that still counted against Atlas after all skip/status routing has run.
Those rows are the high-signal retrieval worklist.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


PASS_GRADES: frozenset[str] = frozenset({"full_match", "partial_match"})


@dataclass(frozen=True)
class CountedMiss:
    packet_slug: str
    question_id: str
    question: str
    grade: str
    lane: str
    evidence_type: str
    source_snapshot_status: Optional[str]
    run_snapshot_status: Optional[str]
    citation_locations: tuple[str, ...] = ()
    citation_count: Optional[int] = None
    answer_len: Optional[int] = None
    rationale: str = ""
    suspected_fix_layer: str = "triage"

    def as_dict(self) -> dict[str, Any]:
        return {
            "packet_slug": self.packet_slug,
            "question_id": self.question_id,
            "question": self.question,
            "grade": self.grade,
            "lane": self.lane,
            "evidence_type": self.evidence_type,
            "source_snapshot_status": self.source_snapshot_status,
            "run_snapshot_status": self.run_snapshot_status,
            "citation_locations": list(self.citation_locations),
            "citation_count": self.citation_count,
            "answer_len": self.answer_len,
            "rationale": self.rationale,
            "suspected_fix_layer": self.suspected_fix_layer,
        }


@dataclass(frozen=True)
class CountedMissReport:
    run_name: str
    run_dir: str
    total_misses: int
    by_lane: dict[str, int] = field(default_factory=dict)
    by_fix_layer: dict[str, int] = field(default_factory=dict)
    misses: list[CountedMiss] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_name": self.run_name,
            "run_dir": self.run_dir,
            "total_misses": self.total_misses,
            "by_lane": self.by_lane,
            "by_fix_layer": self.by_fix_layer,
            "misses": [m.as_dict() for m in self.misses],
        }


def build_report(run_dir: Path) -> CountedMissReport:
    run_dir = Path(run_dir)
    manifest = _load_json(run_dir / "manifest.json")
    run_name = str(manifest.get("run_name") or run_dir.name)
    misses: list[CountedMiss] = []

    for artifact_path in sorted((run_dir / "artifacts").glob("*.json")):
        artifact = _load_json(artifact_path)
        packet_slug = str(
            artifact.get("packet_id")
            or artifact.get("packet_slug")
            or _packet_slug_from_artifact_name(artifact_path)
        )
        for row in artifact.get("rows") or []:
            if not _is_counted_miss(row):
                continue
            misses.append(_miss_from_row(packet_slug, row))

    misses.sort(key=lambda m: (m.lane, m.suspected_fix_layer, m.packet_slug, m.question_id))
    return CountedMissReport(
        run_name=run_name,
        run_dir=str(run_dir),
        total_misses=len(misses),
        by_lane=dict(sorted(Counter(m.lane for m in misses).items())),
        by_fix_layer=dict(sorted(Counter(m.suspected_fix_layer for m in misses).items())),
        misses=misses,
    )


def write_reports(report: CountedMissReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "counted-misses.md"
    json_path = output_dir / "counted-misses.json"
    md_path.write_text(format_markdown(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return md_path, json_path


def format_markdown(report: CountedMissReport) -> str:
    lines: list[str] = []
    lines.append(f"# Counted Misses — {report.run_name}")
    lines.append("")
    lines.append(f"- **Run dir:** `{report.run_dir}`")
    lines.append(f"- **Total counted misses:** {report.total_misses}")
    lines.append("")

    lines.append("## By Lane")
    lines.append("")
    lines.append("| Lane | Misses |")
    lines.append("|---|---:|")
    for lane, count in report.by_lane.items():
        lines.append(f"| `{lane}` | {count} |")
    if not report.by_lane:
        lines.append("| _(none)_ | 0 |")
    lines.append("")

    lines.append("## By Suspected Fix Layer")
    lines.append("")
    lines.append("| Fix layer | Misses |")
    lines.append("|---|---:|")
    for layer, count in report.by_fix_layer.items():
        lines.append(f"| `{layer}` | {count} |")
    if not report.by_fix_layer:
        lines.append("| _(none)_ | 0 |")
    lines.append("")

    lines.append("## Rows")
    lines.append("")
    lines.append("| Packet | QID | Lane | Grade | Snapshots | Fix layer | First citations | Rationale |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for miss in report.misses:
        citations = "<br>".join(f"`{c}`" for c in miss.citation_locations[:3])
        if miss.citation_count is not None and miss.citation_count > 3:
            citations += f"<br>... +{miss.citation_count - 3}"
        if not citations:
            citations = "_none_"
        snapshots = (
            f"`src={miss.source_snapshot_status or 'null'}`<br>"
            f"`run={miss.run_snapshot_status or 'null'}`"
        )
        rationale = _one_line(miss.rationale, max_len=220)
        lines.append(
            "| "
            f"`{miss.packet_slug}` | `{miss.question_id}` | `{miss.lane}` | "
            f"`{miss.grade}` | {snapshots} | `{miss.suspected_fix_layer}` | "
            f"{citations} | {rationale} |"
        )
    if not report.misses:
        lines.append("| _(none)_ |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _packet_slug_from_artifact_name(path: Path) -> str:
    name = path.stem
    marker = "Z-"
    if marker in name:
        return name.split(marker, 1)[1]
    return name


def _is_counted_miss(row: dict[str, Any]) -> bool:
    return (
        (row.get("score_status") or "counted") == "counted"
        and str(row.get("grade") or "") not in PASS_GRADES
    )


def _miss_from_row(packet_slug: str, row: dict[str, Any]) -> CountedMiss:
    lane = str(row.get("lane") or "unknown")
    source_snapshot = row.get("source_snapshot_status")
    run_snapshot = row.get("run_snapshot_status")
    citations = tuple(str(c) for c in (row.get("atlas_citation_locations") or ()))
    fix_layer = _suspected_fix_layer(
        lane=lane,
        source_snapshot_status=source_snapshot,
        run_snapshot_status=run_snapshot,
        citation_locations=citations,
        citation_count=row.get("atlas_citation_count"),
        rationale=str(row.get("rationale") or ""),
    )
    return CountedMiss(
        packet_slug=packet_slug,
        question_id=str(row.get("question_id") or ""),
        question=str(row.get("question") or ""),
        grade=str(row.get("grade") or ""),
        lane=lane,
        evidence_type=str(row.get("evidence_type") or "other"),
        source_snapshot_status=source_snapshot,
        run_snapshot_status=run_snapshot,
        citation_locations=citations,
        citation_count=_int_or_none(row.get("atlas_citation_count")),
        answer_len=_int_or_none(row.get("atlas_answer_len")),
        rationale=str(row.get("rationale") or ""),
        suspected_fix_layer=fix_layer,
    )


def _suspected_fix_layer(
    *,
    lane: str,
    source_snapshot_status: Optional[str],
    run_snapshot_status: Optional[str],
    citation_locations: tuple[str, ...],
    citation_count: Any,
    rationale: str,
) -> str:
    """Best-effort triage label, deliberately conservative.

    This is not a grader verdict. It just gives the next engineer a
    sortable queue. Snapshot mismatch on both receipt/run commits is
    treated as receipt repair before retrieval tuning.
    """
    if source_snapshot_status == "git_source_hash_mismatch":
        if run_snapshot_status == "run_commit_hash_mismatch":
            return "receipt_anchor_mismatch"
        return "receipt_snapshot_mismatch"
    if run_snapshot_status in {"run_commit_hash_mismatch", "run_commit_source_missing"}:
        return "run_commit_line_drift"
    if lane == "explicit_source_fast_path":
        if _looks_single_line(citation_locations):
            return "explicit_span_range_parse_or_expansion"
        return "explicit_span_rendering"
    if lane == "fuzzy_find_code":
        if _mentions_command_or_aggregate(rationale):
            return "receipt_or_command_routing"
        return "fuzzy_retrieval"
    if lane == "doc_resolver":
        return "doc_resolver"
    if not citation_count:
        return "empty_atlas_response"
    return "triage"


def _looks_single_line(citation_locations: tuple[str, ...]) -> bool:
    if len(citation_locations) != 1:
        return False
    loc = citation_locations[0]
    if ":" not in loc or "-" not in loc:
        return False
    try:
        range_part = loc.rsplit(":", 1)[1]
        start, end = range_part.split("-", 1)
        return int(start) == int(end)
    except (TypeError, ValueError):
        return False


def _mentions_command_or_aggregate(rationale: str) -> bool:
    lower = rationale.lower()
    return any(
        token in lower
        for token in (
            "file-size distribution",
            "ranked list",
            "grep",
            "wc -l",
            "absence",
            "command",
        )
    )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _one_line(text: str, *, max_len: int) -> str:
    out = " ".join(str(text or "").split())
    if len(out) <= max_len:
        return out
    return out[: max_len - 1].rstrip() + "…"
