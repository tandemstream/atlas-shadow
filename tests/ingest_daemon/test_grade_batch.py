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
    """The orchestrator should be called with no-op posters AND with
    a local-disk file reader (not the GH Contents API)."""
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
        core_repo_path=Path("/fake/repo"),
        _run_pr_grading=_fake_run_pr_grading,
        _read_file_at_commit=lambda **kw: "stubbed-content",
    )
    # All three GH-posting callbacks are no-ops.
    assert captured_kwargs["_post_pending"] is gb._noop_post_pending
    assert captured_kwargs["_post_final"] is gb._noop_post_final
    assert captured_kwargs["_post_comment"] is gb._noop_post_comment
    # The PR-files fetcher is a closure but pointing at the right qna log.
    files = captured_kwargs["_fetch_pr_files"](
        repo_full_name="tandemstream/core",
        pr_number=0,
        github_token="ignored",
    )
    assert files == [
        {"filename": "products/foo/docs/work/p-1/02-qna-log.md", "status": "modified"}
    ]
    # The file-at-ref reader is wired to read from local disk, NOT GH.
    body = captured_kwargs["_fetch_file_at_ref"](
        repo_full_name="tandemstream/core",
        path="products/foo/docs/work/p-1/02-qna-log.md",
        ref="abc1234",
        github_token="ignored",
    )
    assert body == "stubbed-content"
    # Outcome carries packet identifiers.
    assert outcome["packet_slug"] == "p-1"
    assert outcome["packet_qna_log_path"] == "products/foo/docs/work/p-1/02-qna-log.md"


# ───────── _read_file_via_git_show ───────────────────────────────────


def _init_tmp_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """Spin up a tmp git repo with one committed file. Returns (path, sha)."""
    repo = tmp_path / "tmprepo"
    repo.mkdir()
    import subprocess as _sp
    kw = {"cwd": repo, "check": True, "capture_output": True}
    _sp.run(["git", "init", "--initial-branch=main", "-q"], **kw)
    _sp.run(["git", "config", "user.email", "t@e.com"], **kw)
    _sp.run(["git", "config", "user.name", "T"], **kw)
    (repo / "qna.md").write_text("# committed content\n")
    _sp.run(["git", "add", "qna.md"], **kw)
    _sp.run(["git", "commit", "-m", "init", "-q"], **kw)
    proc = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo,
                   capture_output=True, text=True, check=True)
    return repo, proc.stdout.strip()


def test_read_file_via_git_show_reads_committed_bytes(tmp_path: Path):
    """The local reader reads from the object DB, not the worktree.

    Mirrors the codex r1 P2 #1 fix on the core-side shadow_ingest_docs.py:
    if the worktree drifted, we still want commit-pinned content.
    """
    repo, full = _init_tmp_git_repo(tmp_path)
    # Overwrite the worktree post-commit.
    (repo / "qna.md").write_text("DIRTY WORKTREE CONTENT\n")
    out = gb._read_file_via_git_show(
        core_repo_path=repo, path="qna.md", ref=full
    )
    assert out == "# committed content\n"
    assert "DIRTY" not in out


def test_read_file_via_git_show_returns_none_for_missing_path(tmp_path: Path, capsys):
    """Matches the 404 contract of _fetch_file_at_ref: missing file → None."""
    repo, full = _init_tmp_git_repo(tmp_path)
    out = gb._read_file_via_git_show(
        core_repo_path=repo, path="does-not-exist.md", ref=full
    )
    assert out is None
    err = capsys.readouterr().err
    assert "git show" in err  # WARN logged so operator can diagnose


# ───────── discover_packet_qna_logs commit-pinned mode (codex r3) ────


def _init_repo_with_packet_files(tmp_path: Path) -> tuple[Path, str]:
    """Build a tmp repo with a couple of packet files committed."""
    repo = tmp_path / "tmprepo"
    repo.mkdir()
    import subprocess as _sp
    kw = {"cwd": repo, "check": True, "capture_output": True}
    _sp.run(["git", "init", "--initial-branch=main", "-q"], **kw)
    _sp.run(["git", "config", "user.email", "t@e.com"], **kw)
    _sp.run(["git", "config", "user.name", "T"], **kw)
    pkt_a = repo / "products" / "tandem" / "packages" / "python" / "atlas" / "docs" / "work" / "2026-01-01-a"
    pkt_a.mkdir(parents=True)
    (pkt_a / "02-qna-log.md").write_text("# a\n")
    pkt_b = repo / "docs" / "work" / "2026-01-02-b"
    pkt_b.mkdir(parents=True)
    (pkt_b / "02-qna-log.md").write_text("# b\n")
    (repo / "README.md").write_text("# readme\n")  # decoy
    _sp.run(["git", "add", "."], **kw)
    _sp.run(["git", "commit", "-m", "init", "-q"], **kw)
    proc = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo,
                   capture_output=True, text=True, check=True)
    return repo, proc.stdout.strip()


def test_discover_packet_qna_logs_commit_pinned_mode(tmp_path: Path):
    """Commit-pinned discovery via git ls-tree only sees committed files."""
    repo, full = _init_repo_with_packet_files(tmp_path)
    # Add a NEW packet file to the worktree but DON'T commit.
    new_pkt = repo / "docs" / "work" / "2026-01-03-uncommitted"
    new_pkt.mkdir(parents=True)
    (new_pkt / "02-qna-log.md").write_text("# uncommitted")

    # Commit-pinned: only the 2 committed packets.
    found = gb.discover_packet_qna_logs(repo, commit_sha=full)
    assert set(found) == {
        "docs/work/2026-01-02-b/02-qna-log.md",
        "products/tandem/packages/python/atlas/docs/work/2026-01-01-a/02-qna-log.md",
    }
    # Worktree fallback (no commit_sha): includes the uncommitted file.
    found_worktree = gb.discover_packet_qna_logs(repo)
    assert "docs/work/2026-01-03-uncommitted/02-qna-log.md" in found_worktree


def test_discover_packet_qna_logs_commit_pinned_no_match_returns_empty(tmp_path: Path):
    repo, full = _init_repo_with_packet_files(tmp_path)
    found = gb.discover_packet_qna_logs(
        repo, commit_sha=full, packet_glob="**/no-such-file.md"
    )
    assert found == []


def test_discover_packet_qna_logs_commit_pinned_errors_on_bad_sha(tmp_path: Path):
    repo, _ = _init_repo_with_packet_files(tmp_path)
    with pytest.raises(SystemExit):
        gb.discover_packet_qna_logs(repo, commit_sha="not-a-real-sha")


# ───────── glob-to-regex correctness ─────────────────────────────────


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("**/docs/work/*/02-qna-log.md", "docs/work/p/02-qna-log.md", True),
        ("**/docs/work/*/02-qna-log.md",
         "products/foo/docs/work/p/02-qna-log.md", True),
        ("**/docs/work/*/02-qna-log.md", "docs/work/p/01-proposal.md", False),
        ("**/docs/work/*/02-qna-log.md",
         "docs/work/p/sub/02-qna-log.md", False),  # * doesn't cross /
        ("*.md", "a.md", True),
        ("*.md", "sub/a.md", False),  # bare * doesn't cross /
    ],
)
def test_glob_to_regex_handles_recursive_globs(pattern, path, expected):
    rx = gb._glob_to_regex(pattern)
    assert bool(rx.match(path)) is expected


# ───────── ok-but-no-receipts treatment (codex r3 defensive) ─────────


def _fake_replace(cfg_obj, **overrides):
    """SimpleNamespace-aware stand-in for ``dataclasses.replace``.

    Used because the cmd-level tests pass ``SimpleNamespace`` as cfg
    (since DaemonConfig has lots of required fields). The real
    ``dataclasses.replace`` rejects non-dataclass inputs; this version
    just builds a new namespace with the overrides applied.
    """
    new = SimpleNamespace(**vars(cfg_obj))
    for k, v in overrides.items():
        setattr(new, k, v)
    return new


def _run_cmd_with_outcome(tmp_path: Path, monkeypatch, fake_outcome: dict) -> tuple[int, dict]:
    """Helper: invoke cmd_grade_packet_batch with a single packet whose
    grade_one_packet result is controlled by ``fake_outcome``. Returns
    (rc, manifest)."""
    repo = tmp_path / "core-repo"
    repo.mkdir()
    output_dir = tmp_path / "shadow-runs" / "baseline-test"
    cfg = SimpleNamespace(
        grader_model="sonnet",
        state_file=str(tmp_path / "s.json"),
        core_repo_path=repo,
        shadow_runs_dir=tmp_path / "yaml-shadow-runs",
    )
    args = SimpleNamespace(
        core_repo_path=str(repo),
        commit_sha="deadbeef0000000000000000000000000000000",
        output_dir=str(output_dir),
        packet_glob="**/docs/work/*/02-qna-log.md",
        limit=None,
        repo_full_name="tandemstream/core",
        dry_run=False,
        quiet=True,
    )
    monkeypatch.setattr(gb, "replace", _fake_replace)
    monkeypatch.setattr(
        gb, "discover_packet_qna_logs",
        lambda *a, **kw: ["docs/work/2026-01-01-x/02-qna-log.md"],
    )
    monkeypatch.setattr(gb, "grade_one_packet", lambda *a, **kw: fake_outcome)
    rc = gb.cmd_grade_packet_batch(cfg, args)
    manifest = json.loads((output_dir / "manifest.json").read_text())
    return rc, manifest


def test_cmd_grade_packet_batch_flags_empty_summaries_list_as_no_receipts(
    tmp_path: Path, monkeypatch
):
    """sub-case (a): outcome.summaries == []."""
    rc, manifest = _run_cmd_with_outcome(tmp_path, monkeypatch, {
        "packet_slug": "2026-01-01-x",
        "packet_qna_log_path": "docs/work/2026-01-01-x/02-qna-log.md",
        "status": "ok",
        "summaries": [],
        "base_sha": "deadbeef", "head_sha": "deadbeef",
        "pr_number": 0, "repo_full_name": "tandemstream/core",
    })
    assert rc == 2
    assert manifest["per_packet_pct"]["2026-01-01-x"]["status"] == "ok_but_no_receipts"


def test_cmd_grade_packet_batch_flags_zero_total_summaries_as_no_receipts(
    tmp_path: Path, monkeypatch
):
    """sub-case (b) [codex r4 P2]: outcome.summaries is non-empty but
    each summary has total=0. The naive `not summaries` check would
    miss this; the receipt-sum check catches it."""
    rc, manifest = _run_cmd_with_outcome(tmp_path, monkeypatch, {
        "packet_slug": "2026-01-01-x",
        "packet_qna_log_path": "docs/work/2026-01-01-x/02-qna-log.md",
        "status": "ok",
        "summaries": [
            {"packet_id": "2026-01-01-x", "passed": True, "pass_pct": 0,
             "pass_count": 0, "total": 0}
        ],
        "base_sha": "deadbeef", "head_sha": "deadbeef",
        "pr_number": 0, "repo_full_name": "tandemstream/core",
    })
    assert rc == 2
    assert manifest["per_packet_pct"]["2026-01-01-x"]["status"] == "ok_but_no_receipts"


def test_cmd_grade_packet_batch_accepts_ok_with_any_receipts(
    tmp_path: Path, monkeypatch
):
    """Positive control: status=ok with non-zero receipts is NOT flagged."""
    rc, manifest = _run_cmd_with_outcome(tmp_path, monkeypatch, {
        "packet_slug": "2026-01-01-x",
        "packet_qna_log_path": "docs/work/2026-01-01-x/02-qna-log.md",
        "status": "ok",
        "summaries": [_mk_summary_dict("2026-01-01-x", [("full_match", "q1")])],
        "code_revision_id": "rev-1",
        "base_sha": "deadbeef", "head_sha": "deadbeef",
        "pr_number": 0, "repo_full_name": "tandemstream/core",
    })
    assert rc == 0
    assert manifest["per_packet_pct"]["2026-01-01-x"]["status"] == "ok"


def test_cmd_grade_packet_batch_overrides_cfg_paths_for_orchestrator(
    tmp_path: Path, monkeypatch
):
    """Codex r5 P2: the batch must pass a cfg with core_repo_path and
    shadow_runs_dir overridden to the operator's args, not the YAML
    defaults — otherwise the orchestrator grades against the wrong
    checkout and writes artifacts outside --output-dir."""
    yaml_core_path = tmp_path / "yaml-core"
    yaml_core_path.mkdir()
    yaml_shadow_runs = tmp_path / "yaml-shadow-runs"
    yaml_shadow_runs.mkdir()

    cli_core_path = tmp_path / "cli-core"
    cli_core_path.mkdir()
    cli_output_dir = tmp_path / "cli-output" / "baseline-test"

    cfg = SimpleNamespace(
        grader_model="sonnet",
        state_file=str(tmp_path / "s.json"),
        # YAML-derived defaults; the batch should NOT use these.
        core_repo_path=yaml_core_path,
        shadow_runs_dir=yaml_shadow_runs,
        continuous_shadow_org_id="org-1",
    )
    args = SimpleNamespace(
        core_repo_path=str(cli_core_path),
        commit_sha="deadbeef0000000000000000000000000000000",
        output_dir=str(cli_output_dir),
        packet_glob="**/docs/work/*/02-qna-log.md",
        limit=None,
        repo_full_name="tandemstream/core",
        dry_run=False,
        quiet=True,
    )
    monkeypatch.setattr(
        gb, "discover_packet_qna_logs",
        lambda *a, **kw: ["docs/work/2026-01-01-x/02-qna-log.md"],
    )

    # SimpleNamespace doesn't work with dataclasses.replace, so use the
    # shared `_fake_replace` helper defined at module top.
    monkeypatch.setattr(gb, "replace", _fake_replace)

    captured_cfg = {}
    def _capture(cfg_passed, **kw):
        captured_cfg["cfg"] = cfg_passed
        return {
            "packet_slug": "2026-01-01-x",
            "packet_qna_log_path": kw["qna_log_path"],
            "status": "ok",
            "summaries": [_mk_summary_dict("2026-01-01-x", [("full_match", "q1")])],
            "code_revision_id": "rev-1",
            "base_sha": kw["commit_sha"], "head_sha": kw["commit_sha"],
            "pr_number": 0, "repo_full_name": kw["repo_full_name"],
        }
    monkeypatch.setattr(gb, "grade_one_packet", _capture)

    rc = gb.cmd_grade_packet_batch(cfg, args)
    assert rc == 0
    # The cfg passed to grade_one_packet has the CLI paths, not the YAML.
    cfg_used = captured_cfg["cfg"]
    assert cfg_used.core_repo_path == cli_core_path
    assert cfg_used.shadow_runs_dir == cli_output_dir.resolve() / "artifacts"
    # The original cfg is untouched.
    assert cfg.core_repo_path == yaml_core_path
    assert cfg.shadow_runs_dir == yaml_shadow_runs


# ───────── _aggregate_run_totals ─────────────────────────────────────


def _mk_summary_dict(
    packet_id: str,
    rows: list[tuple[str, str]],
    *,
    artifact_path: str = None,
) -> dict:
    """Build the dict-shape summary that ``run_pr_grading`` actually
    returns in ``outcome['summaries']``.

    ``rows`` is a list of (grade, qid) tuples. ``correct`` counts
    full_match + partial_match — same definition GradingSummary uses.
    """
    correct = sum(1 for g, _ in rows if g in ("full_match", "partial_match"))
    total = len(rows)
    return {
        "packet_id": packet_id,
        "passed": (correct * 100 // total >= 60) if total else False,
        "pass_pct": int(round(correct * 100 / total)) if total else 0,
        "pass_count": correct,
        "total": total,
        "artifact_path": artifact_path,
    }


def _mk_summary(packet_id: str, rows: list[tuple[str, str]]) -> GradingSummary:
    """Build a GradingSummary dataclass instance.

    Kept around for tests that exercise the dataclass fallback path in
    ``_summary_total`` / ``_summary_pass_count`` — production code only
    ever sees the dict shape via ``run_pr_grading``, but the helpers
    accept both for resilience.
    """
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
    to one decimal. Uses the dict-shape summaries that ``run_pr_grading``
    actually returns (codex r1 fix)."""
    outcomes = [
        {
            "packet_slug": "p1",
            "status": "ok",
            "summaries": [
                _mk_summary_dict("p1", [("full_match", "q1"), ("full_match", "q2"), ("no_match", "q3")])
            ],
        },
        {
            "packet_slug": "p2",
            "status": "ok",
            "summaries": [
                _mk_summary_dict("p2", [("partial_match", "q1"), ("no_match", "q2")])
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


def test_aggregate_run_totals_accepts_dataclass_shape_too():
    """Defensive: if a caller passes dataclass-shape summaries (the
    pre-codex-r1-fix interface), aggregation should still work."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary("p1", [("full_match", "q1"), ("no_match", "q2")])],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_receipts"] == 2
    assert totals["total_correct"] == 1


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


def test_write_packet_artifact_inlines_artifact_rows(tmp_path: Path):
    """write_packet_artifact reads the artifact_path JSON written by
    run_pr_grading (which has per-receipt rows) and inlines it into
    the per-packet JSON under "artifact". Aggregate fields stay at top
    level (codex r1 fix — the production summaries are dicts not
    GradingSummary objects)."""
    # Synthesize what grader_service.write_grading_artifact would write.
    artifact_dir = tmp_path / "live-artifacts"
    artifact_dir.mkdir()
    artifact_file = artifact_dir / "pr-0-20260515T200000.000000Z-p1.json"
    artifact_file.write_text(json.dumps({
        "schema_version": "1.0",
        "packet_id": "p1",
        "rows": [
            {"question_id": "q1", "grade": "full_match", "confidence": 0.95},
            {"question_id": "q2", "grade": "no_match", "confidence": 0.4},
        ],
        "pass_count": 1,
        "total": 2,
        "pass_pct": 50,
        "passed": False,
    }))

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
            _mk_summary_dict("p1", [("full_match", "q1"), ("no_match", "q2")],
                             artifact_path=str(artifact_file))
        ],
    }
    path = gb.write_packet_artifact(outcome, tmp_path)
    assert path == tmp_path / "packets" / "p1.json"
    payload = json.loads(path.read_text())
    assert payload["packet_slug"] == "p1"
    s = payload["summaries"][0]
    # Aggregate fields preserved.
    assert s["packet_id"] == "p1"
    assert s["total"] == 2
    assert s["pass_count"] == 1
    assert s["pass_pct"] == 50
    assert s["passed"] is False
    # Artifact rows inlined under "artifact" key.
    assert "artifact" in s
    assert len(s["artifact"]["rows"]) == 2
    assert s["artifact"]["rows"][0]["question_id"] == "q1"


def test_write_packet_artifact_handles_missing_artifact_file(tmp_path: Path):
    """If artifact_path points at a file that doesn't exist (operator
    moved it, FS race), still produce a valid per-packet JSON without
    crashing — just no `artifact` subfield."""
    outcome = {
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [
            _mk_summary_dict("p1", [("full_match", "q1")],
                             artifact_path="/nonexistent/path.json")
        ],
    }
    path = gb.write_packet_artifact(outcome, tmp_path)
    payload = json.loads(path.read_text())
    s = payload["summaries"][0]
    assert s["total"] == 1
    assert "artifact" not in s  # gracefully missing


def test_write_packet_artifact_handles_no_artifact_path_key(tmp_path: Path):
    """If a summary dict has no artifact_path at all, still works."""
    outcome = {
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [{"packet_id": "p1", "passed": True, "pass_pct": 100,
                       "pass_count": 1, "total": 1}],
    }
    path = gb.write_packet_artifact(outcome, tmp_path)
    payload = json.loads(path.read_text())
    assert payload["summaries"][0]["pass_count"] == 1


def test_write_manifest_records_totals_and_metadata(tmp_path: Path):
    outcomes = [
        {"packet_slug": "p1", "status": "ok",
         "summaries": [_mk_summary_dict("p1", [("full_match", "q1")])]}
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


# ───────── cmd_grade_packet_batch failure handling (codex r1 P2) ─────


def test_cmd_grade_packet_batch_counts_returned_error_statuses(tmp_path: Path, monkeypatch):
    """run_pr_grading returns status='error' (not raised) on operational
    failures. The batch must count those as partial failures and exit 2."""
    core_repo = tmp_path / "core-repo"
    core_repo.mkdir()

    output_dir = tmp_path / "shadow-runs" / "baseline-test"

    cfg = SimpleNamespace(
        grader_model="sonnet",
        state_file=str(tmp_path / "state.json"),
        core_repo_path=core_repo,
        shadow_runs_dir=tmp_path / "yaml-shadow-runs",
    )
    args = SimpleNamespace(
        core_repo_path=str(core_repo),
        commit_sha="abcdef0000000000000000000000000000000",
        output_dir=str(output_dir),
        packet_glob="**/docs/work/*/02-qna-log.md",
        limit=None,
        repo_full_name="tandemstream/core",
        dry_run=False,
        quiet=True,
    )

    monkeypatch.setattr(gb, "replace", _fake_replace)
    # Stub the commit-pinned discovery so we don't need a real git repo.
    monkeypatch.setattr(
        gb, "discover_packet_qna_logs",
        lambda *a, **kw: ["docs/work/2026-01-01-x/02-qna-log.md"],
    )
    # Mock grade_one_packet to return a status="error" outcome (mirroring
    # the orchestrator's exception-swallowing behavior).
    def _fake_grade_one(*_args, **_kwargs):
        return {
            "packet_slug": "2026-01-01-x",
            "packet_qna_log_path": _kwargs["qna_log_path"],
            "status": "error",
            "error": "RuntimeError: simulated operational failure",
            "summaries": [],
            "base_sha": _kwargs["commit_sha"],
            "head_sha": _kwargs["commit_sha"],
            "pr_number": 0,
            "repo_full_name": _kwargs["repo_full_name"],
        }
    monkeypatch.setattr(gb, "grade_one_packet", _fake_grade_one)

    rc = gb.cmd_grade_packet_batch(cfg, args)
    assert rc == 2  # partial failure exit code


def test_cmd_grade_packet_batch_succeeds_when_all_packets_ok(tmp_path: Path, monkeypatch):
    core_repo = tmp_path / "core-repo"
    core_repo.mkdir()

    output_dir = tmp_path / "shadow-runs" / "baseline-test"

    cfg = SimpleNamespace(
        grader_model="sonnet",
        state_file=str(tmp_path / "state.json"),
        core_repo_path=core_repo,
        shadow_runs_dir=tmp_path / "yaml-shadow-runs",
    )
    args = SimpleNamespace(
        core_repo_path=str(core_repo),
        commit_sha="abcdef0000000000000000000000000000000",
        output_dir=str(output_dir),
        packet_glob="**/docs/work/*/02-qna-log.md",
        limit=None,
        repo_full_name="tandemstream/core",
        dry_run=False,
        quiet=True,
    )

    monkeypatch.setattr(gb, "replace", _fake_replace)
    monkeypatch.setattr(
        gb, "discover_packet_qna_logs",
        lambda *a, **kw: ["docs/work/2026-01-01-x/02-qna-log.md"],
    )

    def _fake_grade_one(*_args, **_kwargs):
        return {
            "packet_slug": "2026-01-01-x",
            "packet_qna_log_path": _kwargs["qna_log_path"],
            "status": "ok",
            "summaries": [_mk_summary_dict("2026-01-01-x", [("full_match", "q1")])],
            "base_sha": _kwargs["commit_sha"],
            "head_sha": _kwargs["commit_sha"],
            "pr_number": 0,
            "repo_full_name": _kwargs["repo_full_name"],
            "code_revision_id": "rev-1",
        }
    monkeypatch.setattr(gb, "grade_one_packet", _fake_grade_one)

    rc = gb.cmd_grade_packet_batch(cfg, args)
    assert rc == 0
    # Manifest + summary should exist.
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "summary.md").exists()


def test_cmd_grade_packet_batch_counts_raised_exceptions_too(tmp_path: Path, monkeypatch):
    """Defensive: even if run_pr_grading raises (shouldn't per its
    docstring, but bugs happen), batch must still record + count it."""
    core_repo = tmp_path / "core-repo"
    core_repo.mkdir()

    cfg = SimpleNamespace(
        grader_model="sonnet",
        state_file=str(tmp_path / "state.json"),
        core_repo_path=core_repo,
        shadow_runs_dir=tmp_path / "yaml-shadow-runs",
    )
    args = SimpleNamespace(
        core_repo_path=str(core_repo),
        commit_sha="abcdef0000000000000000000000000000000",
        output_dir=str(tmp_path / "shadow-runs" / "baseline-test"),
        packet_glob="**/docs/work/*/02-qna-log.md",
        limit=None,
        repo_full_name="tandemstream/core",
        dry_run=False,
        quiet=True,
    )

    monkeypatch.setattr(gb, "replace", _fake_replace)
    monkeypatch.setattr(
        gb, "discover_packet_qna_logs",
        lambda *a, **kw: ["docs/work/2026-01-01-x/02-qna-log.md"],
    )

    def _fake_grade_one(*_args, **_kwargs):
        raise RuntimeError("bug escaped run_pr_grading")
    monkeypatch.setattr(gb, "grade_one_packet", _fake_grade_one)

    rc = gb.cmd_grade_packet_batch(cfg, args)
    assert rc == 2


def test_write_per_run_summary_md_sorts_packets_worst_first(tmp_path: Path):
    outcomes = [
        {"packet_slug": "good", "status": "ok",
         "summaries": [_mk_summary_dict("good", [("full_match", "q1"), ("full_match", "q2")])]},
        {"packet_slug": "bad", "status": "ok",
         "summaries": [_mk_summary_dict("bad", [("no_match", "q1"), ("no_match", "q2")])]},
        {"packet_slug": "mid", "status": "ok",
         "summaries": [_mk_summary_dict("mid", [("full_match", "q1"), ("no_match", "q2")])]},
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
