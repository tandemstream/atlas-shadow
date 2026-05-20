from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from atlas_shadow.cli import main
from atlas_shadow.ingest_daemon import counted_misses


def _write_run(tmp_path: Path) -> Path:
    run = tmp_path / "baseline-test"
    (run / "artifacts").mkdir(parents=True)
    (run / "packets").mkdir()
    (run / "manifest.json").write_text(
        json.dumps({"run_name": "baseline-test"}),
        encoding="utf-8",
    )
    artifact = {
        "packet_id": "packet-a",
        "rows": [
            {
                "question_id": "q1",
                "question": "pass",
                "grade": "full_match",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
            },
            {
                "question_id": "q2",
                "question": "single-line miss",
                "grade": "atlas_not_found",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
                "evidence_type": "source_excerpt",
                "source_snapshot_status": "git_source_hash_match",
                "run_snapshot_status": "run_commit_hash_match",
                "atlas_citation_locations": ["core/code/types.py:336-336"],
                "atlas_citation_count": 1,
                "atlas_answer_len": 80,
                "rationale": "Only a decorator line was returned.",
            },
            {
                "question_id": "q3",
                "question": "skipped",
                "grade": "no_match",
                "score_status": "skipped_unavailable_source_ref",
                "lane": "fuzzy_find_code",
            },
            {
                "question_id": "q4",
                "question": "receipt drift",
                "grade": "no_match",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
                "source_snapshot_status": "git_source_hash_mismatch",
                "run_snapshot_status": "run_commit_hash_mismatch",
                "atlas_citation_locations": ["core/query/answer.py:1-1"],
            },
        ],
    }
    (run / "artifacts" / "pr-0-20260519T000000Z-packet-a.json").write_text(
        json.dumps(artifact),
        encoding="utf-8",
    )
    return run


def test_counted_miss_report_filters_and_groups(tmp_path: Path) -> None:
    run = _write_run(tmp_path)

    report = counted_misses.build_report(run)

    assert report.run_name == "baseline-test"
    assert report.total_misses == 2
    assert report.by_lane == {"explicit_source_fast_path": 2}
    assert report.by_fix_layer == {
        "explicit_span_range_parse_or_expansion": 1,
        "receipt_anchor_mismatch": 1,
    }
    qids = {m.question_id for m in report.misses}
    assert qids == {"q2", "q4"}


def test_counted_miss_report_writes_markdown_and_json(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    report = counted_misses.build_report(run)

    md_path, json_path = counted_misses.write_reports(report, tmp_path / "out")

    md = md_path.read_text(encoding="utf-8")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert "Counted Misses" in md
    assert "`q2`" in md
    assert data["total_misses"] == 2
    assert data["misses"][0]["packet_slug"] == "packet-a"


def test_cli_shadow_counted_misses(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "shadow-counted-misses",
            "--run-dir",
            str(run),
            "--output-dir",
            str(tmp_path / "report"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "counted_misses=2" in result.output
    assert (tmp_path / "report" / "counted-misses.md").is_file()
    assert (tmp_path / "report" / "counted-misses.json").is_file()
