"""Tests for ``compare_runs`` — probe-comparison tool that diffs two
``shadow-runs/baseline-*`` directories.

Coverage areas:

- ``classify_transition`` — exhaustively pins every per-receipt
  transition: still_passing, newly_passing, newly_failing,
  newly_skipped, un_skipped_*, still_skipped, appeared,
  disappeared. Critical because the entire downstream report
  depends on this classifier.
- ``_extract_rows`` — pulls rows out of multi-summary packet
  artifacts, dedupes by question_id.
- ``compare_runs`` end-to-end — load two synthetic runs from disk,
  assert run-level deltas + per-packet transition counts.
- Back-compat — pre-PR-#14 baselines (no score_status) compare
  cleanly against modern ones.
- ``render_markdown`` / ``render_json`` — basic smoke tests, no
  golden files (rendering is opinionated and likely to evolve).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_shadow.ingest_daemon import compare_runs as cr


# ─── classify_transition ──────────────────────────────────────────────


def _row(grade=None, score_status="counted", **extra):
    """Build a row dict in the on-disk shape."""
    d = {"question_id": "q1", "grade": grade, "score_status": score_status}
    d.update(extra)
    return d


def test_classify_still_passing():
    before = _row(grade="full_match")
    after = _row(grade="full_match")
    assert cr.classify_transition(before, after) == cr.TRANSITION_STILL_PASSING


def test_classify_still_failing():
    before = _row(grade="no_match")
    after = _row(grade="no_match")
    assert cr.classify_transition(before, after) == cr.TRANSITION_STILL_FAILING


def test_classify_newly_passing():
    before = _row(grade="no_match")
    after = _row(grade="full_match")
    assert cr.classify_transition(before, after) == cr.TRANSITION_NEWLY_PASSING


def test_classify_newly_passing_from_partial():
    """partial_match counts as a pass."""
    before = _row(grade="atlas_not_found")
    after = _row(grade="partial_match")
    assert cr.classify_transition(before, after) == cr.TRANSITION_NEWLY_PASSING


def test_classify_newly_failing():
    before = _row(grade="full_match")
    after = _row(grade="no_match")
    assert cr.classify_transition(before, after) == cr.TRANSITION_NEWLY_FAILING


def test_classify_newly_skipped():
    before = _row(grade="no_match", score_status="counted")
    after = _row(grade="atlas_not_found", score_status="skipped_command_snapshot")
    assert cr.classify_transition(before, after) == cr.TRANSITION_NEWLY_SKIPPED


def test_classify_newly_skipped_from_passing():
    """Even a counted-pass that becomes skipped is newly_skipped — the
    transition classifier reads the score_status transition, not the
    grade."""
    before = _row(grade="full_match", score_status="counted")
    after = _row(grade="full_match", score_status="skipped_receipt_stale")
    assert cr.classify_transition(before, after) == cr.TRANSITION_NEWLY_SKIPPED


def test_classify_un_skipped_passing():
    before = _row(grade="atlas_not_found", score_status="skipped_receipt_stale")
    after = _row(grade="full_match", score_status="counted")
    assert cr.classify_transition(before, after) == cr.TRANSITION_UN_SKIPPED_PASSING


def test_classify_un_skipped_failing():
    before = _row(grade="atlas_not_found", score_status="skipped_receipt_stale")
    after = _row(grade="no_match", score_status="counted")
    assert cr.classify_transition(before, after) == cr.TRANSITION_UN_SKIPPED_FAILING


def test_classify_still_skipped():
    before = _row(grade="atlas_not_found", score_status="skipped_receipt_stale")
    after = _row(grade="atlas_not_found", score_status="skipped_command_snapshot")
    # status changed but both still excluded — still_skipped is correct
    # (the receipt didn't re-enter the clean denominator).
    assert cr.classify_transition(before, after) == cr.TRANSITION_STILL_SKIPPED


def test_classify_appeared():
    assert cr.classify_transition(None, _row(grade="full_match")) == cr.TRANSITION_APPEARED


def test_classify_disappeared():
    assert cr.classify_transition(_row(grade="full_match"), None) == cr.TRANSITION_DISAPPEARED


def test_classify_legacy_row_missing_score_status_treated_as_counted():
    """Pre-PR-#14 rows lack ``score_status``. Default to counted so a
    legacy baseline diffed against a modern one classifies cleanly
    (rather than every row counting as 'still_skipped' because the
    field is missing)."""
    before = {"question_id": "q1", "grade": "no_match"}  # no score_status
    after = _row(grade="full_match")
    assert cr.classify_transition(before, after) == cr.TRANSITION_NEWLY_PASSING


# ─── _extract_rows ────────────────────────────────────────────────────


def test_extract_rows_pulls_from_multi_summary_artifact():
    artifact = {
        "summaries": [
            {"artifact": {"rows": [{"question_id": "q1", "grade": "full_match"}]}},
            {"artifact": {"rows": [{"question_id": "q2", "grade": "no_match"}]}},
        ]
    }
    rows = cr._extract_rows(artifact)
    qids = sorted(r["question_id"] for r in rows)
    assert qids == ["q1", "q2"]


def test_extract_rows_dedupes_by_question_id_last_wins():
    """If two summaries carry the same qid, the last one wins."""
    artifact = {
        "summaries": [
            {"artifact": {"rows": [{"question_id": "q1", "grade": "no_match"}]}},
            {"artifact": {"rows": [{"question_id": "q1", "grade": "full_match"}]}},
        ]
    }
    rows = cr._extract_rows(artifact)
    assert len(rows) == 1
    assert rows[0]["grade"] == "full_match"


def test_extract_rows_handles_missing_artifact():
    """A summary without an inlined ``artifact`` key (legacy or in-flight)
    just contributes zero rows."""
    artifact = {"summaries": [{"packet_id": "p1"}]}
    assert cr._extract_rows(artifact) == []


def test_extract_rows_handles_empty_summaries():
    assert cr._extract_rows({"summaries": []}) == []
    assert cr._extract_rows({}) == []


# ─── compare_runs end-to-end ──────────────────────────────────────────


def _write_run(
    root: Path,
    run_name: str,
    manifest_overrides: dict = None,
    packets: dict = None,
) -> Path:
    """Materialize a synthetic baseline-X/ directory with manifest.json
    + packets/<slug>.json files."""
    run_dir = root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_name": run_name,
        "commit_sha": "abc1234",
        "code_revision_id": "rev-1",
        "started_at": "2026-05-19T00:00:00Z",
        "finished_at": "2026-05-19T01:00:00Z",
        "total_packets": 1,
        "total_receipts": 0,
        "total_correct": 0,
        "overall_pct": 0.0,
        "per_packet_pct": {},
    }
    manifest.update(manifest_overrides or {})
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    packets_dir = run_dir / "packets"
    packets_dir.mkdir(parents=True, exist_ok=True)
    for slug, packet_artifact in (packets or {}).items():
        packet_artifact = dict(packet_artifact)
        packet_artifact.setdefault("packet_slug", slug)
        (packets_dir / f"{slug}.json").write_text(
            json.dumps(packet_artifact), encoding="utf-8",
        )
    return run_dir


def _packet_artifact(rows: list[dict]) -> dict:
    """Build a packet artifact with the inlined-artifact shape that
    `write_packet_artifact` produces."""
    return {
        "summaries": [{"artifact": {"rows": rows}}],
    }


def test_compare_runs_run_level_deltas(tmp_path: Path):
    """End-to-end: run-level raw/clean deltas + skip-category deltas
    reflect the manifests, even when no per-packet artifacts exist."""
    before = _write_run(tmp_path, "baseline-before", manifest_overrides={
        "overall_pct": 10.0,
        "clean_overall_pct": 25.0,
        "total_skipped_receipt_stale": 5,
        "total_skipped_command_snapshot": 0,
        "per_packet_pct": {},
    })
    after = _write_run(tmp_path, "baseline-after", manifest_overrides={
        "overall_pct": 15.0,
        "clean_overall_pct": 42.0,
        "total_skipped_receipt_stale": 8,
        "total_skipped_command_snapshot": 2,
        "per_packet_pct": {},
    })
    result = cr.compare_runs(before, after)
    assert result.before_clean_pct == 25.0
    assert result.after_clean_pct == 42.0
    assert result.clean_pct_delta_pp == 17.0
    assert result.before_raw_pct == 10.0
    assert result.after_raw_pct == 15.0
    assert result.raw_pct_delta_pp == 5.0
    # Skip-category deltas.
    skip = result.skip_category_deltas
    assert skip["total_skipped_receipt_stale"] == {
        "before": 5, "after": 8, "delta": 3,
    }
    assert skip["total_skipped_command_snapshot"] == {
        "before": 0, "after": 2, "delta": 2,
    }


def test_compare_runs_clean_pct_none_when_missing_either_side(tmp_path: Path):
    """If either manifest lacks clean_overall_pct, the delta is None
    (we don't fall back to raw or fabricate a number)."""
    before = _write_run(tmp_path, "baseline-legacy", manifest_overrides={
        "overall_pct": 10.0,
        # No clean_overall_pct.
    })
    after = _write_run(tmp_path, "baseline-modern", manifest_overrides={
        "overall_pct": 15.0,
        "clean_overall_pct": 42.0,
    })
    result = cr.compare_runs(before, after)
    assert result.before_clean_pct is None
    assert result.after_clean_pct == 42.0
    assert result.clean_pct_delta_pp is None


def test_compare_runs_per_packet_transitions(tmp_path: Path):
    """A packet with 5 receipts exercising 5 distinct transitions
    classifies cleanly + the per-packet transition_counts reflect it."""
    before_rows = [
        # q1: passing in both → still_passing
        {"question_id": "q1", "grade": "full_match", "score_status": "counted"},
        # q2: failing in both → still_failing
        {"question_id": "q2", "grade": "no_match", "score_status": "counted"},
        # q3: was counted-fail → newly_passing in after
        {"question_id": "q3", "grade": "no_match", "score_status": "counted"},
        # q4: was counted-fail → newly_skipped in after
        {"question_id": "q4", "grade": "no_match", "score_status": "counted"},
        # q5: was skipped → un_skipped_passing in after
        {"question_id": "q5", "grade": "atlas_not_found",
         "score_status": "skipped_receipt_stale"},
    ]
    after_rows = [
        {"question_id": "q1", "grade": "full_match", "score_status": "counted"},
        {"question_id": "q2", "grade": "no_match", "score_status": "counted"},
        {"question_id": "q3", "grade": "full_match", "score_status": "counted"},
        {"question_id": "q4", "grade": "atlas_not_found",
         "score_status": "skipped_command_snapshot"},
        {"question_id": "q5", "grade": "full_match", "score_status": "counted"},
        # q6: appeared (only in after).
        {"question_id": "q6", "grade": "full_match", "score_status": "counted"},
    ]
    before = _write_run(
        tmp_path, "baseline-before",
        manifest_overrides={
            "per_packet_pct": {"p1": {"receipts": 5, "clean_pct": 20.0}},
        },
        packets={"p1": _packet_artifact(before_rows)},
    )
    after = _write_run(
        tmp_path, "baseline-after",
        manifest_overrides={
            "per_packet_pct": {"p1": {"receipts": 6, "clean_pct": 80.0}},
        },
        packets={"p1": _packet_artifact(after_rows)},
    )
    result = cr.compare_runs(before, after)
    assert len(result.per_packet) == 1
    pkt = result.per_packet[0]
    assert pkt.packet_slug == "p1"
    assert pkt.before_clean_pct == 20.0
    assert pkt.after_clean_pct == 80.0
    assert pkt.clean_pct_delta_pp == 60.0
    # Transition counts.
    c = pkt.transition_counts
    assert c[cr.TRANSITION_STILL_PASSING] == 1   # q1
    assert c[cr.TRANSITION_STILL_FAILING] == 1   # q2
    assert c[cr.TRANSITION_NEWLY_PASSING] == 1   # q3
    assert c[cr.TRANSITION_NEWLY_SKIPPED] == 1   # q4
    assert c[cr.TRANSITION_UN_SKIPPED_PASSING] == 1  # q5
    assert c[cr.TRANSITION_APPEARED] == 1  # q6
    # All other buckets at zero.
    assert c[cr.TRANSITION_NEWLY_FAILING] == 0
    assert c[cr.TRANSITION_DISAPPEARED] == 0
    # Run-level rollup sums per-packet counts.
    assert result.transition_counts[cr.TRANSITION_NEWLY_PASSING] == 1
    assert result.transition_counts[cr.TRANSITION_APPEARED] == 1


def test_compare_runs_disappeared_receipt(tmp_path: Path):
    before_rows = [
        {"question_id": "q1", "grade": "full_match", "score_status": "counted"},
        {"question_id": "q2", "grade": "no_match", "score_status": "counted"},
    ]
    after_rows = [
        {"question_id": "q1", "grade": "full_match", "score_status": "counted"},
        # q2 missing in after.
    ]
    before = _write_run(
        tmp_path, "baseline-before",
        manifest_overrides={"per_packet_pct": {"p1": {"clean_pct": 50.0}}},
        packets={"p1": _packet_artifact(before_rows)},
    )
    after = _write_run(
        tmp_path, "baseline-after",
        manifest_overrides={"per_packet_pct": {"p1": {"clean_pct": 100.0}}},
        packets={"p1": _packet_artifact(after_rows)},
    )
    result = cr.compare_runs(before, after)
    pkt = result.per_packet[0]
    assert pkt.transition_counts[cr.TRANSITION_DISAPPEARED] == 1
    assert pkt.transition_counts[cr.TRANSITION_STILL_PASSING] == 1


def test_compare_runs_back_compat_legacy_row_no_score_status(tmp_path: Path):
    """A pre-PR-#14 baseline (rows lack score_status) diffed against a
    modern run classifies cleanly. The legacy row's missing
    score_status is interpreted as 'counted' so a no_match → full_match
    transition becomes newly_passing, not still_skipped."""
    before_rows = [
        {"question_id": "q1", "grade": "no_match"},  # no score_status
    ]
    after_rows = [
        {"question_id": "q1", "grade": "full_match", "score_status": "counted"},
    ]
    before = _write_run(
        tmp_path, "baseline-legacy",
        manifest_overrides={"per_packet_pct": {"p1": {}}},
        packets={"p1": _packet_artifact(before_rows)},
    )
    after = _write_run(
        tmp_path, "baseline-modern",
        manifest_overrides={"per_packet_pct": {"p1": {}}},
        packets={"p1": _packet_artifact(after_rows)},
    )
    result = cr.compare_runs(before, after)
    assert result.per_packet[0].transition_counts[
        cr.TRANSITION_NEWLY_PASSING
    ] == 1


def test_compare_runs_evidence_type_delta(tmp_path: Path):
    """Run-level by_evidence_type_delta reflects per-bucket changes."""
    before_bd = {
        "source_excerpt": {"receipts": 10, "correct": 2, "excluded": 0,
                            "clean_total": 10, "clean_pct": 20.0},
        "external_tool_docs": {"receipts": 5, "correct": 0, "excluded": 0,
                                "clean_total": 5, "clean_pct": 0.0},
        "user_context": {"receipts": 0, "correct": 0, "excluded": 0,
                          "clean_total": 0, "clean_pct": None},
        "absence_search": {"receipts": 0, "correct": 0, "excluded": 0,
                            "clean_total": 0, "clean_pct": None},
        "other": {"receipts": 0, "correct": 0, "excluded": 0,
                   "clean_total": 0, "clean_pct": None},
    }
    after_bd = {
        "source_excerpt": {"receipts": 10, "correct": 7, "excluded": 0,
                            "clean_total": 10, "clean_pct": 70.0},
        # external_tool_docs now fully excluded — clean_pct None
        "external_tool_docs": {"receipts": 5, "correct": 0, "excluded": 5,
                                "clean_total": 0, "clean_pct": None},
        "user_context": {"receipts": 0, "correct": 0, "excluded": 0,
                          "clean_total": 0, "clean_pct": None},
        "absence_search": {"receipts": 0, "correct": 0, "excluded": 0,
                            "clean_total": 0, "clean_pct": None},
        "other": {"receipts": 0, "correct": 0, "excluded": 0,
                   "clean_total": 0, "clean_pct": None},
    }
    before = _write_run(tmp_path, "baseline-before", manifest_overrides={
        "total_by_evidence_type": before_bd,
    })
    after = _write_run(tmp_path, "baseline-after", manifest_overrides={
        "total_by_evidence_type": after_bd,
    })
    result = cr.compare_runs(before, after)
    se = result.by_evidence_type_delta["source_excerpt"]
    assert se["before_clean_pct"] == 20.0
    assert se["after_clean_pct"] == 70.0
    assert se["delta_pp"] == 50.0
    etd = result.by_evidence_type_delta["external_tool_docs"]
    assert etd["before_clean_pct"] == 0.0
    assert etd["after_clean_pct"] is None  # all excluded
    assert etd["delta_pp"] is None


def test_compare_runs_legacy_manifest_no_evidence_breakdown(tmp_path: Path):
    """A baseline lacking ``total_by_evidence_type`` (pre-evidence-
    breakdown manifest) produces an empty by_evidence_type_delta
    dict rather than raising."""
    before = _write_run(tmp_path, "baseline-before")
    after = _write_run(tmp_path, "baseline-after")
    result = cr.compare_runs(before, after)
    assert result.by_evidence_type_delta == {}
    # Same back-compat contract for by_lane_delta.
    assert result.by_lane_delta == {}


def test_compare_runs_by_lane_delta(tmp_path: Path):
    """Run-level by_lane_delta reflects per-lane clean-pct changes.
    Mirror of the by_evidence_type delta test. The whole point of
    this rollup is so a lane-specific fix (e.g. Codex's doc_resolver
    work) shows movement in exactly one bucket — this test pins that
    semantic."""
    before_bd = {
        "explicit_source_fast_path": {
            "receipts": 10, "correct": 5, "excluded": 0,
            "clean_total": 10, "clean_pct": 50.0,
        },
        # doc_resolver lane was failing pre-fix.
        "doc_resolver": {
            "receipts": 5, "correct": 1, "excluded": 0,
            "clean_total": 5, "clean_pct": 20.0,
        },
        "fuzzy_find_code": {
            "receipts": 0, "correct": 0, "excluded": 0,
            "clean_total": 0, "clean_pct": None,
        },
        "scan_search": {
            "receipts": 0, "correct": 0, "excluded": 0,
            "clean_total": 0, "clean_pct": None,
        },
        "non_retrieval": {
            "receipts": 0, "correct": 0, "excluded": 0,
            "clean_total": 0, "clean_pct": None,
        },
        "other": {
            "receipts": 0, "correct": 0, "excluded": 0,
            "clean_total": 0, "clean_pct": None,
        },
    }
    after_bd = {
        # fast_path lane unchanged.
        "explicit_source_fast_path": {
            "receipts": 10, "correct": 5, "excluded": 0,
            "clean_total": 10, "clean_pct": 50.0,
        },
        # doc_resolver lane moved up after Codex's fix landed.
        "doc_resolver": {
            "receipts": 5, "correct": 4, "excluded": 0,
            "clean_total": 5, "clean_pct": 80.0,
        },
        "fuzzy_find_code": {
            "receipts": 0, "correct": 0, "excluded": 0,
            "clean_total": 0, "clean_pct": None,
        },
        "scan_search": {
            "receipts": 0, "correct": 0, "excluded": 0,
            "clean_total": 0, "clean_pct": None,
        },
        "non_retrieval": {
            "receipts": 0, "correct": 0, "excluded": 0,
            "clean_total": 0, "clean_pct": None,
        },
        "other": {
            "receipts": 0, "correct": 0, "excluded": 0,
            "clean_total": 0, "clean_pct": None,
        },
    }
    before = _write_run(tmp_path, "baseline-before", manifest_overrides={
        "total_by_lane": before_bd,
    })
    after = _write_run(tmp_path, "baseline-after", manifest_overrides={
        "total_by_lane": after_bd,
    })
    result = cr.compare_runs(before, after)
    # fast_path stayed flat — its delta is zero.
    fp = result.by_lane_delta["explicit_source_fast_path"]
    assert fp["before_clean_pct"] == 50.0
    assert fp["after_clean_pct"] == 50.0
    assert fp["delta_pp"] == 0.0
    # doc_resolver lane moved +60pp — exactly the attribution the
    # rollup is supposed to surface.
    dr = result.by_lane_delta["doc_resolver"]
    assert dr["before_clean_pct"] == 20.0
    assert dr["after_clean_pct"] == 80.0
    assert dr["delta_pp"] == 60.0


def test_compare_runs_missing_manifest_raises(tmp_path: Path):
    """A run_dir with no manifest.json is an operator error — raise
    rather than fabricate an empty diff."""
    (tmp_path / "baseline-empty").mkdir()
    other = _write_run(tmp_path, "baseline-other")
    with pytest.raises(FileNotFoundError):
        cr.compare_runs(tmp_path / "baseline-empty", other)


# ─── render_markdown / render_json smoke tests ────────────────────────


def test_render_markdown_includes_run_level_deltas(tmp_path: Path):
    """The markdown report carries the headline numbers + tables for
    skip categories and per-packet detail."""
    before_rows = [
        {"question_id": "q1", "grade": "no_match", "score_status": "counted"},
    ]
    after_rows = [
        {"question_id": "q1", "grade": "full_match", "score_status": "counted"},
    ]
    before = _write_run(
        tmp_path, "baseline-before",
        manifest_overrides={
            "overall_pct": 10.0, "clean_overall_pct": 20.0,
            "total_skipped_command_snapshot": 0,
            "per_packet_pct": {"p1": {"clean_pct": 0.0}},
        },
        packets={"p1": _packet_artifact(before_rows)},
    )
    after = _write_run(
        tmp_path, "baseline-after",
        manifest_overrides={
            "overall_pct": 30.0, "clean_overall_pct": 60.0,
            "total_skipped_command_snapshot": 3,
            "per_packet_pct": {"p1": {"clean_pct": 100.0}},
        },
        packets={"p1": _packet_artifact(after_rows)},
    )
    result = cr.compare_runs(before, after)
    md = cr.render_markdown(result)
    # Run-level delta table.
    assert "# Atlas shadow-mode — probe comparison" in md
    assert "baseline-before" in md
    assert "baseline-after" in md
    assert "Raw %" in md
    assert "Clean %" in md
    # Skip-category section surfaces the command_snapshot delta.
    assert "skipped_command_snapshot" in md
    # Transition section surfaces newly_passing for q1.
    assert "newly_passing" in md
    # Per-packet detail.
    assert "p1" in md


def test_render_json_is_parseable_and_has_per_packet(tmp_path: Path):
    """JSON renderer produces parseable JSON with the run + per-packet
    structure that downstream tooling expects."""
    before_rows = [
        {"question_id": "q1", "grade": "no_match", "score_status": "counted"},
    ]
    after_rows = [
        {"question_id": "q1", "grade": "full_match", "score_status": "counted"},
    ]
    before = _write_run(
        tmp_path, "baseline-before",
        manifest_overrides={"per_packet_pct": {"p1": {"clean_pct": 0.0}}},
        packets={"p1": _packet_artifact(before_rows)},
    )
    after = _write_run(
        tmp_path, "baseline-after",
        manifest_overrides={"per_packet_pct": {"p1": {"clean_pct": 100.0}}},
        packets={"p1": _packet_artifact(after_rows)},
    )
    result = cr.compare_runs(before, after)
    payload = json.loads(cr.render_json(result))
    assert payload["before_run_name"] == "baseline-before"
    assert payload["after_run_name"] == "baseline-after"
    assert payload["transition_counts"]["newly_passing"] == 1
    assert len(payload["per_packet"]) == 1
    pkt = payload["per_packet"][0]
    assert pkt["packet_slug"] == "p1"
    # Per-receipt transitions are inlined for downstream consumers.
    assert len(pkt["transitions"]) == 1
    assert pkt["transitions"][0]["transition"] == "newly_passing"


def test_write_reports_writes_both_files(tmp_path: Path):
    """write_reports writes comparison-report.md + .json under
    output_dir, creating it if absent."""
    before = _write_run(tmp_path, "baseline-before")
    after = _write_run(tmp_path, "baseline-after")
    result = cr.compare_runs(before, after)
    out_dir = tmp_path / "comparison-out"
    md_path, json_path = cr.write_reports(result, out_dir)
    assert md_path.is_file()
    assert json_path.is_file()
    assert md_path.name == "comparison-report.md"
    assert json_path.name == "comparison-report.json"
    # JSON is parseable.
    json.loads(json_path.read_text())
    # MD is non-empty.
    assert "probe comparison" in md_path.read_text()
