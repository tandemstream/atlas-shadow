"""aggregate — cross-packet metrics writer.

Reads `shadow-runs/<fixture-id>/atlas-qa-shadow.jsonl` files and emits a
single `shadow-runs/_aggregate/comparison-report.md` summarizing grade
distribution, latency, and Atlas-vs-grep delta. At Phase 2 there is only
one packet ("dogfood-v2"); the aggregator still emits a stub report so the
T-W4 acceptance criterion ("Total packets: 1") is verifiable.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def discover_runs(shadow_runs_dir: Path) -> list[Path]:
    """Return a sorted list of per-fixture `atlas-qa-shadow.jsonl` paths."""
    if not shadow_runs_dir.exists():
        return []
    out: list[Path] = []
    for child in sorted(shadow_runs_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        candidate = child / "atlas-qa-shadow.jsonl"
        if candidate.exists():
            out.append(candidate)
    return out


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} — invalid JSON: {exc}") from exc
    return rows


def summarize_fixture(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grades = Counter()
    tools = Counter()
    latencies: list[int] = []
    exceptions = 0
    for row in rows:
        grader = row.get("grader_response") or {}
        atlas = row.get("atlas_response") or {}
        grade = grader.get("grade") or "(missing)"
        grades[grade] += 1
        tool = atlas.get("tool_used") or "(unknown)"
        tools[tool] += 1
        lat = atlas.get("atlas_latency_ms")
        if isinstance(lat, (int, float)):
            latencies.append(int(lat))
        if atlas.get("exception"):
            exceptions += 1
    total = sum(grades.values())
    avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0
    return {
        "total": total,
        "grades": dict(grades),
        "tools": dict(tools),
        "avg_latency_ms": avg_latency,
        "exceptions": exceptions,
    }


def render_report(per_fixture: dict[str, dict[str, Any]]) -> str:
    """Render a markdown report from per-fixture summary dicts."""
    lines: list[str] = []
    lines.append("# atlas-shadow comparison report")
    lines.append("")
    lines.append(f"Total packets: {len(per_fixture)}")
    lines.append("")
    if not per_fixture:
        lines.append("_No shadow runs found under `shadow-runs/`._")
        return "\n".join(lines) + "\n"

    # Per-packet detail
    for fixture_id in sorted(per_fixture.keys()):
        summary = per_fixture[fixture_id]
        lines.append(f"## {fixture_id}")
        lines.append("")
        lines.append(f"- Total questions: {summary['total']}")
        lines.append(f"- Exceptions: {summary['exceptions']}")
        lines.append(f"- Avg Atlas latency: {summary['avg_latency_ms']} ms")
        lines.append("")
        lines.append("### Grade distribution")
        lines.append("")
        lines.append("| grade | count |")
        lines.append("| --- | --- |")
        for grade in ("full_match", "partial_match", "no_match", "atlas_not_found"):
            count = summary["grades"].get(grade, 0)
            lines.append(f"| {grade} | {count} |")
        lines.append("")
        lines.append("### Tool routing")
        lines.append("")
        lines.append("| tool | count |")
        lines.append("| --- | --- |")
        for tool, count in sorted(summary["tools"].items()):
            lines.append(f"| {tool} | {count} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def aggregate(shadow_runs_dir: Path, out_path: Path) -> dict[str, Any]:
    """Discover all per-fixture runs, summarize, write report. Returns the
    aggregate summary dict."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    runs = discover_runs(shadow_runs_dir)
    per_fixture: dict[str, dict[str, Any]] = {}
    for run_path in runs:
        fixture_id = run_path.parent.name
        rows = _load_jsonl(run_path)
        per_fixture[fixture_id] = summarize_fixture(rows)
    report = render_report(per_fixture)
    out_path.write_text(report, encoding="utf-8")
    return {
        "total_packets": len(per_fixture),
        "per_fixture": per_fixture,
        "report_path": str(out_path),
    }
