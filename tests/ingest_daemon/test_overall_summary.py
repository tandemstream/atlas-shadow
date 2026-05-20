"""Unit tests for ``atlas_shadow.ingest_daemon.overall_summary``.

Covers:
- ``load_run_manifests`` — sort + skip-on-corrupt
- ``_compute_callouts`` — regression + improvement thresholds
- ``_format_md`` / ``_format_json`` — output shape
- ``regenerate`` — end-to-end + atomic write
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_shadow.ingest_daemon import overall_summary as os_mod


# ───────── load_run_manifests ────────────────────────────────────────


def _write_run(root: Path, run_name: str, **overrides) -> None:
    """Materialize a synthetic baseline-X/manifest.json."""
    run_dir = root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_name": run_name,
        "commit_sha": "d9a5d53c97ad6abd768103bc9386cde25ee61be2",
        "code_revision_id": "rev-default",
        "started_at": "2026-05-15T00:00:00Z",
        "finished_at": "2026-05-15T01:00:00Z",
        "total_packets": 1,
        "total_receipts": 10,
        "total_correct": 8,
        "overall_pct": 80.0,
        "grader_backend": "claude_cli",
        "grader_model": "sonnet",
        "per_packet_pct": {"p1": {"receipts": 10, "correct": 8, "pct": 80.0}},
    }
    manifest.update(overrides)
    (run_dir / "manifest.json").write_text(json.dumps(manifest))


def test_load_run_manifests_returns_sorted_by_started_at(tmp_path: Path):
    _write_run(tmp_path, "baseline-2026-05-15", started_at="2026-05-15T10:00:00Z")
    _write_run(tmp_path, "baseline-2026-05-16", started_at="2026-05-16T10:00:00Z")
    _write_run(tmp_path, "baseline-2026-05-14", started_at="2026-05-14T10:00:00Z")
    runs = os_mod.load_run_manifests(tmp_path)
    assert [r["run_name"] for r in runs] == [
        "baseline-2026-05-14",
        "baseline-2026-05-15",
        "baseline-2026-05-16",
    ]


def test_load_run_manifests_skips_corrupt_files(tmp_path: Path, capsys):
    _write_run(tmp_path, "baseline-good", started_at="2026-05-15T10:00:00Z")
    bad_dir = tmp_path / "baseline-bad"
    bad_dir.mkdir()
    (bad_dir / "manifest.json").write_text("not valid json")
    runs = os_mod.load_run_manifests(tmp_path)
    assert [r["run_name"] for r in runs] == ["baseline-good"]
    err = capsys.readouterr().err
    assert "baseline-bad" in err


@pytest.mark.parametrize(
    "bad_content,kind",
    [
        ("[]", "list"),
        ("null", "NoneType"),
        ('"a string"', "str"),
        ("42", "int"),
    ],
)
def test_load_run_manifests_skips_non_object_manifests(
    tmp_path: Path, capsys, bad_content, kind
):
    """Codex r7 P2: a manifest that's valid JSON but not a dict (top-
    level array, null, scalar) would crash the `manifest["_dir"]=...`
    assignment. Skip with a WARN instead so one malformed historical
    manifest doesn't break every subsequent batch."""
    _write_run(tmp_path, "baseline-good", started_at="2026-05-15T10:00:00Z")
    bad_dir = tmp_path / "baseline-malformed"
    bad_dir.mkdir()
    (bad_dir / "manifest.json").write_text(bad_content)
    runs = os_mod.load_run_manifests(tmp_path)
    assert [r["run_name"] for r in runs] == ["baseline-good"]
    err = capsys.readouterr().err
    assert "baseline-malformed" in err
    assert "not a JSON object" in err
    assert kind in err  # explicit type name in the WARN aids debugging


def test_load_run_manifests_handles_missing_root(tmp_path: Path):
    runs = os_mod.load_run_manifests(tmp_path / "does-not-exist")
    assert runs == []


def test_load_run_manifests_ignores_non_baseline_dirs(tmp_path: Path):
    """Subdirs that aren't ``baseline-*`` (e.g. pr-<n>-... PR artifacts)
    should NOT be picked up by the dashboard."""
    _write_run(tmp_path, "baseline-2026-05-15")
    other_dir = tmp_path / "pr-275-20260515-202650-no-packet"
    other_dir.mkdir()
    (other_dir / "manifest.json").write_text(json.dumps({"run_name": "pr-275"}))
    runs = os_mod.load_run_manifests(tmp_path)
    assert [r["run_name"] for r in runs] == ["baseline-2026-05-15"]


# ───────── de-duplication when two dirs share run_name ──────────────


def _write_run_with_manifest(
    root: Path,
    dir_name: str,
    *,
    manifest_run_name: str,
    started_at: str = "2026-05-15T00:00:00Z",
) -> None:
    """Materialize a baseline dir where the directory name and the
    ``run_name`` field deliberately differ. Used to exercise the
    de-dup logic — _write_run() ties the two together."""
    run_dir = root / dir_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_name": manifest_run_name,
        "commit_sha": "d9a5d53c97ad6abd768103bc9386cde25ee61be2",
        "code_revision_id": "rev-default",
        "started_at": started_at,
        "finished_at": "2026-05-15T01:00:00Z",
        "total_packets": 1,
        "total_receipts": 10,
        "total_correct": 8,
        "overall_pct": 80.0,
        "grader_backend": "claude_cli",
        "grader_model": "sonnet",
        "per_packet_pct": {"p1": {"receipts": 10, "correct": 8, "pct": 80.0}},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))


def test_load_run_manifests_dedupes_by_run_name_prefers_matching_dir(
    tmp_path: Path, capsys,
):
    """The most common shape: an archived/broken copy of a baseline
    sitting next to the real one, both with ``run_name=baseline-X``.
    The directory whose name matches ``run_name`` is canonical; the
    archived copy is dropped with a stderr warning.

    Reproduces the real-world case where
    ``shadow-runs/baseline-2026-05-15-broken-no-workspace-py/`` shared
    ``run_name=baseline-2026-05-15`` with the real run and the
    dashboard double-counted both.
    """
    # Canonical: directory name matches run_name.
    _write_run(tmp_path, "baseline-2026-05-15",
               started_at="2026-05-15T21:38:39Z")
    # Archive copy: same run_name field, different directory name.
    _write_run_with_manifest(
        tmp_path,
        "baseline-2026-05-15-broken-no-workspace-py",
        manifest_run_name="baseline-2026-05-15",
        started_at="2026-05-15T21:31:17Z",
    )

    runs = os_mod.load_run_manifests(tmp_path)
    assert len(runs) == 1
    assert runs[0]["_dir"] == "baseline-2026-05-15"
    err = capsys.readouterr().err
    assert "baseline-2026-05-15-broken-no-workspace-py" in err
    assert "rename or move them out of baseline-*/" in err


def test_load_run_manifests_dedupes_falls_back_to_latest_started_at(
    tmp_path: Path,
):
    """When neither directory matches ``run_name`` exactly (both are
    archived copies, both renamed away from canonical), prefer the
    latest ``started_at``. Without a directory-name match there's no
    way to tell which copy is canonical; latest-finished is the
    least-bad heuristic.
    """
    _write_run_with_manifest(
        tmp_path, "baseline-2026-archive-a",
        manifest_run_name="baseline-original",
        started_at="2026-05-15T01:00:00Z",
    )
    _write_run_with_manifest(
        tmp_path, "baseline-2026-archive-b",
        manifest_run_name="baseline-original",
        started_at="2026-05-15T02:00:00Z",
    )
    runs = os_mod.load_run_manifests(tmp_path)
    assert len(runs) == 1
    # Later started_at wins.
    assert runs[0]["_dir"] == "baseline-2026-archive-b"


def test_load_run_manifests_distinct_run_names_kept_intact(tmp_path: Path):
    """De-dup only collapses entries that SHARE a run_name. Two
    runs with different run_names must both survive — the canonical
    multi-day-baselines case."""
    _write_run(tmp_path, "baseline-2026-05-15")
    _write_run(tmp_path, "baseline-2026-05-19",
               started_at="2026-05-19T10:00:00Z")
    runs = os_mod.load_run_manifests(tmp_path)
    assert [r["run_name"] for r in runs] == [
        "baseline-2026-05-15", "baseline-2026-05-19",
    ]


# ───────── _compute_callouts ─────────────────────────────────────────


def test_compute_callouts_flags_regression_drop(tmp_path: Path):
    """A packet dropping >=10pp from prior run → regression callout."""
    _write_run(
        tmp_path, "baseline-old",
        started_at="2026-05-15T00:00:00Z",
        per_packet_pct={
            "stable": {"pct": 80.0}, "regressing": {"pct": 90.0},
        },
    )
    _write_run(
        tmp_path, "baseline-new",
        started_at="2026-05-16T00:00:00Z",
        per_packet_pct={
            "stable": {"pct": 78.0},      # -2pp: not flagged
            "regressing": {"pct": 70.0},  # -20pp: flagged
        },
    )
    runs = os_mod.load_run_manifests(tmp_path)
    regs, imps = os_mod._compute_callouts(runs)
    slugs = [r["packet_slug"] for r in regs]
    assert "regressing" in slugs
    assert "stable" not in slugs
    assert imps == []


def test_compute_callouts_flags_improvement_gain(tmp_path: Path):
    """A packet gaining >=10pp from prior run → improvement callout."""
    _write_run(
        tmp_path, "baseline-old",
        started_at="2026-05-15T00:00:00Z",
        per_packet_pct={"improving": {"pct": 50.0}},
    )
    _write_run(
        tmp_path, "baseline-new",
        started_at="2026-05-16T00:00:00Z",
        per_packet_pct={"improving": {"pct": 75.0}},  # +25pp
    )
    runs = os_mod.load_run_manifests(tmp_path)
    regs, imps = os_mod._compute_callouts(runs)
    assert regs == []
    assert len(imps) == 1
    assert imps[0]["packet_slug"] == "improving"
    assert imps[0]["delta_pp"] == 25.0


def test_compute_callouts_returns_empty_with_one_run(tmp_path: Path):
    """Can't compute deltas with a single run on disk."""
    _write_run(tmp_path, "baseline-only", started_at="2026-05-15T00:00:00Z")
    runs = os_mod.load_run_manifests(tmp_path)
    regs, imps = os_mod._compute_callouts(runs)
    assert regs == [] and imps == []


def test_compute_callouts_ignores_new_packets(tmp_path: Path):
    """A packet that exists in latest but not prior is a structural
    change, not a regression. Don't flag it either way."""
    _write_run(
        tmp_path, "baseline-old",
        started_at="2026-05-15T00:00:00Z",
        per_packet_pct={"existed": {"pct": 80.0}},
    )
    _write_run(
        tmp_path, "baseline-new",
        started_at="2026-05-16T00:00:00Z",
        per_packet_pct={
            "existed": {"pct": 81.0},
            "brand-new": {"pct": 0.0},  # 0% but didn't exist in prior — not flagged
        },
    )
    runs = os_mod.load_run_manifests(tmp_path)
    regs, imps = os_mod._compute_callouts(runs)
    assert all(r["packet_slug"] != "brand-new" for r in regs)


def test_compute_callouts_threshold_boundary_is_inclusive(tmp_path: Path):
    """Exactly -10.0pp / +10.0pp must trigger the callout (>= semantics)."""
    _write_run(
        tmp_path, "baseline-old",
        started_at="2026-05-15T00:00:00Z",
        per_packet_pct={
            "edge-down": {"pct": 80.0}, "edge-up": {"pct": 70.0}, "just-shy": {"pct": 80.0},
        },
    )
    _write_run(
        tmp_path, "baseline-new",
        started_at="2026-05-16T00:00:00Z",
        per_packet_pct={
            "edge-down": {"pct": 70.0},  # -10.0pp exactly
            "edge-up": {"pct": 80.0},     # +10.0pp exactly
            "just-shy": {"pct": 70.5},    # -9.5pp: NOT flagged
        },
    )
    runs = os_mod.load_run_manifests(tmp_path)
    regs, imps = os_mod._compute_callouts(runs)
    assert "edge-down" in [r["packet_slug"] for r in regs]
    assert "edge-up" in [r["packet_slug"] for r in imps]
    assert "just-shy" not in [r["packet_slug"] for r in regs]
    assert "just-shy" not in [r["packet_slug"] for r in imps]


# ───────── _format_md ────────────────────────────────────────────────


def test_format_md_has_no_runs_section_when_empty():
    out = os_mod._format_md([], [], [])
    assert "No runs on disk yet" in out
    # The Run history header is always there.
    assert "## Run history" in out


def test_format_md_includes_regression_callout(tmp_path: Path):
    _write_run(tmp_path, "baseline-old",
               started_at="2026-05-15T00:00:00Z",
               per_packet_pct={"sad": {"pct": 90.0}})
    _write_run(tmp_path, "baseline-new",
               started_at="2026-05-16T00:00:00Z",
               per_packet_pct={"sad": {"pct": 50.0}})
    runs = os_mod.load_run_manifests(tmp_path)
    regs, imps = os_mod._compute_callouts(runs)
    out = os_mod._format_md(runs, regs, imps)
    assert "Regressions" in out
    assert "sad" in out
    assert "-40.0pp" in out


def test_format_md_shows_delta_arrows_in_run_history(tmp_path: Path):
    _write_run(tmp_path, "baseline-a",
               started_at="2026-05-15T00:00:00Z", overall_pct=80.0)
    _write_run(tmp_path, "baseline-b",
               started_at="2026-05-16T00:00:00Z", overall_pct=85.0)
    _write_run(tmp_path, "baseline-c",
               started_at="2026-05-17T00:00:00Z", overall_pct=75.0)
    runs = os_mod.load_run_manifests(tmp_path)
    out = os_mod._format_md(runs, [], [])
    # First row: no delta yet.
    a_line = next(l for l in out.split("\n") if "baseline-a" in l)
    assert "—" in a_line
    # Second: up.
    b_line = next(l for l in out.split("\n") if "baseline-b" in l)
    assert "↑" in b_line and "+5.0pp" in b_line
    # Third: down.
    c_line = next(l for l in out.split("\n") if "baseline-c" in l)
    assert "↓" in c_line and "-10.0pp" in c_line


# ───────── _format_json ──────────────────────────────────────────────


def test_format_json_is_parseable_and_includes_runs_and_callouts(tmp_path: Path):
    _write_run(tmp_path, "baseline-1", started_at="2026-05-15T00:00:00Z",
               per_packet_pct={"p1": {"pct": 90.0}})
    _write_run(tmp_path, "baseline-2", started_at="2026-05-16T00:00:00Z",
               per_packet_pct={"p1": {"pct": 70.0}})
    runs = os_mod.load_run_manifests(tmp_path)
    regs, imps = os_mod._compute_callouts(runs)
    payload = json.loads(os_mod._format_json(runs, regs, imps))
    assert len(payload["runs"]) == 2
    assert payload["regressions"][0]["packet_slug"] == "p1"
    assert payload["regression_threshold_pp"] == os_mod.REGRESSION_THRESHOLD_PP
    assert payload["improvement_threshold_pp"] == os_mod.IMPROVEMENT_THRESHOLD_PP


# ───────── regenerate (end-to-end + atomic write) ────────────────────


def test_regenerate_writes_both_files(tmp_path: Path):
    _write_run(tmp_path, "baseline-2026-05-15", started_at="2026-05-15T00:00:00Z")
    md_path, json_path = os_mod.regenerate(tmp_path)
    assert md_path == tmp_path / "overall-summary.md"
    assert json_path == tmp_path / "overall-summary.json"
    assert md_path.exists() and json_path.exists()
    md = md_path.read_text()
    payload = json.loads(json_path.read_text())
    assert "baseline-2026-05-15" in md
    assert payload["runs"][0]["run_name"] == "baseline-2026-05-15"


def test_regenerate_overwrites_prior_contents(tmp_path: Path):
    """Idempotent: re-running with the same on-disk runs rewrites the
    dashboard to the same content; with new runs, picks them up."""
    _write_run(tmp_path, "baseline-1", started_at="2026-05-15T00:00:00Z")
    os_mod.regenerate(tmp_path)
    first_md = (tmp_path / "overall-summary.md").read_text()
    # Add a second run.
    _write_run(tmp_path, "baseline-2", started_at="2026-05-16T00:00:00Z")
    os_mod.regenerate(tmp_path)
    second_md = (tmp_path / "overall-summary.md").read_text()
    assert "baseline-2" in second_md
    assert second_md != first_md


def test_regenerate_atomic_write_leaves_no_tmp_files(tmp_path: Path):
    _write_run(tmp_path, "baseline-1", started_at="2026-05-15T00:00:00Z")
    os_mod.regenerate(tmp_path)
    leftovers = list(tmp_path.glob("*.tmp")) + list(tmp_path.glob("*.tmp.*"))
    assert leftovers == []


def test_regenerate_creates_root_if_absent(tmp_path: Path):
    """If shadow-runs/ doesn't exist yet, regenerate should still
    succeed (empty runs list) — but writing to a missing root needs
    the parent to exist."""
    root = tmp_path / "shadow-runs"
    # The function uses load_run_manifests which returns [] if root
    # missing, but writing to root/overall-summary.md requires root.
    # _atomic_write_text creates parent dirs.
    md_path, json_path = os_mod.regenerate(root)
    assert md_path.exists()
    assert "No runs on disk yet" in md_path.read_text()


# ─── PR #14: clean-denominator dashboard columns ─────────────────────


def test_regenerate_renders_clean_pct_for_pr14_runs(tmp_path: Path):
    """A manifest carrying clean_overall_pct + total_excluded should
    surface those columns in the markdown dashboard."""
    _write_run(
        tmp_path, "baseline-2026-05-19",
        started_at="2026-05-19T00:00:00Z",
        finished_at="2026-05-19T01:00:00Z",
        total_receipts=48,
        total_correct=32,
        overall_pct=66.7,
        clean_overall_pct=91.4,
        clean_total=35,
        total_excluded=13,
        total_skipped_receipt_stale=13,
    )
    md_path, _ = os_mod.regenerate(tmp_path)
    text = md_path.read_text()
    assert "66.7%" in text  # raw
    assert "91.4%" in text  # clean
    assert "Clean %" in text
    assert "Excluded" in text
    # The trailing note explaining the columns.
    assert "skipped_receipt_stale" in text


def test_regenerate_renders_clean_pct_as_na_for_legacy_runs(tmp_path: Path):
    """Pre-PR-14 manifests (no clean_overall_pct field) should render
    'n/a' in the Clean % column rather than crashing or showing 0."""
    _write_run(tmp_path, "baseline-2026-05-15", started_at="2026-05-15T10:00:00Z")
    md_path, _ = os_mod.regenerate(tmp_path)
    text = md_path.read_text()
    assert "80.0%" in text  # raw still rendered
    assert "n/a" in text  # clean column is n/a


def test_regenerate_clean_pct_lands_in_json(tmp_path: Path):
    """overall-summary.json must surface clean_overall_pct +
    total_excluded so the cross-run JSON is consumable by
    classification scripts (not just humans reading markdown)."""
    _write_run(
        tmp_path, "baseline-pr14",
        clean_overall_pct=91.4,
        clean_total=35,
        total_excluded=13,
        total_skipped_receipt_stale=13,
    )
    _, json_path = os_mod.regenerate(tmp_path)
    payload = json.loads(json_path.read_text())
    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["clean_overall_pct"] == 91.4
    assert run["clean_total"] == 35
    assert run["total_excluded"] == 13
    assert run["total_skipped_receipt_stale"] == 13


def test_regenerate_clean_pct_omitted_keeps_run_renderable(tmp_path: Path):
    """A legacy manifest (no clean_* fields) still produces a JSON
    entry — clean_overall_pct is None, totals default to 0."""
    _write_run(tmp_path, "baseline-legacy")
    _, json_path = os_mod.regenerate(tmp_path)
    payload = json.loads(json_path.read_text())
    run = payload["runs"][0]
    assert run["clean_overall_pct"] is None
    assert run["total_excluded"] == 0


# ─── PR #15: run-commit drift in dashboard ────────────────────────────


def test_regenerate_run_commit_drift_lands_in_json(tmp_path: Path):
    """A PR-#15 manifest carries total_skipped_run_commit_line_drift;
    the dashboard JSON exposes it for downstream charting."""
    _write_run(
        tmp_path, "baseline-pr15",
        clean_overall_pct=91.7,
        clean_total=11,
        total_excluded=1,
        total_skipped_receipt_stale=0,
        total_skipped_run_commit_line_drift=1,
    )
    _, json_path = os_mod.regenerate(tmp_path)
    payload = json.loads(json_path.read_text())
    run = payload["runs"][0]
    assert run["total_skipped_run_commit_line_drift"] == 1
    assert run["total_skipped_receipt_stale"] == 0


def test_regenerate_legacy_run_drift_defaults_zero(tmp_path: Path):
    """Pre-PR-15 manifests (no drift field) get zero, not None — the
    field has a defined semantic for legacy runs ('we didn't track
    drift then, so we report none')."""
    _write_run(tmp_path, "baseline-legacy")
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_skipped_run_commit_line_drift"] == 0


# ─── PR #17: non-retrieval skip totals in dashboard ───────────────────


def test_regenerate_pr17_skip_totals_land_in_json(tmp_path: Path):
    """PR #17's four non-retrieval skip totals surface in the
    overall-summary.json so consumers can chart each category
    independently."""
    _write_run(
        tmp_path, "baseline-pr17",
        total_skipped_non_repo_evidence=2,
        total_skipped_absence_search=1,
        total_skipped_unavailable_source_ref=3,
        total_skipped_doc_corpus_excluded=1,
    )
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_skipped_non_repo_evidence"] == 2
    assert run["total_skipped_absence_search"] == 1
    assert run["total_skipped_unavailable_source_ref"] == 3
    assert run["total_skipped_doc_corpus_excluded"] == 1


def test_regenerate_legacy_pr17_totals_default_zero(tmp_path: Path):
    """Pre-PR-17 manifests get zero for all four new totals."""
    _write_run(tmp_path, "baseline-legacy")
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_skipped_non_repo_evidence"] == 0
    assert run["total_skipped_absence_search"] == 0
    assert run["total_skipped_unavailable_source_ref"] == 0
    assert run["total_skipped_doc_corpus_excluded"] == 0


# ─── PR #20: command-snapshot lane in dashboard ───────────────────────


def test_regenerate_command_snapshot_total_lands_in_json(tmp_path: Path):
    _write_run(
        tmp_path, "baseline-pr20",
        total_skipped_command_snapshot=5,
    )
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_skipped_command_snapshot"] == 5


def test_regenerate_legacy_command_snapshot_defaults_zero(tmp_path: Path):
    _write_run(tmp_path, "baseline-legacy")
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_skipped_command_snapshot"] == 0


# ─── Per-evidence-type breakdown in dashboard ─────────────────────────


def _sample_breakdown(
    *,
    source_excerpt: tuple[int, int, int] = (0, 0, 0),
    external_tool_docs: tuple[int, int, int] = (0, 0, 0),
    user_context: tuple[int, int, int] = (0, 0, 0),
    absence_search: tuple[int, int, int] = (0, 0, 0),
    other: tuple[int, int, int] = (0, 0, 0),
) -> dict:
    """Build a breakdown dict matching the shape `_aggregate_run_totals`
    emits. Each tuple is (receipts, correct, excluded)."""
    def bucket(t):
        r, c, e = t
        clean_total = r - e
        return {
            "receipts": r, "correct": c, "excluded": e,
            "clean_total": clean_total,
            "clean_pct": (
                round(c * 100 / clean_total, 1)
                if clean_total > 0 else None
            ),
        }
    return {
        "source_excerpt": bucket(source_excerpt),
        "external_tool_docs": bucket(external_tool_docs),
        "user_context": bucket(user_context),
        "absence_search": bucket(absence_search),
        "other": bucket(other),
    }


def test_regenerate_by_evidence_type_lands_in_json(tmp_path: Path):
    """overall-summary.json must expose total_by_evidence_type per run
    so downstream tooling (charts, the upcoming probe-compare CLI) can
    consume the rollup directly."""
    bd = _sample_breakdown(
        source_excerpt=(40, 12, 5),
        external_tool_docs=(3, 0, 3),
        user_context=(2, 0, 2),
        absence_search=(1, 0, 1),
    )
    _write_run(tmp_path, "baseline-breakdown", total_by_evidence_type=bd)
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_by_evidence_type"] is not None
    assert run["total_by_evidence_type"]["source_excerpt"]["receipts"] == 40
    assert run["total_by_evidence_type"]["source_excerpt"]["clean_total"] == 35
    assert (
        run["total_by_evidence_type"]["external_tool_docs"]["clean_pct"]
        is None  # all excluded
    )


def test_regenerate_legacy_run_total_by_evidence_type_is_none(tmp_path: Path):
    """Pre-breakdown manifests get None (not a zero-filled stub) so
    consumers can distinguish 'no breakdown available' from 'all
    buckets at zero.'"""
    _write_run(tmp_path, "baseline-legacy")
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_by_evidence_type"] is None


def test_format_md_includes_evidence_type_section_when_breakdown_present(
    tmp_path: Path,
):
    """When the latest run carries a non-zero by-evidence-type
    breakdown, the markdown gains a 'Latest run — by evidence type'
    section listing each non-empty bucket."""
    bd = _sample_breakdown(
        source_excerpt=(40, 12, 5),
        external_tool_docs=(3, 0, 3),
    )
    _write_run(tmp_path, "baseline-pr20", total_by_evidence_type=bd)
    runs = os_mod.load_run_manifests(tmp_path)
    out = os_mod._format_md(runs, [], [])
    assert "## Latest run — by evidence type" in out
    # Check for the actual table rows (pipe-prefixed) rather than just
    # the bucket name — the explanatory blurb names every bucket so a
    # bare substring check would always match.
    assert "| `source_excerpt` |" in out
    assert "| `external_tool_docs` |" in out
    # user_context/absence_search/other had zero receipts → suppressed
    # from the table even though they're still named in the blurb.
    assert "| `user_context` |" not in out
    assert "| `absence_search` |" not in out
    assert "| `other` |" not in out


def test_format_md_skips_evidence_type_section_for_legacy_runs(tmp_path: Path):
    """If the latest run lacks the breakdown field entirely, the
    section is omitted (rather than rendering an empty table)."""
    _write_run(tmp_path, "baseline-legacy")
    runs = os_mod.load_run_manifests(tmp_path)
    out = os_mod._format_md(runs, [], [])
    assert "Latest run — by evidence type" not in out


def test_format_md_skips_evidence_type_section_when_all_zero(tmp_path: Path):
    """A breakdown with every bucket at zero (e.g. a run with zero
    receipts) doesn't render an empty section."""
    bd = _sample_breakdown()  # all defaults at zero
    _write_run(tmp_path, "baseline-zero", total_by_evidence_type=bd)
    runs = os_mod.load_run_manifests(tmp_path)
    out = os_mod._format_md(runs, [], [])
    assert "Latest run — by evidence type" not in out


# ─── Per-lane breakdown in dashboard (sibling of by-evidence-type) ────


def _sample_lane_breakdown(
    *,
    explicit_source_fast_path: tuple[int, int, int] = (0, 0, 0),
    fuzzy_find_code: tuple[int, int, int] = (0, 0, 0),
    scan_search: tuple[int, int, int] = (0, 0, 0),
    doc_resolver: tuple[int, int, int] = (0, 0, 0),
    non_retrieval: tuple[int, int, int] = (0, 0, 0),
    other: tuple[int, int, int] = (0, 0, 0),
) -> dict:
    """Build a lane breakdown dict matching the shape
    ``_aggregate_run_totals`` emits. Each tuple is
    (receipts, correct, excluded). Mirror of ``_sample_breakdown``."""
    def bucket(t):
        r, c, e = t
        clean_total = r - e
        return {
            "receipts": r, "correct": c, "excluded": e,
            "clean_total": clean_total,
            "clean_pct": (
                round(c * 100 / clean_total, 1)
                if clean_total > 0 else None
            ),
        }
    return {
        "explicit_source_fast_path": bucket(explicit_source_fast_path),
        "fuzzy_find_code": bucket(fuzzy_find_code),
        "scan_search": bucket(scan_search),
        "doc_resolver": bucket(doc_resolver),
        "non_retrieval": bucket(non_retrieval),
        "other": bucket(other),
    }


def test_regenerate_by_lane_lands_in_json(tmp_path: Path):
    """overall-summary.json must expose total_by_lane per run so
    downstream tooling (compare_runs, dashboards) can consume the
    rollup directly. Mirror of the by_evidence_type JSON test."""
    bd = _sample_lane_breakdown(
        explicit_source_fast_path=(30, 12, 0),
        doc_resolver=(15, 3, 0),
        non_retrieval=(8, 0, 8),
    )
    _write_run(tmp_path, "baseline-by-lane", total_by_lane=bd)
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_by_lane"] is not None
    assert run["total_by_lane"]["explicit_source_fast_path"][
        "receipts"
    ] == 30
    assert run["total_by_lane"]["explicit_source_fast_path"][
        "clean_pct"
    ] == 40.0
    assert run["total_by_lane"]["doc_resolver"]["clean_pct"] == 20.0
    # non_retrieval all excluded → clean_pct None
    assert run["total_by_lane"]["non_retrieval"]["clean_pct"] is None


def test_regenerate_legacy_run_total_by_lane_is_none(tmp_path: Path):
    """Pre-by-lane manifests get None (not a zero-filled stub) so
    consumers can distinguish 'no breakdown available' from 'all
    buckets at zero.' Mirror of the by_evidence_type back-compat test."""
    _write_run(tmp_path, "baseline-legacy")
    _, json_path = os_mod.regenerate(tmp_path)
    run = json.loads(json_path.read_text())["runs"][0]
    assert run["total_by_lane"] is None


def test_format_md_includes_lane_section_when_breakdown_present(
    tmp_path: Path,
):
    """When the latest run carries a non-zero by-lane breakdown, the
    markdown gains a 'Latest run — by retrieval lane' section listing
    each non-empty bucket."""
    bd = _sample_lane_breakdown(
        explicit_source_fast_path=(20, 8, 2),
        doc_resolver=(10, 1, 0),
    )
    _write_run(tmp_path, "baseline-by-lane", total_by_lane=bd)
    runs = os_mod.load_run_manifests(tmp_path)
    out = os_mod._format_md(runs, [], [])
    assert "## Latest run — by retrieval lane" in out
    # Pipe-prefix match avoids matching the same names in the
    # explanatory blurb below the table.
    assert "| `explicit_source_fast_path` |" in out
    assert "| `doc_resolver` |" in out
    # Zero-receipt buckets suppressed from the table.
    assert "| `fuzzy_find_code` |" not in out
    assert "| `scan_search` |" not in out
    assert "| `non_retrieval` |" not in out
    assert "| `other` |" not in out


def test_format_md_skips_lane_section_for_legacy_runs(tmp_path: Path):
    """If the latest run lacks the breakdown field entirely, the
    section is omitted (rather than rendering an empty table). Mirror
    of the by_evidence_type back-compat MD test."""
    _write_run(tmp_path, "baseline-legacy")
    runs = os_mod.load_run_manifests(tmp_path)
    out = os_mod._format_md(runs, [], [])
    assert "Latest run — by retrieval lane" not in out


def test_format_md_skips_lane_section_when_all_zero(tmp_path: Path):
    """A breakdown with every bucket at zero doesn't render an empty
    section. Mirror of the all-zero by_evidence_type MD test."""
    bd = _sample_lane_breakdown()  # all defaults at zero
    _write_run(tmp_path, "baseline-zero", total_by_lane=bd)
    runs = os_mod.load_run_manifests(tmp_path)
    out = os_mod._format_md(runs, [], [])
    assert "Latest run — by retrieval lane" not in out


# ─── Counted-misses dashboard integration ────────────────────────────


def _write_run_with_artifact_row(
    root: Path,
    run_name: str,
    *,
    row: dict,
    started_at: str = "2026-05-19T00:00:00Z",
) -> Path:
    """Materialize a baseline dir with one artifact carrying one row.

    Used to exercise the counted_misses auto-regen path — the report
    builder reads ``<run>/artifacts/*.json`` and pulls per-receipt
    rows out of them.
    """
    run_dir = root / run_name
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_name": run_name,
        "commit_sha": "abc1234",
        "code_revision_id": "rev",
        "started_at": started_at,
        "finished_at": "2026-05-19T01:00:00Z",
        "total_packets": 1,
        "total_receipts": 1,
        "total_correct": 0,
        "overall_pct": 0.0,
        "grader_backend": "claude_cli",
        "grader_model": "sonnet",
        "per_packet_pct": {"p1": {"receipts": 1, "correct": 0, "pct": 0.0}},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    artifact = {
        "packet_id": "p1",
        "schema_version": "1.0",
        "rows": [row],
    }
    (run_dir / "artifacts" / "pr-0-Z-p1.json").write_text(json.dumps(artifact))
    return run_dir


def _counted_miss_row(**overrides) -> dict:
    """A row shaped like a counted clean-denominator miss.

    Default lane = explicit_source_fast_path, hash_mismatch on both
    snapshots → fix_layer = receipt_anchor_mismatch.
    """
    base = {
        "question_id": "q1",
        "question": "what does this function do?",
        "grade": "no_match",
        "tool": "find_code",
        "score_status": "counted",
        "lane": "explicit_source_fast_path",
        "evidence_type": "source_excerpt",
        "source_snapshot_status": "git_source_hash_mismatch",
        "run_snapshot_status": "run_commit_hash_mismatch",
        "atlas_citation_locations": ["foo.py:10-10"],
        "atlas_citation_count": 1,
        "atlas_answer_len": 120,
        "rationale": "atlas returned single comment line, oracle wants function body",
    }
    base.update(overrides)
    return base


def test_ensure_counted_misses_report_returns_none_for_legacy_run(
    tmp_path: Path,
):
    """A baseline without an ``artifacts/`` dir (legacy aggregate-only
    run) returns None and does NOT create an empty _counted_misses/."""
    _write_run(tmp_path, "baseline-legacy")  # no artifacts/
    result = os_mod._ensure_counted_misses_report(tmp_path / "baseline-legacy")
    assert result is None
    assert not (tmp_path / "baseline-legacy" / "_counted_misses").exists()


def test_ensure_counted_misses_report_builds_for_modern_run(tmp_path: Path):
    """A baseline WITH artifacts/ gets _counted_misses/counted-misses.md
    + .json generated, and the parsed JSON payload is returned."""
    _write_run_with_artifact_row(
        tmp_path, "baseline-modern", row=_counted_miss_row(),
    )
    payload = os_mod._ensure_counted_misses_report(tmp_path / "baseline-modern")
    assert payload is not None
    assert payload["total_misses"] == 1
    assert payload["by_lane"] == {"explicit_source_fast_path": 1}
    assert payload["by_fix_layer"] == {"receipt_anchor_mismatch": 1}
    cm_dir = tmp_path / "baseline-modern" / "_counted_misses"
    assert (cm_dir / "counted-misses.md").is_file()
    assert (cm_dir / "counted-misses.json").is_file()


def test_ensure_counted_misses_report_swallows_errors(tmp_path: Path, capsys):
    """A corrupt artifact file shouldn't crash the dashboard regen.
    ``_ensure_counted_misses_report`` logs a warning and returns None."""
    run_dir = tmp_path / "baseline-corrupt"
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    # Write the manifest so load_run_manifests would accept the dir
    # but break the artifact JSON so build_report raises.
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_name": "baseline-corrupt",
        "started_at": "2026-05-19T00:00:00Z",
    }))
    (run_dir / "artifacts" / "pr-0-Z-bad.json").write_text("not valid json {{{")
    result = os_mod._ensure_counted_misses_report(run_dir)
    assert result is None
    err = capsys.readouterr().err
    assert "counted_misses regen failed" in err
    assert "baseline-corrupt" in err


def test_format_md_includes_counted_misses_section_when_misses_present(
    tmp_path: Path,
):
    """The dashboard MD gains a 'Latest run — counted misses by fix
    layer' section when the latest run has counted misses."""
    _write_run_with_artifact_row(
        tmp_path, "baseline-with-misses", row=_counted_miss_row(),
    )
    md_path, _ = os_mod.regenerate(tmp_path)
    out = md_path.read_text()
    assert "## Latest run — counted misses by fix layer" in out
    assert "**1 counted misses**" in out
    assert "| `receipt_anchor_mismatch` | 1 |" in out
    # Link to the per-row detail file.
    assert (
        "baseline-with-misses/_counted_misses/counted-misses.md" in out
    )


def test_format_md_skips_counted_misses_section_for_legacy_runs(tmp_path: Path):
    """If the latest run has no artifacts/, the dashboard skips the
    counted-misses section (rather than rendering an empty one)."""
    _write_run(tmp_path, "baseline-legacy")  # no artifacts/
    md_path, _ = os_mod.regenerate(tmp_path)
    assert "Latest run — counted misses" not in md_path.read_text()


def test_format_md_skips_counted_misses_section_when_zero_misses(
    tmp_path: Path,
):
    """A run with artifacts/ but no counted misses (everything passed
    or got correctly skipped) suppresses the section — empty tables
    just add noise."""
    _write_run_with_artifact_row(
        tmp_path, "baseline-clean",
        row=_counted_miss_row(grade="full_match"),  # passes → not a miss
    )
    md_path, _ = os_mod.regenerate(tmp_path)
    assert "Latest run — counted misses" not in md_path.read_text()


def test_format_json_exposes_counted_misses_per_run(tmp_path: Path):
    """overall-summary.json carries the counted_misses summary
    (total_misses + by_lane + by_fix_layer) per run."""
    _write_run_with_artifact_row(
        tmp_path, "baseline-with-misses", row=_counted_miss_row(),
    )
    _, json_path = os_mod.regenerate(tmp_path)
    payload = json.loads(json_path.read_text())
    run = payload["runs"][0]
    assert run["counted_misses"] is not None
    assert run["counted_misses"]["total_misses"] == 1
    assert run["counted_misses"]["by_lane"] == {"explicit_source_fast_path": 1}
    assert run["counted_misses"]["by_fix_layer"] == {
        "receipt_anchor_mismatch": 1,
    }


def test_format_json_counted_misses_none_for_legacy_runs(tmp_path: Path):
    """Legacy runs (no artifacts/) get counted_misses=null in the JSON
    so consumers can distinguish 'unavailable' from 'zero'."""
    _write_run(tmp_path, "baseline-legacy")
    _, json_path = os_mod.regenerate(tmp_path)
    payload = json.loads(json_path.read_text())
    assert payload["runs"][0]["counted_misses"] is None


def test_regenerate_creates_counted_misses_dir_for_each_modern_run(
    tmp_path: Path,
):
    """End-to-end: regenerate() walks every baseline-*/ with
    artifacts/ and creates _counted_misses/ on disk. Legacy runs are
    left alone (no empty subdir created)."""
    _write_run_with_artifact_row(
        tmp_path, "baseline-modern", row=_counted_miss_row(),
        started_at="2026-05-19T00:00:00Z",
    )
    _write_run(tmp_path, "baseline-legacy")
    os_mod.regenerate(tmp_path)
    # Modern run got the dir + files.
    assert (
        tmp_path / "baseline-modern" / "_counted_misses" / "counted-misses.md"
    ).is_file()
    assert (
        tmp_path / "baseline-modern" / "_counted_misses" / "counted-misses.json"
    ).is_file()
    # Legacy run was skipped entirely.
    assert not (tmp_path / "baseline-legacy" / "_counted_misses").exists()
