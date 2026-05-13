"""Tests for atlas_shadow.aggregate."""

from __future__ import annotations

import json
from pathlib import Path

from atlas_shadow import aggregate as aggregate_mod


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for r in rows:
            fp.write(json.dumps(r) + "\n")


def test_aggregate_empty_shadow_runs_dir_emits_zero_packets(tmp_path):
    out = tmp_path / "_aggregate" / "comparison-report.md"
    summary = aggregate_mod.aggregate(tmp_path, out)
    assert summary["total_packets"] == 0
    text = out.read_text(encoding="utf-8")
    assert "Total packets: 0" in text


def test_aggregate_one_packet_emits_summary(tmp_path):
    rows = [
        {
            "question_id": "Q01",
            "atlas_response": {"tool_used": "find_code", "atlas_latency_ms": 100},
            "grader_response": {"grade": "full_match", "confidence": 0.9},
        },
        {
            "question_id": "Q02",
            "atlas_response": {"tool_used": "find_code", "atlas_latency_ms": 200},
            "grader_response": {"grade": "partial_match", "confidence": 0.5},
        },
        {
            "question_id": "Q03",
            "atlas_response": {"tool_used": "scan_search", "atlas_latency_ms": 300},
            "grader_response": {"grade": "atlas_not_found", "confidence": 1.0},
        },
    ]
    _write_jsonl(tmp_path / "dogfood-v2-questions" / "atlas-qa-shadow.jsonl", rows)
    out = tmp_path / "_aggregate" / "comparison-report.md"
    summary = aggregate_mod.aggregate(tmp_path, out)
    assert summary["total_packets"] == 1
    fixture_summary = summary["per_fixture"]["dogfood-v2-questions"]
    assert fixture_summary["total"] == 3
    assert fixture_summary["grades"]["full_match"] == 1
    assert fixture_summary["grades"]["partial_match"] == 1
    assert fixture_summary["grades"]["atlas_not_found"] == 1
    assert fixture_summary["tools"]["find_code"] == 2
    assert fixture_summary["tools"]["scan_search"] == 1
    assert fixture_summary["avg_latency_ms"] == 200

    text = out.read_text(encoding="utf-8")
    assert "Total packets: 1" in text
    assert "dogfood-v2-questions" in text
    assert "| full_match | 1 |" in text
    assert "| partial_match | 1 |" in text
    assert "| atlas_not_found | 1 |" in text


def test_aggregate_skips_underscore_prefixed_dirs(tmp_path):
    """`_aggregate/` itself should never be re-aggregated."""
    rows = [
        {
            "atlas_response": {"tool_used": "find_code", "atlas_latency_ms": 10},
            "grader_response": {"grade": "full_match"},
        }
    ]
    _write_jsonl(tmp_path / "fixture-a" / "atlas-qa-shadow.jsonl", rows)
    _write_jsonl(tmp_path / "_aggregate" / "atlas-qa-shadow.jsonl", rows)
    out = tmp_path / "_aggregate" / "comparison-report.md"
    summary = aggregate_mod.aggregate(tmp_path, out)
    assert summary["total_packets"] == 1
    assert "fixture-a" in summary["per_fixture"]
    assert "_aggregate" not in summary["per_fixture"]


def test_discover_runs_returns_sorted_paths(tmp_path):
    _write_jsonl(tmp_path / "b-fix" / "atlas-qa-shadow.jsonl", [{}])
    _write_jsonl(tmp_path / "a-fix" / "atlas-qa-shadow.jsonl", [{}])
    runs = aggregate_mod.discover_runs(tmp_path)
    assert [r.parent.name for r in runs] == ["a-fix", "b-fix"]


def test_render_report_zero_packets():
    text = aggregate_mod.render_report({})
    assert "Total packets: 0" in text
    assert "No shadow runs found" in text
