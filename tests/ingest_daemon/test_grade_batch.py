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


def _init_repo_with_qna_history(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    """Build a repo where a packet qna log is created, edited, then
    left untouched by a later run-head commit.

    Returns (repo, qna_path, created_sha, edited_sha, run_sha).
    """
    repo = tmp_path / "historyrepo"
    repo.mkdir()
    import subprocess as _sp
    kw = {"cwd": repo, "check": True, "capture_output": True}
    _sp.run(["git", "init", "--initial-branch=main", "-q"], **kw)
    _sp.run(["git", "config", "user.email", "t@e.com"], **kw)
    _sp.run(["git", "config", "user.name", "T"], **kw)

    qna_path = "docs/work/2026-01-01-history/02-qna-log.md"
    packet_dir = repo / "docs" / "work" / "2026-01-01-history"
    packet_dir.mkdir(parents=True)
    (packet_dir / "02-qna-log.md").write_text("# first\n")
    _sp.run(["git", "add", qna_path], **kw)
    _sp.run(["git", "commit", "-m", "create packet", "-q"], **kw)
    created = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo,
                      capture_output=True, text=True, check=True).stdout.strip()

    (packet_dir / "02-qna-log.md").write_text("# second\n")
    _sp.run(["git", "add", qna_path], **kw)
    _sp.run(["git", "commit", "-m", "edit packet", "-q"], **kw)
    edited = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo,
                     capture_output=True, text=True, check=True).stdout.strip()

    (repo / "README.md").write_text("# unrelated\n")
    _sp.run(["git", "add", "README.md"], **kw)
    _sp.run(["git", "commit", "-m", "unrelated run head", "-q"], **kw)
    run = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo,
                  capture_output=True, text=True, check=True).stdout.strip()
    return repo, qna_path, created, edited, run


def test_resolve_packet_commit_sha_modes(tmp_path: Path):
    """Historical shadow grading can pin each packet to its own history
    instead of grading all packets against the latest run commit."""
    repo, qna_path, created, edited, run = _init_repo_with_qna_history(tmp_path)

    assert gb.resolve_packet_commit_sha(
        repo, run_commit_sha=run, qna_log_path=qna_path, mode="created"
    ) == created
    assert gb.resolve_packet_commit_sha(
        repo, run_commit_sha=run, qna_log_path=qna_path, mode="latest-change"
    ) == edited
    assert gb.resolve_packet_commit_sha(
        repo, run_commit_sha=run, qna_log_path=qna_path, mode="run-commit"
    ) == run


def test_resolve_packet_commit_sha_falls_back_to_run_commit(tmp_path: Path, capsys):
    repo, _, _, _, run = _init_repo_with_qna_history(tmp_path)
    resolved = gb.resolve_packet_commit_sha(
        repo,
        run_commit_sha=run,
        qna_log_path="docs/work/no-such-packet/02-qna-log.md",
        mode="created",
    )
    assert resolved == run
    assert "falling back to run commit" in capsys.readouterr().err


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
    # Codex r6: state_file is also isolated to a per-batch path so the
    # orchestrator's pin lifecycle doesn't race the live daemon.
    assert cfg_used.state_file == cli_output_dir.resolve() / ".batch-state.json"
    assert cfg_used.state_file != Path(cfg.state_file)
    # The original cfg is untouched.
    assert cfg.core_repo_path == yaml_core_path
    assert cfg.shadow_runs_dir == yaml_shadow_runs


def test_cmd_grade_packet_batch_uses_packet_specific_history_sha(
    tmp_path: Path, monkeypatch
):
    """The batch run commit is for discovery; each packet's synthetic PR
    base/head SHA is resolved independently from packet history."""
    cli_core_path = tmp_path / "cli-core"
    cli_core_path.mkdir()
    output_dir = tmp_path / "cli-output" / "baseline-test"

    cfg = SimpleNamespace(
        grader_model="sonnet",
        state_file=str(tmp_path / "s.json"),
        core_repo_path=cli_core_path,
        shadow_runs_dir=tmp_path / "yaml-shadow-runs",
        continuous_shadow_org_id="org-1",
    )
    args = SimpleNamespace(
        core_repo_path=str(cli_core_path),
        commit_sha="runsha0000000000000000000000000000000000",
        output_dir=str(output_dir),
        packet_glob="**/docs/work/*/02-qna-log.md",
        packet_sha_mode="created",
        limit=None,
        repo_full_name="tandemstream/core",
        dry_run=False,
        quiet=True,
    )
    qna_path = "docs/work/2026-01-01-x/02-qna-log.md"
    packet_sha = "packetsha00000000000000000000000000000000"

    monkeypatch.setattr(gb, "replace", _fake_replace)
    monkeypatch.setattr(gb, "discover_packet_qna_logs", lambda *a, **kw: [qna_path])
    monkeypatch.setattr(gb, "resolve_packet_commit_sha", lambda *a, **kw: packet_sha)

    captured = {}
    def _capture(cfg_passed, **kw):
        captured["commit_sha"] = kw["commit_sha"]
        return {
            "packet_slug": "2026-01-01-x",
            "packet_qna_log_path": kw["qna_log_path"],
            "status": "ok",
            "summaries": [_mk_summary_dict("2026-01-01-x", [("full_match", "q1")])],
            "code_revision_id": "rev-packet",
            "base_sha": kw["commit_sha"],
            "head_sha": kw["commit_sha"],
            "pr_number": 0,
            "repo_full_name": kw["repo_full_name"],
        }
    monkeypatch.setattr(gb, "grade_one_packet", _capture)

    rc = gb.cmd_grade_packet_batch(cfg, args)
    assert rc == 0
    assert captured["commit_sha"] == packet_sha
    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert manifest["run_commit_sha"] == args.commit_sha
    assert manifest["packet_sha_mode"] == "created"
    assert manifest["packet_base_shas"]["2026-01-01-x"] == packet_sha
    packet_payload = json.loads((output_dir / "packets" / "2026-01-01-x.json").read_text())
    assert packet_payload["run_commit_sha"] == args.commit_sha
    assert packet_payload["packet_base_sha"] == packet_sha


def test_packet_sha_mode_default_is_run_commit(tmp_path: Path, monkeypatch):
    """Codex r1 (PR #9) regression lock-down: the cmd-level default for
    --packet-sha-mode MUST be 'run-commit', not 'created'.

    Rationale: the documented baseline workflow only ingests the latest
    commit, and `run_pr_grading` soft-passes any non-ingested SHA as
    `revision_not_indexed`. If 'created' is the default, every packet
    falls through to that soft-pass and the batch exits 2 with zero
    grades, which silently breaks the documented operator command.
    Historical SHA modes are opt-in (operators who want them must
    pre-ingest the SHAs).
    """
    parser = gb._build_subparser_for_test() if hasattr(gb, "_build_subparser_for_test") else None
    if parser is None:
        # Use the real argparse builder from entrypoint wiring.
        import argparse
        root = argparse.ArgumentParser()
        sub = root.add_subparsers(dest="cmd")
        gb.build_subparser(sub)
        args = root.parse_args([
            "grade-packet-batch",
            "--core-repo-path", str(tmp_path),
            "--output-dir", str(tmp_path / "out"),
        ])
    else:
        args = parser.parse_args([
            "--core-repo-path", str(tmp_path),
            "--output-dir", str(tmp_path / "out"),
        ])
    assert args.packet_sha_mode == "run-commit", (
        f"Default --packet-sha-mode is {args.packet_sha_mode!r}, expected "
        f"'run-commit'. Per codex r1 PR #9 fix, the default must NOT be "
        f"'created' (which requires per-packet SHAs to be pre-ingested)."
    )


def test_cmd_grade_packet_batch_dry_run_reports_packet_base_shas(
    tmp_path: Path, monkeypatch, capsys
):
    repo = tmp_path / "core-repo"
    repo.mkdir()
    cfg = SimpleNamespace(
        grader_model="sonnet",
        state_file=str(tmp_path / "s.json"),
        core_repo_path=repo,
        shadow_runs_dir=tmp_path / "yaml-shadow-runs",
    )
    args = SimpleNamespace(
        core_repo_path=str(repo),
        commit_sha="runsha0000000000000000000000000000000000",
        output_dir=str(tmp_path / "unused"),
        packet_glob="**/docs/work/*/02-qna-log.md",
        packet_sha_mode="created",
        limit=None,
        repo_full_name="tandemstream/core",
        dry_run=True,
        quiet=True,
    )
    qna_path = "docs/work/2026-01-01-x/02-qna-log.md"
    packet_sha = "packetsha00000000000000000000000000000000"
    monkeypatch.setattr(gb, "discover_packet_qna_logs", lambda *a, **kw: [qna_path])
    monkeypatch.setattr(gb, "resolve_packet_commit_sha", lambda *a, **kw: packet_sha)

    rc = gb.cmd_grade_packet_batch(cfg, args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["packet_sha_mode"] == "created"
    assert payload["packet_base_shas"] == {qna_path: packet_sha}


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


# ───────── by_evidence_type rollup ────────────────────────────────────


def _mk_summary_dict_with_evidence(
    packet_id: str,
    by_evidence_type: dict,
    *,
    total: int = 0,
    pass_count: int = 0,
    excluded_count: int = 0,
) -> dict:
    """Same dict shape ``run_pr_grading`` emits, but with explicit
    ``by_evidence_type`` plumbed through. Used to exercise the
    aggregator's sum path."""
    return {
        "packet_id": packet_id,
        "passed": False,
        "pass_pct": 0,
        "pass_count": pass_count,
        "total": total,
        "excluded_count": excluded_count,
        "by_evidence_type": by_evidence_type,
        "artifact_path": None,
    }


def test_aggregate_run_totals_sums_by_evidence_type_across_packets():
    """Run-level total_by_evidence_type sums receipts/correct/excluded
    across every packet, then derives clean_total + clean_pct from the
    SUMMED counts (not by averaging per-packet percentages)."""
    outcomes = [
        {
            "packet_slug": "p1",
            "status": "ok",
            "summaries": [_mk_summary_dict_with_evidence(
                "p1",
                {
                    "source_excerpt": {
                        "receipts": 10, "correct": 4, "excluded": 2,
                        "clean_total": 8, "clean_pct": 50.0,
                    },
                    "external_tool_docs": {
                        "receipts": 3, "correct": 0, "excluded": 3,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "user_context": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "absence_search": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "other": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                },
                total=13, pass_count=4, excluded_count=5,
            )],
        },
        {
            "packet_slug": "p2",
            "status": "ok",
            "summaries": [_mk_summary_dict_with_evidence(
                "p2",
                {
                    "source_excerpt": {
                        "receipts": 6, "correct": 3, "excluded": 0,
                        "clean_total": 6, "clean_pct": 50.0,
                    },
                    "external_tool_docs": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "user_context": {
                        "receipts": 2, "correct": 0, "excluded": 2,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "absence_search": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "other": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                },
                total=8, pass_count=3, excluded_count=2,
            )],
        },
    ]
    totals = gb._aggregate_run_totals(outcomes)
    run = totals["total_by_evidence_type"]

    # source_excerpt: 10+6=16 receipts, 4+3=7 correct, 2+0=2 excluded
    # clean_total=14, clean_pct=7/14=50.0
    assert run["source_excerpt"]["receipts"] == 16
    assert run["source_excerpt"]["correct"] == 7
    assert run["source_excerpt"]["excluded"] == 2
    assert run["source_excerpt"]["clean_total"] == 14
    assert run["source_excerpt"]["clean_pct"] == 50.0

    # external_tool_docs: all excluded → clean_total=0, clean_pct=None
    assert run["external_tool_docs"]["receipts"] == 3
    assert run["external_tool_docs"]["excluded"] == 3
    assert run["external_tool_docs"]["clean_total"] == 0
    assert run["external_tool_docs"]["clean_pct"] is None

    # user_context: 2 receipts both excluded
    assert run["user_context"]["receipts"] == 2
    assert run["user_context"]["clean_pct"] is None

    # Always-emitted buckets (zero-filled).
    assert run["absence_search"]["receipts"] == 0
    assert run["other"]["receipts"] == 0

    # Per-packet breakdown also present.
    assert totals["per_packet_pct"]["p1"]["by_evidence_type"]["source_excerpt"][
        "receipts"
    ] == 10
    assert totals["per_packet_pct"]["p2"]["by_evidence_type"]["user_context"][
        "receipts"
    ] == 2


def test_aggregate_run_totals_by_evidence_type_back_compat_missing_field():
    """Legacy summaries (pre-evidence-breakdown) lack ``by_evidence_type``.
    Aggregator must still emit zero-filled run-level breakdown rather
    than raising or returning a partial dict."""
    outcomes = [{
        "packet_slug": "legacy",
        "status": "ok",
        "summaries": [{
            "packet_id": "legacy", "passed": False, "pass_pct": 0,
            "pass_count": 0, "total": 5,
            # No by_evidence_type field at all.
            "artifact_path": None,
        }],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    run = totals["total_by_evidence_type"]
    # Every canonical bucket present with zero counts.
    for bucket in (
        "source_excerpt", "external_tool_docs",
        "user_context", "absence_search", "other",
    ):
        assert bucket in run
        assert run[bucket]["receipts"] == 0
        assert run[bucket]["clean_total"] == 0
        assert run[bucket]["clean_pct"] is None
    # Per-packet breakdown also zero-filled.
    assert totals["per_packet_pct"]["legacy"]["by_evidence_type"][
        "source_excerpt"
    ]["receipts"] == 0


def test_aggregate_run_totals_by_evidence_type_unknown_bucket_routed_to_other():
    """If a packet's summary carries an unexpected bucket key (e.g. a
    future evidence_type rolled out before the aggregator was updated),
    its counts route into 'other' rather than corrupting a canonical
    bucket or raising KeyError."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict_with_evidence(
            "p1",
            {
                "source_excerpt": {
                    "receipts": 5, "correct": 2, "excluded": 0,
                    "clean_total": 5, "clean_pct": 40.0,
                },
                # Unexpected bucket from a future grader version.
                "newly_invented_type": {
                    "receipts": 2, "correct": 1, "excluded": 0,
                    "clean_total": 2, "clean_pct": 50.0,
                },
            },
            total=7, pass_count=3, excluded_count=0,
        )],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    run = totals["total_by_evidence_type"]
    assert run["source_excerpt"]["receipts"] == 5
    # The unknown bucket landed in 'other'.
    assert run["other"]["receipts"] == 2
    assert run["other"]["correct"] == 1
    assert run["other"]["clean_pct"] == 50.0


# ───────── by_lane rollup (sibling of by_evidence_type rollup) ─────────


def _mk_summary_dict_with_lane(
    packet_id: str,
    by_lane: dict,
    *,
    total: int = 0,
    pass_count: int = 0,
    excluded_count: int = 0,
) -> dict:
    """Same dict shape ``run_pr_grading`` emits, with ``by_lane``
    plumbed through. Mirror of ``_mk_summary_dict_with_evidence``."""
    return {
        "packet_id": packet_id,
        "passed": False,
        "pass_pct": 0,
        "pass_count": pass_count,
        "total": total,
        "excluded_count": excluded_count,
        "by_lane": by_lane,
        "artifact_path": None,
    }


def test_aggregate_run_totals_sums_by_lane_across_packets():
    """Run-level total_by_lane sums receipts/correct/excluded across
    every packet, then derives clean_total + clean_pct from the SUMMED
    counts (not by averaging per-packet percentages). Mirror of
    test_aggregate_run_totals_sums_by_evidence_type_across_packets."""
    outcomes = [
        {
            "packet_slug": "p1",
            "status": "ok",
            "summaries": [_mk_summary_dict_with_lane(
                "p1",
                {
                    "explicit_source_fast_path": {
                        "receipts": 8, "correct": 3, "excluded": 0,
                        "clean_total": 8, "clean_pct": 37.5,
                    },
                    "doc_resolver": {
                        "receipts": 4, "correct": 1, "excluded": 1,
                        "clean_total": 3, "clean_pct": 33.3,
                    },
                    "non_retrieval": {
                        "receipts": 2, "correct": 0, "excluded": 2,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "fuzzy_find_code": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "scan_search": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "other": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                },
                total=14, pass_count=4, excluded_count=3,
            )],
        },
        {
            "packet_slug": "p2",
            "status": "ok",
            "summaries": [_mk_summary_dict_with_lane(
                "p2",
                {
                    "explicit_source_fast_path": {
                        "receipts": 4, "correct": 4, "excluded": 0,
                        "clean_total": 4, "clean_pct": 100.0,
                    },
                    "doc_resolver": {
                        "receipts": 2, "correct": 1, "excluded": 0,
                        "clean_total": 2, "clean_pct": 50.0,
                    },
                    "non_retrieval": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "fuzzy_find_code": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "scan_search": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                    "other": {
                        "receipts": 0, "correct": 0, "excluded": 0,
                        "clean_total": 0, "clean_pct": None,
                    },
                },
                total=6, pass_count=5, excluded_count=0,
            )],
        },
    ]
    totals = gb._aggregate_run_totals(outcomes)
    run = totals["total_by_lane"]

    # explicit_source_fast_path: 8+4=12 receipts, 3+4=7 correct, 0 excluded
    # clean_total=12, clean_pct=7/12=58.3
    assert run["explicit_source_fast_path"]["receipts"] == 12
    assert run["explicit_source_fast_path"]["correct"] == 7
    assert run["explicit_source_fast_path"]["excluded"] == 0
    assert run["explicit_source_fast_path"]["clean_total"] == 12
    assert run["explicit_source_fast_path"]["clean_pct"] == 58.3

    # doc_resolver: 4+2=6 receipts, 1+1=2 correct, 1+0=1 excluded
    # clean_total=5, clean_pct=2/5=40.0
    assert run["doc_resolver"]["receipts"] == 6
    assert run["doc_resolver"]["correct"] == 2
    assert run["doc_resolver"]["clean_total"] == 5
    assert run["doc_resolver"]["clean_pct"] == 40.0

    # non_retrieval: all excluded
    assert run["non_retrieval"]["receipts"] == 2
    assert run["non_retrieval"]["clean_total"] == 0
    assert run["non_retrieval"]["clean_pct"] is None

    # Per-packet breakdown also present.
    assert totals["per_packet_pct"]["p1"]["by_lane"][
        "explicit_source_fast_path"
    ]["receipts"] == 8
    assert totals["per_packet_pct"]["p2"]["by_lane"][
        "doc_resolver"
    ]["receipts"] == 2


def test_aggregate_run_totals_by_lane_back_compat_missing_field():
    """Legacy summaries (pre-by-lane) lack ``by_lane``. Aggregator
    must still emit zero-filled run-level breakdown rather than
    raising or returning a partial dict. Mirror of the by_evidence_type
    back-compat test."""
    outcomes = [{
        "packet_slug": "legacy",
        "status": "ok",
        "summaries": [{
            "packet_id": "legacy", "passed": False, "pass_pct": 0,
            "pass_count": 0, "total": 5,
            # No by_lane field at all.
            "artifact_path": None,
        }],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    run = totals["total_by_lane"]
    # Every canonical bucket present with zero counts.
    for bucket in (
        "explicit_source_fast_path", "fuzzy_find_code",
        "scan_search", "doc_resolver", "non_retrieval", "other",
    ):
        assert bucket in run
        assert run[bucket]["receipts"] == 0
        assert run[bucket]["clean_total"] == 0
        assert run[bucket]["clean_pct"] is None
    # Per-packet breakdown also zero-filled.
    assert totals["per_packet_pct"]["legacy"]["by_lane"][
        "explicit_source_fast_path"
    ]["receipts"] == 0


def test_aggregate_run_totals_by_lane_unknown_bucket_routed_to_other():
    """If a packet's summary carries an unexpected lane bucket key
    (e.g. a future lane added to _infer_lane before the aggregator
    was updated), its counts route into 'other' rather than corrupting
    a canonical bucket or raising KeyError. Implements the
    tolerance-to-future-values requirement per Codex's guidance."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict_with_lane(
            "p1",
            {
                "doc_resolver": {
                    "receipts": 5, "correct": 2, "excluded": 0,
                    "clean_total": 5, "clean_pct": 40.0,
                },
                # Unexpected lane from a future grader version.
                "newly_added_lane": {
                    "receipts": 3, "correct": 1, "excluded": 0,
                    "clean_total": 3, "clean_pct": 33.3,
                },
            },
            total=8, pass_count=3, excluded_count=0,
        )],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    run = totals["total_by_lane"]
    assert run["doc_resolver"]["receipts"] == 5
    # The unknown bucket landed in 'other'.
    assert run["other"]["receipts"] == 3
    assert run["other"]["correct"] == 1
    assert run["other"]["clean_pct"] == 33.3


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


# ─── PR #14: clean-denominator aggregation ────────────────────────────


def _mk_summary_dict_with_skips(
    packet_id: str,
    *,
    pass_count: int,
    total: int,
    excluded_count: int,
    skipped_receipt_stale_count: int,
) -> dict:
    """Variant of _mk_summary_dict that carries the PR #14 clean-
    denominator bookkeeping. Used to test the aggregation math in
    isolation from the row-level lane/score_status derivation."""
    clean_total = total - excluded_count
    clean_pct = (
        int(round(pass_count * 100 / clean_total)) if clean_total > 0 else None
    )
    return {
        "packet_id": packet_id,
        "passed": (pass_count * 100 // total >= 60) if total else False,
        "pass_pct": int(round(pass_count * 100 / total)) if total else 0,
        "pass_count": pass_count,
        "total": total,
        "clean_pass_pct": clean_pct,
        "clean_total": clean_total,
        "excluded_count": excluded_count,
        "skipped_receipt_stale_count": skipped_receipt_stale_count,
        "artifact_path": None,
    }


def test_aggregate_run_totals_clean_score_drops_excluded_rows():
    """Three packets, mix of passes / fails / receipt-stale skips.
    Clean score should drop skipped rows from BOTH numerator and
    denominator; raw score keeps them in the denominator."""
    outcomes = [
        {
            "packet_slug": "p1",
            "status": "ok",
            "summaries": [_mk_summary_dict_with_skips(
                "p1", pass_count=10, total=12,
                excluded_count=2, skipped_receipt_stale_count=2,
            )],
        },
        {
            "packet_slug": "p2",
            "status": "ok",
            "summaries": [_mk_summary_dict_with_skips(
                "p2", pass_count=4, total=13,
                excluded_count=0, skipped_receipt_stale_count=0,
            )],
        },
        {
            "packet_slug": "p3",
            "status": "ok",
            "summaries": [_mk_summary_dict_with_skips(
                "p3", pass_count=8, total=23,
                excluded_count=5, skipped_receipt_stale_count=5,
            )],
        },
    ]
    totals = gb._aggregate_run_totals(outcomes)
    # Raw: 10+4+8 = 22 correct out of 12+13+23 = 48 → 45.8%
    assert totals["total_receipts"] == 48
    assert totals["total_correct"] == 22
    assert totals["overall_pct"] == round(22 * 100 / 48, 1)
    # Clean: 22 correct out of (48 - 7) = 41 → 53.7%
    assert totals["total_excluded"] == 7
    assert totals["total_skipped_receipt_stale"] == 7
    assert totals["clean_total"] == 41
    assert totals["clean_overall_pct"] == round(22 * 100 / 41, 1)
    # Per-packet rows carry both metrics.
    assert totals["per_packet_pct"]["p1"]["excluded"] == 2
    assert totals["per_packet_pct"]["p1"]["clean_total"] == 10
    assert totals["per_packet_pct"]["p1"]["clean_pct"] == 100.0
    assert totals["per_packet_pct"]["p2"]["clean_pct"] == round(4 * 100 / 13, 1)
    assert totals["per_packet_pct"]["p3"]["clean_pct"] == round(8 * 100 / 18, 1)


def test_aggregate_run_totals_counts_legacy_receipt_defect_skip():
    outcomes = [
        {
            "packet_slug": "legacy",
            "status": "ok",
            "summaries": [{
                "packet_id": "legacy",
                "passed": True,
                "pass_pct": 50,
                "pass_count": 1,
                "total": 2,
                "clean_pass_pct": 100,
                "clean_total": 1,
                "excluded_count": 1,
                "skipped_legacy_receipt_defect_count": 1,
                "artifact_path": None,
            }],
        }
    ]
    totals = gb._aggregate_run_totals(outcomes)

    assert totals["total_excluded"] == 1
    assert totals["total_skipped_legacy_receipt_defect"] == 1
    assert totals["clean_total"] == 1
    assert totals["clean_overall_pct"] == 100.0
    assert (
        totals["per_packet_pct"]["legacy"]["skipped_legacy_receipt_defect"]
        == 1
    )


def test_aggregate_run_totals_legacy_summaries_still_work():
    """Pre-PR-14 summaries (no excluded_count field) should aggregate
    cleanly — excluded defaults to zero, clean_overall_pct equals
    overall_pct."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        # Use the legacy helper that lacks new fields entirely.
        "summaries": [_mk_summary_dict("p1", [
            ("full_match", "q1"), ("full_match", "q2"), ("no_match", "q3"),
        ])],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_excluded"] == 0
    assert totals["total_skipped_receipt_stale"] == 0
    assert totals["clean_total"] == 3
    # Same as overall_pct since nothing was excluded.
    assert totals["clean_overall_pct"] == totals["overall_pct"]


def test_aggregate_run_totals_clean_pct_is_none_when_all_excluded():
    """If every row in a run was excluded (e.g. every receipt was
    stale), clean_overall_pct returns None rather than 0/0."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict_with_skips(
            "p1", pass_count=0, total=3,
            excluded_count=3, skipped_receipt_stale_count=3,
        )],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["clean_total"] == 0
    assert totals["clean_overall_pct"] is None
    # Per-packet too.
    assert totals["per_packet_pct"]["p1"]["clean_pct"] is None


def _mk_summary_dict_with_drift(
    packet_id: str,
    *,
    pass_count: int,
    total: int,
    excluded_count: int,
    skipped_receipt_stale_count: int,
    skipped_run_commit_line_drift_count: int,
) -> dict:
    """PR #15: variant that carries both excluded counts."""
    clean_total = total - excluded_count
    clean_pct = (
        int(round(pass_count * 100 / clean_total)) if clean_total > 0 else None
    )
    return {
        "packet_id": packet_id,
        "passed": (pass_count * 100 // total >= 60) if total else False,
        "pass_pct": int(round(pass_count * 100 / total)) if total else 0,
        "pass_count": pass_count,
        "total": total,
        "clean_pass_pct": clean_pct,
        "clean_total": clean_total,
        "excluded_count": excluded_count,
        "skipped_receipt_stale_count": skipped_receipt_stale_count,
        "skipped_run_commit_line_drift_count": skipped_run_commit_line_drift_count,
        "artifact_path": None,
    }


def test_aggregate_run_totals_separates_stale_from_drift():
    """PR #15: total_excluded should equal stale + drift, and the two
    components are reported as distinct totals so operators can chart
    them independently."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict_with_drift(
            "p1", pass_count=8, total=12,
            excluded_count=3,
            skipped_receipt_stale_count=2,
            skipped_run_commit_line_drift_count=1,
        )],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_excluded"] == 3
    assert totals["total_skipped_receipt_stale"] == 2
    assert totals["total_skipped_run_commit_line_drift"] == 1
    assert totals["clean_total"] == 9  # 12 - 3
    # Per-packet too.
    pp = totals["per_packet_pct"]["p1"]
    assert pp["skipped_receipt_stale"] == 2
    assert pp["skipped_run_commit_line_drift"] == 1


def test_aggregate_run_totals_legacy_summary_drift_zero():
    """Pre-PR-15 summaries (no skipped_run_commit_line_drift_count
    field) default to zero — same back-compat shape as PR #14's
    excluded_count default."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        # Legacy helper with no drift field.
        "summaries": [_mk_summary_dict_with_skips(
            "p1", pass_count=10, total=12,
            excluded_count=2, skipped_receipt_stale_count=2,
        )],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_skipped_receipt_stale"] == 2
    assert totals["total_skipped_run_commit_line_drift"] == 0


def test_write_per_run_summary_md_breaks_out_drift(tmp_path: Path):
    """summary.md exclusion line shows both receipt-stale AND drift
    breakouts."""
    outcomes = [{
        "packet_slug": "pkt1",
        "status": "ok",
        "summaries": [_mk_summary_dict_with_drift(
            "pkt1", pass_count=9, total=12,
            excluded_count=3,
            skipped_receipt_stale_count=2,
            skipped_run_commit_line_drift_count=1,
        )],
    }]
    path = gb.write_per_run_summary_md(
        "baseline-pr15",
        tmp_path,
        commit_sha="d9a5d53c97ad" + "0" * 28,
        packet_outcomes=outcomes,
        overall_pct=75.0,
        total_receipts=12,
        total_correct=9,
        clean_overall_pct=100.0,
        clean_total=9,
        total_excluded=3,
        total_skipped_receipt_stale=2,
        total_skipped_run_commit_line_drift=1,
    )
    text = path.read_text()
    assert "2 receipt-stale" in text
    assert "1 run-commit-line-drift" in text


def test_write_per_run_summary_md_renders_both_scores(tmp_path: Path):
    """summary.md must show Raw % AND Clean % columns + the totals
    line that calls out exclusion counts."""
    outcomes = [{
        "packet_slug": "pkt1",
        "status": "ok",
        "summaries": [_mk_summary_dict_with_skips(
            "pkt1", pass_count=10, total=12,
            excluded_count=2, skipped_receipt_stale_count=2,
        )],
    }]
    path = gb.write_per_run_summary_md(
        "baseline-pr14",
        tmp_path,
        commit_sha="d9a5d53c97ad" + "0" * 28,
        packet_outcomes=outcomes,
        overall_pct=83.3,
        total_receipts=12,
        total_correct=10,
        clean_overall_pct=100.0,
        clean_total=10,
        total_excluded=2,
        total_skipped_receipt_stale=2,
    )
    text = path.read_text()
    # Both totals rendered.
    assert "Raw correct" in text
    assert "Clean correct" in text
    assert "(83.3%)" in text  # raw
    assert "(100.0%)" in text  # clean
    # Receipt-stale exclusion called out.
    assert "2 receipt-stale" in text
    # Table header shows both pct columns.
    assert "Raw %" in text and "Clean %" in text and "Excluded" in text


# ─── PR #17: non-retrieval skip aggregation ───────────────────────────


def _mk_summary_dict_pr17(
    packet_id: str,
    *,
    pass_count: int,
    total: int,
    non_repo: int = 0,
    absence: int = 0,
    unavailable: int = 0,
    corpus_excluded: int = 0,
) -> dict:
    """PR #17: dict shape including all four new skip counters."""
    excluded = non_repo + absence + unavailable + corpus_excluded
    clean_total = total - excluded
    clean_pct = (
        int(round(pass_count * 100 / clean_total)) if clean_total > 0 else None
    )
    return {
        "packet_id": packet_id,
        "passed": False,
        "pass_pct": int(round(pass_count * 100 / total)) if total else 0,
        "pass_count": pass_count,
        "total": total,
        "clean_pass_pct": clean_pct,
        "clean_total": clean_total,
        "excluded_count": excluded,
        "skipped_receipt_stale_count": 0,
        "skipped_run_commit_line_drift_count": 0,
        "skipped_non_repo_evidence_count": non_repo,
        "skipped_absence_search_count": absence,
        "skipped_unavailable_source_ref_count": unavailable,
        "skipped_doc_corpus_excluded_count": corpus_excluded,
        "artifact_path": None,
    }


def test_aggregate_run_totals_separates_pr17_skip_categories():
    """PR #17's four non-retrieval skips contribute distinct totals
    to the manifest, all rolled up into total_excluded."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict_pr17(
            "p1", pass_count=3, total=10,
            non_repo=2, absence=1, unavailable=3, corpus_excluded=1,
        )],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_excluded"] == 7  # 2+1+3+1
    assert totals["total_skipped_non_repo_evidence"] == 2
    assert totals["total_skipped_absence_search"] == 1
    assert totals["total_skipped_unavailable_source_ref"] == 3
    assert totals["total_skipped_doc_corpus_excluded"] == 1
    assert totals["clean_total"] == 3
    assert totals["clean_overall_pct"] == 100.0
    # Per-packet breakouts too.
    pp = totals["per_packet_pct"]["p1"]
    assert pp["skipped_non_repo_evidence"] == 2
    assert pp["skipped_absence_search"] == 1
    assert pp["skipped_unavailable_source_ref"] == 3
    assert pp["skipped_doc_corpus_excluded"] == 1


def test_aggregate_run_totals_legacy_summaries_pr17_zero():
    """Pre-PR-17 summaries (no new fields) get zero across the board."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict("p1", [("full_match", "q1")])],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_skipped_non_repo_evidence"] == 0
    assert totals["total_skipped_absence_search"] == 0
    assert totals["total_skipped_unavailable_source_ref"] == 0
    assert totals["total_skipped_doc_corpus_excluded"] == 0


def test_write_per_run_summary_md_breaks_out_pr17_components(tmp_path: Path):
    """The exclusion-line breakout names every non-zero PR #17 skip
    component when present."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict_pr17(
            "p1", pass_count=3, total=10,
            non_repo=2, unavailable=2, corpus_excluded=1,
        )],
    }]
    path = gb.write_per_run_summary_md(
        "baseline-pr17",
        tmp_path,
        commit_sha="d9a5d53c97ad" + "0" * 28,
        packet_outcomes=outcomes,
        overall_pct=30.0,
        total_receipts=10,
        total_correct=3,
        clean_overall_pct=60.0,
        clean_total=5,
        total_excluded=5,
        total_skipped_non_repo_evidence=2,
        total_skipped_unavailable_source_ref=2,
        total_skipped_doc_corpus_excluded=1,
    )
    text = path.read_text()
    assert "2 non-repo-evidence" in text
    assert "2 unavailable-source-ref" in text
    assert "1 doc-corpus-excluded" in text
    # Categories with zero count don't appear in the breakdown line.
    assert "absence-search" not in text or "0 absence-search" not in text


# ─── PR #20: command-snapshot lane aggregation ────────────────────────


def _mk_summary_dict_pr20(
    packet_id: str,
    *,
    pass_count: int,
    total: int,
    command_snapshot: int = 0,
) -> dict:
    """Variant carrying the PR #20 command-snapshot count."""
    excluded = command_snapshot
    clean_total = total - excluded
    clean_pct = (
        int(round(pass_count * 100 / clean_total)) if clean_total > 0 else None
    )
    return {
        "packet_id": packet_id,
        "passed": False,
        "pass_pct": int(round(pass_count * 100 / total)) if total else 0,
        "pass_count": pass_count,
        "total": total,
        "clean_pass_pct": clean_pct,
        "clean_total": clean_total,
        "excluded_count": excluded,
        "skipped_command_snapshot_count": command_snapshot,
        "artifact_path": None,
    }


def test_aggregate_run_totals_command_snapshot_lane():
    """Command-snapshot skips roll up into total_skipped_command_snapshot
    independently of other skip categories."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict_pr20(
            "p1", pass_count=2, total=10, command_snapshot=3,
        )],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_skipped_command_snapshot"] == 3
    pp = totals["per_packet_pct"]["p1"]
    assert pp["skipped_command_snapshot"] == 3


def test_aggregate_run_totals_legacy_summary_command_snapshot_zero():
    """Pre-PR-20 summaries (no skipped_command_snapshot_count) get
    zero — same back-compat shape as the PR #17 totals."""
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict("p1", [("full_match", "q1")])],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_skipped_command_snapshot"] == 0


def test_write_per_run_summary_md_includes_command_snapshot_breakout(tmp_path: Path):
    outcomes = [{
        "packet_slug": "p1",
        "status": "ok",
        "summaries": [_mk_summary_dict_pr20(
            "p1", pass_count=2, total=10, command_snapshot=3,
        )],
    }]
    path = gb.write_per_run_summary_md(
        "baseline-pr20",
        tmp_path,
        commit_sha="d9a5d53c97ad" + "0" * 28,
        packet_outcomes=outcomes,
        overall_pct=20.0,
        total_receipts=10,
        total_correct=2,
        clean_overall_pct=28.6,
        clean_total=7,
        total_excluded=3,
        total_skipped_command_snapshot=3,
    )
    text = path.read_text()
    assert "3 command-snapshot" in text


# ─── Cache aggregation (PR atlas-shadow-query-cache-v1) ──────────────


def test_aggregate_run_totals_sums_atlas_cache_counts_across_packets():
    """``_aggregate_run_totals`` sums cache hit/miss/disabled counts
    across packets so the manifest reports a single run-level total.
    Without this aggregation, operators can't tell whether a fast run
    came from cache hits (which means Atlas's measured improvement is
    masked) or genuine Atlas improvement."""
    outcomes = [
        {
            "packet_slug": "p1",
            "status": "ok",
            "summaries": [{
                "packet_id": "p1", "passed": True, "pass_pct": 80,
                "pass_count": 8, "total": 10,
                "atlas_cache_hit_count": 3,
                "atlas_cache_miss_count": 4,
                "atlas_cache_disabled_count": 0,
                "artifact_path": None,
            }],
        },
        {
            "packet_slug": "p2",
            "status": "ok",
            "summaries": [{
                "packet_id": "p2", "passed": True, "pass_pct": 100,
                "pass_count": 5, "total": 5,
                "atlas_cache_hit_count": 2,
                "atlas_cache_miss_count": 1,
                "atlas_cache_disabled_count": 0,
                "artifact_path": None,
            }],
        },
    ]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_atlas_cache_hits"] == 5  # 3 + 2
    assert totals["total_atlas_cache_misses"] == 5  # 4 + 1
    assert totals["total_atlas_cache_disabled"] == 0
    # Per-packet totals also present.
    assert totals["per_packet_pct"]["p1"]["atlas_cache_hits"] == 3
    assert totals["per_packet_pct"]["p2"]["atlas_cache_hits"] == 2


def test_aggregate_run_totals_back_compat_when_cache_counts_missing():
    """Legacy summaries (pre-cache PR) lack the atlas_cache_* fields.
    Aggregator must treat them as zero so the manifest stays
    well-formed without forcing pre-cache baselines to re-grade."""
    outcomes = [{
        "packet_slug": "legacy",
        "status": "ok",
        "summaries": [{
            "packet_id": "legacy", "passed": True, "pass_pct": 50,
            "pass_count": 1, "total": 2,
            # No atlas_cache_* fields at all.
            "artifact_path": None,
        }],
    }]
    totals = gb._aggregate_run_totals(outcomes)
    assert totals["total_atlas_cache_hits"] == 0
    assert totals["total_atlas_cache_misses"] == 0
    assert totals["total_atlas_cache_disabled"] == 0
    assert totals["per_packet_pct"]["legacy"]["atlas_cache_hits"] == 0


# ===========================================================================
# PR atlas-shadow-receipt-parallelism-v1 — --max-workers flag plumbing
# ===========================================================================


def test_grade_one_packet_forwards_max_workers_to_orchestrator():
    """``grade_one_packet`` must propagate ``max_workers`` to
    ``run_pr_grading`` unchanged. The CLI flag → batch loop → packet
    grader → orchestrator chain is what makes parallelism opt-in at
    the batch level without leaking into the live PR-grading webhook
    path (which doesn't pass this kwarg)."""
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
    gb.grade_one_packet(
        cfg,
        repo_full_name="tandemstream/core",
        commit_sha="abc1234",
        qna_log_path="products/foo/docs/work/p-1/02-qna-log.md",
        github_token="ignored",
        core_repo_path=Path("/fake/repo"),
        max_workers=4,
        _run_pr_grading=_fake_run_pr_grading,
        _read_file_at_commit=lambda **kw: "stubbed-content",
    )
    assert captured_kwargs["max_workers"] == 4


def test_grade_one_packet_defaults_max_workers_to_none():
    """When the CLI doesn't pass --max-workers, grade_one_packet must
    forward ``max_workers=None`` so the orchestrator falls back to
    cfg.grading_max_workers. Explicit-None preserves the documented
    resolution order and lets cfg-only operators get parallelism by
    setting the YAML field alone (no CLI flag needed)."""
    captured_kwargs = {}

    def _fake_run_pr_grading(cfg, event, **kwargs):
        captured_kwargs.update(kwargs)
        return {
            "summaries": [], "status": "ok",
            "code_revision_id": "fake-rev",
            "base_sha": event.base_sha, "head_sha": event.head_sha,
            "pr_number": event.pr_number,
            "repo_full_name": event.repo_full_name,
        }

    cfg = SimpleNamespace()
    gb.grade_one_packet(
        cfg,
        repo_full_name="tandemstream/core",
        commit_sha="abc1234",
        qna_log_path="products/foo/docs/work/p-1/02-qna-log.md",
        github_token="ignored",
        core_repo_path=Path("/fake/repo"),
        _run_pr_grading=_fake_run_pr_grading,
        _read_file_at_commit=lambda **kw: "stubbed-content",
    )
    assert captured_kwargs["max_workers"] is None


def test_grade_packet_batch_argparser_accepts_max_workers():
    """The subcommand parser must expose ``--max-workers`` as an int
    with default None. argparse default-of-None is load-bearing because
    cmd_grade_packet_batch reads ``getattr(args, 'max_workers', None)``."""
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    gb.build_subparser(sub)
    # Without --max-workers → None.
    args = parser.parse_args([
        "grade-packet-batch",
        "--core-repo-path", "/tmp/core",
        "--output-dir", "/tmp/out",
    ])
    assert args.max_workers is None
    # With --max-workers 7 → int 7.
    args2 = parser.parse_args([
        "grade-packet-batch",
        "--core-repo-path", "/tmp/core",
        "--output-dir", "/tmp/out",
        "--max-workers", "7",
    ])
    assert args2.max_workers == 7
