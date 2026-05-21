import json
from pathlib import Path

from click.testing import CliRunner

from atlas_shadow.cli import main
from atlas_shadow.ingest_daemon import layered_report as lr


def _packet_json(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "packet.json"
    payload = {
        "packet_slug": "packet-x",
        "summaries": [
            {
                "artifact": {
                    "rows": rows,
                }
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _spec_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "oracle.yaml"
    path.write_text(
        """
schema_version: 1
packet_id: packet-x
title: Packet X
cost:
  claim_count: 3
  verified_claim_count: 2
  unresolved_claim_count: 1
  tool_calls: 8
evidence_oracle:
  rows:
    - qid: q1
      claim_type: current_behavior
      evidence_type: source_excerpt
      oracle_status: verified
      planner_evidence_status: pass
      oracle_bucket: evidence
      synthesis_role: required_point
    - qid: q2
      claim_type: current_behavior
      evidence_type: source_excerpt
      oracle_status: verified
      planner_evidence_status: fail
      planner_failure_type: stale_or_false_claim
      oracle_bucket: evidence
      synthesis_role: required_point
    - qid: q3
      claim_type: historical_context
      evidence_type: user_context
      oracle_status: unresolved
      oracle_failure_type: unresolved_source_ref
      planner_evidence_status: not_scored
      oracle_bucket: context
      synthesis_role: context_only
synthesis_oracle:
  ideal_conclusion: Do the thing, but preserve uncertainty.
  required_points:
    - id: S1
      text: Required point
  forbidden_claims:
    - id: F1
      text: Forbidden claim
  uncertainty_notes:
    - id: U1
      text: Uncertainty note
  criteria:
    - id: conclusion
      label: Correct conclusion
      points_possible: 1
      planner_points: 1
      atlas_points: 0.5
      atlas_miss_class: grader_too_harsh
    - id: required
      label: Required point covered
      points_possible: 2
      planner_points: 1
      atlas_points: 1
      planner_miss_class: evidence_present_answer_missed
      atlas_miss_class: evidence_missing
""",
        encoding="utf-8",
    )
    return path


def test_layered_report_builds_layered_scores(tmp_path):
    packet = _packet_json(
        tmp_path,
        [
            {
                "question_id": "q1",
                "grade": "full_match",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
            },
            {
                "question_id": "q2",
                "grade": "no_match",
                "score_status": "counted",
                "lane": "fuzzy_find_code",
            },
        ],
    )

    report = lr.build_report(spec_path=_spec_yaml(tmp_path), packet_json_path=packet)

    assert report.oracle_verified == 2
    assert report.oracle_unresolved == 1
    assert report.context_verified == 0
    assert report.command_verified == 0
    assert report.benchmark_confidence == "red"
    assert report.planner_evidence_pass == 1
    assert report.planner_evidence_total == 2
    assert report.atlas_evidence_pass == 1
    assert report.atlas_evidence_total == 2
    assert report.planner_synthesis_points == 2
    assert report.atlas_synthesis_points == 1.5
    assert report.synthesis_points_possible == 3
    assert report.failure_counts["planner_evidence"]["stale_or_false_claim"] == 1
    assert report.failure_counts["oracle"]["unresolved_source_ref"] == 1
    assert report.failure_counts["synthesis"]["planner:evidence_present_answer_missed"] == 1
    assert report.failure_counts["synthesis"]["atlas:evidence_missing"] == 1


def test_layered_report_excludes_runtime_skipped_rows_from_atlas_denominator(tmp_path):
    packet = _packet_json(
        tmp_path,
        [
            {
                "question_id": "q1",
                "grade": "no_match",
                "score_status": "skipped_run_commit_line_drift",
                "clean_excluded_reason": "run_commit_line_drift",
                "lane": "explicit_source_fast_path",
            },
        ],
    )

    report = lr.build_report(spec_path=_spec_yaml(tmp_path), packet_json_path=packet)

    assert report.atlas_evidence_pass == 0
    assert report.atlas_evidence_total == 2
    assert report.failure_counts["atlas_evidence"]["run_commit_line_drift"] == 1


def test_layered_report_writes_markdown_and_json(tmp_path):
    packet = _packet_json(
        tmp_path,
        [
            {
                "question_id": "q1",
                "grade": "partial_match",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
            },
        ],
    )
    report = lr.build_report(spec_path=_spec_yaml(tmp_path), packet_json_path=packet)

    md_path, json_path = lr.write_reports(report, tmp_path / "out")

    assert "Planner Evidence" in md_path.read_text(encoding="utf-8")
    assert "Command/Context Oracle" in md_path.read_text(encoding="utf-8")
    assert "Planner Invalid Evidence Rows" in md_path.read_text(encoding="utf-8")
    assert "stale_or_false_claim" in md_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["oracle"]["verified"] == 2
    assert payload["planner_synthesis"]["pct"] == 66.7


def test_shadow_layered_report_cli(tmp_path):
    packet = _packet_json(
        tmp_path,
        [
            {
                "question_id": "q1",
                "grade": "full_match",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
            },
        ],
    )
    spec = _spec_yaml(tmp_path)
    out_dir = tmp_path / "layered"

    result = CliRunner().invoke(
        main,
        [
            "shadow-layered-report",
            "--packet-json",
            str(packet),
            "--oracle-spec",
            str(spec),
            "--output-dir",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out_dir / "layered-shadow-report.md").exists()
    assert "oracle=2 verified + 0 context + 0 command + 1 unresolved" in result.output


def test_write_run_summary_renders_aggregate_and_packets(tmp_path):
    packet = _packet_json(
        tmp_path,
        [
            {
                "question_id": "q1",
                "grade": "full_match",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
            },
            {
                "question_id": "q2",
                "grade": "no_match",
                "score_status": "counted",
                "lane": "fuzzy_find_code",
            },
        ],
    )
    report = lr.build_report(spec_path=_spec_yaml(tmp_path), packet_json_path=packet)

    md_path, json_path = lr.write_run_summary([report], tmp_path / "summary")

    md = md_path.read_text(encoding="utf-8")
    assert "Layered Shadow Run Summary" in md
    assert "Planner Evidence" in md
    assert "`packet-x`" in md
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["totals"]["packets"] == 1
    assert payload["totals"]["planner_evidence_pct"] == 50.0


def test_write_synthesis_audit_requires_classified_misses(tmp_path):
    packet = _packet_json(
        tmp_path,
        [
            {
                "question_id": "q1",
                "grade": "full_match",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
            },
        ],
    )
    report = lr.build_report(spec_path=_spec_yaml(tmp_path), packet_json_path=packet)

    md_path, json_path = lr.write_synthesis_audit([report], tmp_path / "audit")

    md = md_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert "Synthesis Audit" in md
    assert payload["total_misses"] == 3
    assert payload["class_counts"]["evidence_missing"] == 1
    assert payload["class_counts"]["evidence_present_answer_missed"] == 1
    assert payload["class_counts"]["grader_too_harsh"] == 1


def test_layered_report_warns_when_required_point_has_no_verified_evidence(tmp_path):
    packet = _packet_json(
        tmp_path,
        [
            {
                "question_id": "q1",
                "grade": "full_match",
                "score_status": "counted",
                "lane": "explicit_source_fast_path",
            },
        ],
    )
    spec = tmp_path / "oracle.yaml"
    spec.write_text(
        """
schema_version: 1
packet_id: packet-x
title: Packet X
evidence_oracle:
  rows:
    - qid: q1
      claim_type: current_behavior
      evidence_type: source_excerpt
      oracle_status: unresolved
      oracle_failure_type: legacy_receipt_defect
      planner_evidence_status: fail
      oracle_bucket: evidence
      synthesis_role: required_point
      required_point_ids: [S1]
synthesis_oracle:
  ideal_conclusion: Do the thing.
  required_points:
    - id: S1
      text: Point backed only by unresolved evidence.
    - id: S2
      text: Point with no supporting rows.
  forbidden_claims: []
  uncertainty_notes: []
  criteria: []
""",
        encoding="utf-8",
    )

    report = lr.build_report(spec_path=spec, packet_json_path=packet)

    assert report.synthesis_warnings == [
        {
            "required_point_id": "S1",
            "warning": "depends_on_unresolved_evidence",
            "qids": ["q1"],
        },
        {
            "required_point_id": "S2",
            "warning": "no_supporting_rows",
            "qids": [],
        },
    ]
    rendered = lr.render_markdown(report)
    assert "Synthesis Support Warnings" in rendered
    assert "depends_on_unresolved_evidence" in rendered


def test_shadow_layered_batch_cli_writes_all_packet_reports_and_summary(tmp_path):
    run_dir = tmp_path / "run"
    packet_dir = run_dir / "packets"
    packet_dir.mkdir(parents=True)
    packet_path = packet_dir / "packet-x.json"
    packet_path.write_text(
        _packet_json(
            tmp_path,
            [
                {
                    "question_id": "q1",
                    "grade": "full_match",
                    "score_status": "counted",
                    "lane": "explicit_source_fast_path",
                },
            ],
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    oracle_dir = tmp_path / "oracles"
    oracle_dir.mkdir()
    (oracle_dir / "packet-x-layered-oracle.yaml").write_text(
        _spec_yaml(tmp_path).read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "shadow-layered-batch",
            "--run-dir",
            str(run_dir),
            "--oracle-dir",
            str(oracle_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (run_dir / "_layered" / "packet-x" / "layered-shadow-report.json").exists()
    assert (run_dir / "_layered" / "layered-summary.md").exists()
    assert (run_dir / "_layered" / "synthesis-audit.md").exists()
    assert "layered_packets=1" in result.output
