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
    assert report.synthesis_score_source == "authored_static"
    assert report.synthesis_supported_required_points == 0
    assert report.synthesis_required_points_total == 1
    assert report.synthesis_readiness_pct == 0.0
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
    assert payload["synthesis_score_source"] == "authored_static"
    assert payload["synthesis_readiness"] == {
        "supported_required_points": 0,
        "required_points": 1,
        "pct": 0.0,
    }


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
    assert payload["totals"]["synthesis_readiness_pct"] == 0.0
    assert payload["packets"][0]["synthesis_readiness"]["required_points"] == 1


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
    assert payload["score_sources"] == {"authored_static": 1}
    assert payload["support_warnings"][0]["warning"] == "no_supporting_rows"
    assert payload["repair_queue"][0]["recommended_action"].startswith(
        "Attach the point to verified evidence"
    )
    assert "Repair Queue" in md
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
    assert "Synthesis Readiness" in rendered
    assert "depends_on_unresolved_evidence" in rendered
    assert report.synthesis_supported_required_points == 0
    assert report.synthesis_required_points_total == 2


def test_layered_report_counts_supported_required_points(tmp_path):
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
      oracle_status: verified
      planner_evidence_status: pass
      oracle_bucket: evidence
      synthesis_role: required_point
      required_point_ids: [S1]
synthesis_oracle:
  ideal_conclusion: Do the thing.
  required_points:
    - id: S1
      text: Point backed by verified evidence.
    - id: S2
      text: Point with no supporting rows.
  forbidden_claims: []
  uncertainty_notes: []
  criteria: []
""",
        encoding="utf-8",
    )

    report = lr.build_report(spec_path=spec, packet_json_path=packet)

    assert report.synthesis_supported_required_points == 1
    assert report.synthesis_required_points_total == 2
    assert report.synthesis_readiness_pct == 50.0


def test_synthesis_audit_uses_subcriterion_miss_rows(tmp_path):
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
      oracle_status: verified
      planner_evidence_status: pass
      oracle_bucket: evidence
      synthesis_role: required_point
      required_point_ids: [S1]
synthesis_oracle:
  ideal_conclusion: Do the thing.
  required_points:
    - id: S1
      text: Point backed by verified evidence.
  forbidden_claims: []
  uncertainty_notes: []
  criteria:
    - id: required_points
      label: Required points
      points_possible: 2
      planner_points: 2
      atlas_points: 1
      atlas_miss_class: evidence_missing
      subcriteria:
        - id: required_points.first
          text: First point.
          points: 1
          supporting_qids: [q1]
          required_point_id: S1
          planner_status: covered
          atlas_status: covered
        - id: required_points.second
          text: Second point.
          points: 1
          supporting_qids: [q1]
          required_point_id: S1
          planner_status: covered
          atlas_status: missed
          atlas_miss_class: evidence_missing
""",
        encoding="utf-8",
    )
    report = lr.build_report(spec_path=spec, packet_json_path=packet)

    md_path, json_path = lr.write_synthesis_audit([report], tmp_path / "audit")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    md = md_path.read_text(encoding="utf-8")

    assert payload["total_misses"] == 1
    assert payload["rows"][0]["subcriterion_id"] == "required_points.second"
    assert payload["rows"][0]["supporting_qids"] == ["q1"]
    assert "required_points.second" in md


def test_validate_spec_catches_strict_subcriteria_errors(tmp_path):
    spec = tmp_path / "oracle.yaml"
    spec.write_text(
        """
schema_version: 1
packet_id: packet-x
title: Packet X
evidence_oracle:
  rows:
    - qid: q1
      oracle_status: verified
      planner_evidence_status: pass
      oracle_bucket: evidence
synthesis_oracle:
  required_points:
    - id: S1
      text: Point
  criteria:
    - id: required_points
      label: Required points
      points_possible: 2
      planner_points: 1
      atlas_points: 1
      subcriteria:
        - id: required_points.first
          text: First point.
          points: 1
          supporting_qids: [q-missing]
          required_point_id: S-missing
          planner_status: covered
          atlas_status: missed
""",
        encoding="utf-8",
    )

    errors = lr.validate_spec(spec)

    assert any("atlas_status=missed requires atlas_miss_class" in e for e in errors)
    assert any("unknown qid q-missing" in e for e in errors)
    assert any("unknown required_point_id S-missing" in e for e in errors)
    assert any("points_possible 2 != subcriteria sum 1" in e for e in errors)


def test_validate_spec_can_require_required_point_support_tags(tmp_path):
    spec = tmp_path / "oracle.yaml"
    spec.write_text(
        """
schema_version: 1
packet_id: packet-x
title: Packet X
evidence_oracle:
  rows:
    - qid: q1
      oracle_status: verified
      planner_evidence_status: pass
      oracle_bucket: evidence
      required_point_ids: []
synthesis_oracle:
  required_points:
    - id: S1
      text: Point
  criteria:
    - id: required_points
      label: Required points
      points_possible: 1
      planner_points: 1
      atlas_points: 1
      subcriteria:
        - id: required_points.first
          text: First point.
          points: 1
          supporting_qids: [q1]
          required_point_id: S1
          planner_status: covered
          atlas_status: covered
""",
        encoding="utf-8",
    )

    assert lr.validate_spec(spec) == []
    errors = lr.validate_spec(spec, require_required_point_support=True)

    assert any("qid q1 does not list required_point_id S1" in e for e in errors)
    assert any("required_points.S1: no evidence_oracle row" in e for e in errors)


def test_shadow_validate_layered_oracles_cli(tmp_path):
    oracle_dir = tmp_path / "oracles"
    oracle_dir.mkdir()
    (oracle_dir / "packet-x-layered-oracle.yaml").write_text(
        _spec_yaml(tmp_path).read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "shadow-validate-layered-oracles",
            "--oracle-dir",
            str(oracle_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "validated_layered_oracles=1" in result.output


def test_shadow_validate_layered_oracles_cli_can_require_support_tags(tmp_path):
    oracle_dir = tmp_path / "oracles"
    oracle_dir.mkdir()
    spec = _spec_yaml(tmp_path).read_text(encoding="utf-8").replace(
        "required_points:\n    - id: S1\n      text: Required point",
        "required_points:\n    - id: S1\n      text: Required point",
    ).replace(
        "criteria:\n    - id: conclusion",
        """criteria:
    - id: strict
      label: Strict
      points_possible: 1
      planner_points: 1
      atlas_points: 1
      subcriteria:
        - id: strict.first
          text: First point.
          points: 1
          supporting_qids: [q1]
          required_point_id: S1
          planner_status: covered
          atlas_status: covered
    - id: conclusion""",
    )
    (oracle_dir / "packet-x-layered-oracle.yaml").write_text(
        spec,
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "shadow-validate-layered-oracles",
            "--oracle-dir",
            str(oracle_dir),
            "--require-required-point-support",
        ],
    )

    assert result.exit_code != 0
    assert "does not list required_point_id S1" in result.output


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


def test_shadow_layered_batch_cli_filters_named_packet_slice(tmp_path):
    run_dir = tmp_path / "run"
    packet_dir = run_dir / "packets"
    packet_dir.mkdir(parents=True)
    for slug in ("packet-x", "packet-y"):
        (packet_dir / f"{slug}.json").write_text(
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
    for slug in ("packet-x", "packet-y"):
        text = _spec_yaml(tmp_path).read_text(encoding="utf-8").replace(
            "packet_id: packet-x",
            f"packet_id: {slug}",
        )
        (oracle_dir / f"{slug}-layered-oracle.yaml").write_text(
            text,
            encoding="utf-8",
        )
    slice_config = tmp_path / "slices.yaml"
    slice_config.write_text(
        """
schema_version: 1
slices:
  only-x:
    packets:
      - packet-x
""",
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
            "--packet-slice",
            "only-x",
            "--slice-config",
            str(slice_config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (run_dir / "_layered-only-x" / "packet-x").exists()
    assert not (run_dir / "_layered-only-x" / "packet-y").exists()
    assert "layered_packets=1 packet_slice=only-x" in result.output


def test_shadow_gold_slice_scoreboard_cli(tmp_path):
    run_dir = tmp_path / "run"
    packet_dir = run_dir / "packets"
    packet_dir.mkdir(parents=True)
    for slug in ("packet-x", "packet-y"):
        (packet_dir / f"{slug}.json").write_text(
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
    for slug in ("packet-x", "packet-y"):
        text = _spec_yaml(tmp_path).read_text(encoding="utf-8").replace(
            "packet_id: packet-x",
            f"packet_id: {slug}",
        )
        (oracle_dir / f"{slug}-layered-oracle.yaml").write_text(
            text,
            encoding="utf-8",
        )
    slice_config = tmp_path / "slices.yaml"
    slice_config.write_text(
        """
schema_version: 1
slices:
  both:
    description: Both packets.
    packets:
      - packet-x
      - packet-y
  one-missing:
    description: Missing packet is surfaced, not hidden.
    packets:
      - packet-missing
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "shadow-gold-slice-scoreboard",
            "--run-dir",
            str(run_dir),
            "--oracle-dir",
            str(oracle_dir),
            "--slice-config",
            str(slice_config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "gold_slices=2" in result.output
    md = (run_dir / "_gold_slices" / "gold-slice-scoreboard.md").read_text(
        encoding="utf-8"
    )
    payload = json.loads(
        (run_dir / "_gold_slices" / "gold-slice-scoreboard.json").read_text(
            encoding="utf-8"
        )
    )
    assert "`both`" in md
    assert "2/2" in md
    assert payload["slices"][0]["slice"] == "both"
    assert payload["slices"][1]["missing_packets"] == ["packet-missing"]
