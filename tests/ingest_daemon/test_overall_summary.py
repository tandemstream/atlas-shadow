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
