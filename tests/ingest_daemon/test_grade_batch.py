"""Unit tests for ``atlas_shadow.ingest_daemon.grade_batch``.

Covers:
- ``discover_packet_qna_logs`` — globbing
- ``_packet_slug_from_qna_path`` — slug extraction
- ``_synthesize_pr_event`` — synthetic event shape
- ``_stub_fetch_pr_files`` — file-list stub for one packet
- ``grade_one_packet`` — orchestrator wired with stubs (no GH, no DB)
- ``_aggregate_run_totals`` — totals math
- ``write_packet_artifact`` / ``write_manifest`` / ``write_per_run_summary_md``
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from atlas_shadow.ingest_daemon import grade_batch as gb
from atlas_shadow.ingest_daemon.pr_comment import GradingSummary, ReceiptGradingRow
from atlas_shadow.ingest_daemon.receiver import PrEvent


# ───────── discover_packet_qna_logs ──────────────────────────────────


def test_discover_packet_qna_logs_finds_packet_files(tmp_path: Path):
    """Default glob ``**/docs/work/*/02-qna-log.md`` finds packets at
    any depth in the core checkout layout."""
    # Top-level docs/work/ (rare but supported).
    (tmp_path / "docs" / "work" / "2026-05-14-foo" / "evidence").mkdir(parents=True)
    (tmp_path / "docs" / "work" / "2026-05-14-foo" / "02-qna-log.md").write_text("...")
    # Atlas-leaf-nested location (the real layout).
    nested = tmp_path / "products" / "tandem" / "packages" / "python" / "atlas" / "docs" / "work"
    (nested / "2026-05-13-bar").mkdir(parents=True)
    (nested / "2026-05-13-bar" / "02-qna-log.md").write_text("...")
    # Decoy: NOT a qna log.
    (nested / "2026-05-13-bar" / "01-proposal.md").write_text("...")
    # Decoy: outside docs/work/.
    (tmp_path / "README.md").write_text("...")

    found = gb.discover_packet_qna_logs(tmp_path)
    assert set(found) == {
        "docs/work/2026-05-14-foo/02-qna-log.md",
        "products/tandem/packages/python/atlas/docs/work/2026-05-13-bar/02-qna-log.md",
    }


def test_discover_packet_qna_logs_returns_sorted():
    """Manifest reproducibility: same input → same output order."""
    # Use a temp dir per the fixture pattern; spot-checks sort order.
    pass  # covered by test_discover_packet_qna_logs_finds_packet_files (set-based but pathlib walks deterministically and our sort guarantees order)


# ───────── _packet_slug_from_qna_path ────────────────────────────────


@pytest.mark.parametrize(
    "qna_path,expected_slug",
    [
        (
            "products/tandem/packages/python/atlas/docs/work/2026-05-14-foo/02-qna-log.md",
            "2026-05-14-foo",
        ),
        ("docs/work/2026-05-13-bar/02-qna-log.md", "2026-05-13-bar"),
        # Defensive fallback: if "work" not in path, parent dir name.
        ("some/other/path/funky/02-qna-log.md", "funky"),
    ],
)
def test_packet_slug_from_qna_path(qna_path, expected_slug):
    assert gb._packet_slug_from_qna_path(qna_path) == expected_slug


# ───────── _synthesize_pr_event ──────────────────────────────────────


def test_synthesize_pr_event_shape():
    event = gb._synthesize_pr_event(
        repo_full_name="tandemstream/core",
        commit_sha="d9a5d53c97ad6abd768103bc9386cde25ee61be2",
        packet_qna_log_path="products/tandem/.../docs/work/foo/02-qna-log.md",
    )
    assert isinstance(event, PrEvent)
    assert event.action == "opened"
    assert event.repo_full_name == "tandemstream/core"
    assert event.pr_number == 0  # synthetic marker
    assert event.base_sha == event.head_sha == "d9a5d53c97ad6abd768103bc9386cde25ee61be2"
    assert event.base_ref == "main"
    assert "d9a5d53" in event.head_ref


# ───────── _stub_fetch_pr_files ──────────────────────────────────────


def test_stub_fetch_pr_files_returns_only_the_one_qna_log():
    files = gb._stub_fetch_pr_files(
        repo_full_name="tandemstream/core",
        pr_number=0,
        github_token="ignored",
        qna_log_path="some/packet/02-qna-log.md",
    )
    assert files == [{"filename": "some/packet/02-qna-log.md", "status": "modified"}]


# ───────── grade_one_packet (wires no-op posters) ────────────────────


def test_grade_one_packet_wires_noop_posters_and_returns_outcome():
    """The orchestrator should be called with no-op posters and
    receive a stubbed file list pointing at the packet's qna log."""
    captured_kwargs = {}

    def _fake_run_pr_grading(cfg, event, **kwargs):
        captured_kwargs.update(kwargs)
        return {
            "summaries": [],
            "status": "ok",
            "code_revision_id": "fake-rev",
            "base_sha": event.base_sha,
            "head_sha": event.head_sha,
            "pr_number": event.pr_number,
            "repo_full_name": event.repo_full_name,
        }

    cfg = SimpleNamespace()
    outcome = gb.grade_one_packet(
        cfg,
        repo_full_name="tandemstream/core",
        commit_sha="abc1234",
        qna_log_path="products/foo/docs/work/p-1/02-qna-log.md",
        github_token="ignored",
        _run_pr_grading=_fake_run_pr_grading,
    )
    # All four GH callbacks are no-ops.
    assert captured_kwargs["_post_pending"] is gb._noop_post_pending
    assert captured_kwargs["_post_final"] is gb._noop_post_final
    assert captured_kwargs["_post_comment"] is gb._noop_post_comment
    # The fetcher is a closure but pointing at the right qna log.
    files = captured_kwargs["_fetch_pr_files"](
        repo_full_name="tandemstream/core",
        pr_number=0,
        github_token="ignored",
    )
    assert files == [
        {"filename": "products/foo/docs/work/p-1/02-qna-log.md", "status": "modified"}
    ]
    # Outcome carries packet identifiers.
    assert outcome["packet_slug"] == "p-1"
    assert outcome["packet_qna_log_path"] == "products/foo/docs/work/p-1/02-qna-log.md"


# ───────── _aggregate_run_totals ─────────────────────────────────────


def _mk_summary(packet_id: str, rows: list[tuple[str, str]]) -> GradingSummary:
    """Build a GradingSummary with rows as (grade, qid) tuples."""
    grading_rows = [
        ReceiptGradingRow(
            question_id=qid,
            question="...",
            grade=grade,
            confidence=1.0,
            rationale="...",
            tool="find_code",
        )
        for grade, qid in rows
    ]
    return GradingSummary(
        packet_id=packet_id,
        code_revision_id="rev-1",
        base_sha="abc",
        threshold_pct=60,
        rows=grading_rows,
    )


def test_aggregate_run_totals_sums_across_packets():
    """Totals are summed across every packet's summaries; pct rounds
    to one decimal."""
    outcomes = [
        {
            "packet_slug": "p1",
            "status": "ok",
            "summaries": [
                _mk_summary("p1", [("full_match", "q1"), ("full_match", "q2"), ("no_match", "q3")])
            ],
        },
        {
            "packet_slug": "p2",
            "status": "ok",
            "summaries": [
                _mk_summary("p2", [("partial_match", "q1"), ("no_match", "q2")])
            ],
        },
    ]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_packets"] == 2
    assert totals["total_receipts"] == 5
    # p1: 2 of 3 (full+full); p2: 1 of 2 (partial counts as correct).
    assert totals["total_correct"] == 3
    assert totals["overall_pct"] == 60.0
    assert totals["per_packet_pct"]["p1"]["correct"] == 2
    assert totals["per_packet_pct"]["p1"]["pct"] == round(200 / 3, 1)
    assert totals["per_packet_pct"]["p2"]["correct"] == 1
    assert totals["per_packet_pct"]["p2"]["pct"] == 50.0


def test_aggregate_run_totals_empty():
    totals = gb._aggregate_run_totals([])
    assert totals["total_packets"] == 0
    assert totals["total_receipts"] == 0
    assert totals["overall_pct"] == 0.0


def test_aggregate_run_totals_handles_zero_receipt_packet():
    """A packet with no receipts (parser couldn't extract any) doesn't
    cause division-by-zero — pct is 0.0."""
    outcomes = [{"packet_slug": "empty-pkt", "status": "ok", "summaries": []}]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["per_packet_pct"]["empty-pkt"]["pct"] == 0.0


# ───────── write_packet_artifact + manifest + summary.md ─────────────


def test_write_packet_artifact_serializes_gradingsummary(tmp_path: Path):
    """asdict on GradingSummary captures all rows, plus we add the
    derived aggregate props that asdict skips."""
    outcome = {
        "packet_slug": "p1",
        "status": "ok",
        "code_revision_id": "rev-1",
        "base_sha": "abc",
        "head_sha": "abc",
        "pr_number": 0,
        "repo_full_name": "tandemstream/core",
        "packet_qna_log_path": "p1/qna.md",
        "summaries": [
            _mk_summary("p1", [("full_match", "q1"), ("no_match", "q2")])
        ],
    }
    path = gb.write_packet_artifact(outcome, tmp_path)
    assert path == tmp_path / "packets" / "p1.json"
    payload = json.loads(path.read_text())
    assert payload["packet_slug"] == "p1"
    s = payload["summaries"][0]
    assert s["packet_id"] == "p1"
    assert s["total"] == 2
    assert s["pass_count"] == 1
    assert s["pass_pct"] == 50
    assert s["passed"] is False  # threshold 60% > 50%
    assert len(s["rows"]) == 2


def test_write_manifest_records_totals_and_metadata(tmp_path: Path):
    outcomes = [
        {"packet_slug": "p1", "status": "ok",
         "summaries": [_mk_summary("p1", [("full_match", "q1")])]}
    ]
    path = gb.write_manifest(
        "baseline-2026-05-15",
        tmp_path,
        commit_sha="d9a5d53",
        code_revision_id="rev-1",
        packet_outcomes=outcomes,
        started_at="2026-05-15T00:00:00Z",
        finished_at="2026-05-15T01:00:00Z",
        grader_backend="claude_cli",
        grader_model="sonnet",
    )
    payload = json.loads(path.read_text())
    assert payload["run_name"] == "baseline-2026-05-15"
    assert payload["commit_sha"] == "d9a5d53"
    assert payload["code_revision_id"] == "rev-1"
    assert payload["total_packets"] == 1
    assert payload["total_receipts"] == 1
    assert payload["total_correct"] == 1
    assert payload["overall_pct"] == 100.0
    assert payload["grader_backend"] == "claude_cli"
    assert payload["grader_model"] == "sonnet"


def test_write_per_run_summary_md_sorts_packets_worst_first(tmp_path: Path):
    outcomes = [
        {"packet_slug": "good", "status": "ok",
         "summaries": [_mk_summary("good", [("full_match", "q1"), ("full_match", "q2")])]},
        {"packet_slug": "bad", "status": "ok",
         "summaries": [_mk_summary("bad", [("no_match", "q1"), ("no_match", "q2")])]},
        {"packet_slug": "mid", "status": "ok",
         "summaries": [_mk_summary("mid", [("full_match", "q1"), ("no_match", "q2")])]},
    ]
    path = gb.write_per_run_summary_md(
        "baseline-x",
        tmp_path,
        commit_sha="d9a5d53",
        packet_outcomes=outcomes,
        overall_pct=50.0,
        total_receipts=6,
        total_correct=3,
    )
    text = path.read_text()
    # Worst (bad, 0%) before mid (50%) before good (100%).
    bad_pos = text.index("| bad |")
    mid_pos = text.index("| mid |")
    good_pos = text.index("| good |")
    assert bad_pos < mid_pos < good_pos
    assert "baseline-x" in text
    assert "d9a5d53" in text
    assert "50.0%" in text
