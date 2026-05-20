"""cli — entry points for the make targets.

Top-level subcommands:

- ``shadow-run``           — parse fixture → run Atlas → grade → write JSONL
- ``shadow-grade``         — re-grade an existing pre-grade JSONL (rare;
                             used to swap grader models without
                             re-querying Atlas)
- ``shadow-aggregate``     — write the cross-packet comparison report
- ``shadow-compare-runs``  — diff two ``shadow-runs/baseline-*`` runs
                             (probe-comparison tool — see
                             :mod:`atlas_shadow.ingest_daemon.compare_runs`)

The shadow-run path is the main one. Out-of-band mode (`--commit <sha>`)
invokes :mod:`atlas_shadow.ingest` first to spin up a fresh org at that
commit, then routes the batch through that new org_id.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click
import yaml

from . import aggregate as aggregate_mod
from . import grader as grader_mod
from . import ingest as ingest_mod
from . import parser as parser_mod
from . import runner as runner_mod
from .ingest_daemon import compare_runs as compare_runs_mod
from .ingest_daemon import counted_misses as counted_misses_mod
from .ingest_daemon import layered_report as layered_report_mod


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise click.ClickException(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    if not isinstance(data, dict):
        raise click.ClickException(f"config must be a YAML mapping: {path}")
    return data


def _fixture_path(fixture: str) -> Path:
    """Resolve a fixture name to a path in tests/fixtures/."""
    here = Path(__file__).resolve().parent.parent
    # Allow both bare names and absolute paths
    candidate = here / "tests" / "fixtures" / f"{fixture}.jsonl"
    if candidate.exists():
        return candidate
    candidate_md = here / "tests" / "fixtures" / f"{fixture}.md"
    if candidate_md.exists():
        return candidate_md
    # Maybe it's already a path
    p = Path(fixture)
    if p.exists():
        return p
    raise click.ClickException(f"fixture not found: {fixture}")


def _to_json(obj: Any) -> Any:
    """Recursively convert dataclasses + primitives to JSON-safe types."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json(v) for v in obj]
    if is_dataclass(obj):
        return _to_json(asdict(obj))
    if hasattr(obj, "__dict__"):
        return _to_json(vars(obj))
    return repr(obj)


@click.group()
def main() -> None:
    """atlas-shadow CLI."""


@main.command("shadow-run")
@click.option("--fixture", required=True, help="Fixture name (without extension) or absolute path.")
@click.option("--config", "config_path", default="shadow-config.yaml", show_default=True, type=click.Path(path_type=Path))
@click.option("--commit", default=None, help="Out-of-band: ingest at this commit before running.")
@click.option("--tool", default="auto", show_default=True, type=click.Choice(["answer", "find_code", "scan_search", "auto"]))
@click.option("--domain-pack", default="code", show_default=True, help="Domain pack hint for find_code/scan_search.")
@click.option("--no-grade", is_flag=True, help="Skip grader; write atlas-only responses.")
@click.option("--limit", type=int, default=None, help="Run only the first N receipts (for cheap smoketests).")
@click.option("--timeout", type=int, default=180, show_default=True, help="Per-query timeout in seconds.")
def shadow_run(
    fixture: str,
    config_path: Path,
    commit: Optional[str],
    tool: str,
    domain_pack: str,
    no_grade: bool,
    limit: Optional[int],
    timeout: int,
) -> None:
    """Run the benchmark for FIXTURE and write atlas-qa-shadow.jsonl."""
    config = _load_config(config_path)
    fixture_path = _fixture_path(fixture)
    receipts = parser_mod.parse_fixture(fixture_path)
    if limit:
        receipts = receipts[:limit]
    if not receipts:
        raise click.ClickException(f"no receipts parsed from {fixture_path}")

    core_repo_path = Path(config["core_repo_path"]).expanduser()
    grader_model = config.get("grader_model") or "claude-3-5-sonnet-20241022"
    principal_id = config.get("default_principal_id")
    # D5 freshness handoff: prefer ingest-daemon's state file when present,
    # fall back to the pinned config value (amendment decision #1).
    code_revision_id = runner_mod.resolve_code_revision_id(
        config, config_path=Path(config_path)
    )

    if commit:
        # Out-of-band mode: ingest at <commit>, get a fresh org_id, run against it.
        from . import ingest as ingest_mod

        click.echo(f"[atlas-shadow] out-of-band ingest @ {commit}", err=True)
        ingest_result = ingest_mod.ensure_org_for_commit(
            commit_sha=commit,
            core_repo_path=core_repo_path,
            template_org_id=config.get("one_off_template_org_id"),
        )
        org_id = ingest_result["org_id"]
        code_revision_id = ingest_result.get("code_revision_id") or code_revision_id
        click.echo(
            f"[atlas-shadow] ingest done: org_id={org_id} "
            f"code_revision_id={code_revision_id} "
            f"latency_ms={ingest_result.get('latency_ms')}",
            err=True,
        )
    else:
        org_id = config.get("continuous_shadow_org_id")
        if not org_id:
            raise click.ClickException(
                "continuous_shadow_org_id missing in config; "
                "either set it or pass --commit <sha> for out-of-band mode."
            )

    out_dir = Path("shadow-runs") / fixture
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "atlas-qa-shadow.jsonl"

    def _progress(i: int, n: int, resp: runner_mod.ShadowResponse) -> None:
        a = resp.atlas_response
        click.echo(
            f"[atlas-shadow] {i}/{n} {resp.question_id} "
            f"tool={a.tool_used or '?'} rc={a.returncode} "
            f"answer_len={len(a.answer_text or '')} "
            f"latency_ms={a.atlas_latency_ms}",
            err=True,
        )

    responses = runner_mod.run_batch(
        receipts,
        fixture_id=fixture,
        org_id=str(org_id),
        core_repo_path=core_repo_path,
        tool=tool,
        principal_id=principal_id,
        domain_pack=domain_pack,
        code_revision_id=code_revision_id,
        timeout=timeout,
        progress_cb=_progress,
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    records: list[dict[str, Any]] = []
    for receipt, resp in zip(receipts, responses):
        grader_result: Optional[dict[str, Any]] = None
        if not no_grade:
            g = grader_mod.grade(
                question=receipt.question,
                oracle_excerpt=receipt.oracle_excerpt,
                oracle_claim=receipt.oracle_claim,
                atlas_answer_text=resp.atlas_response.answer_text,
                model=grader_model,
                api_key=api_key,
            )
            grader_result = _to_json(g)
        record = {
            "question_id": resp.question_id,
            "question": resp.question,
            "fixture_id": resp.fixture_id,
            "class_label": receipt.class_label,
            "oracle": {
                "source_path": receipt.source_path,
                "source_lines": receipt.source_lines,
                "commit_sha": receipt.commit_sha,
                "excerpt": receipt.oracle_excerpt,
                "claim": receipt.oracle_claim,
            },
            "atlas_response": _to_json(resp.atlas_response),
            "grader_response": grader_result,
            "wall_time_ms": resp.wall_time_ms,
            "captured_at": resp.captured_at,
            "org_id": resp.org_id,
            "tool_requested": resp.tool,
        }
        records.append(record)

    with out_path.open("w", encoding="utf-8") as fp:
        for r in records:
            fp.write(json.dumps(r, sort_keys=True, default=str) + "\n")

    click.echo(f"[atlas-shadow] wrote {len(records)} records to {out_path}", err=True)


@main.command("shadow-grade")
@click.option("--fixture", required=True)
@click.option("--config", "config_path", default="shadow-config.yaml", show_default=True, type=click.Path(path_type=Path))
def shadow_grade(fixture: str, config_path: Path) -> None:
    """Re-grade an existing atlas-qa-shadow.jsonl in place.

    Useful when swapping grader models or re-trying parse failures without
    re-querying Atlas. Reads the existing file, regrades each row's
    ``atlas_response.answer_text`` against ``oracle.excerpt`` + claim, and
    overwrites the file.
    """
    config = _load_config(config_path)
    grader_model = config.get("grader_model") or "claude-3-5-sonnet-20241022"
    target = Path("shadow-runs") / fixture / "atlas-qa-shadow.jsonl"
    if not target.exists():
        raise click.ClickException(f"target not found: {target}")
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    for row in rows:
        atlas = row.get("atlas_response") or {}
        oracle = row.get("oracle") or {}
        g = grader_mod.grade(
            question=row.get("question", ""),
            oracle_excerpt=oracle.get("excerpt", ""),
            oracle_claim=oracle.get("claim", ""),
            atlas_answer_text=atlas.get("answer_text", ""),
            model=grader_model,
            api_key=api_key,
        )
        row["grader_response"] = _to_json(g)
    with target.open("w", encoding="utf-8") as fp:
        for r in rows:
            fp.write(json.dumps(r, sort_keys=True, default=str) + "\n")
    click.echo(f"[atlas-shadow] re-graded {len(rows)} rows in {target}", err=True)


@main.command("shadow-aggregate")
@click.option("--config", "config_path", default="shadow-config.yaml", show_default=True, type=click.Path(path_type=Path))
@click.option("--shadow-runs-dir", default="shadow-runs", show_default=True, type=click.Path(path_type=Path))
@click.option("--out", "out_path", default="shadow-runs/_aggregate/comparison-report.md", show_default=True, type=click.Path(path_type=Path))
def shadow_aggregate(config_path: Path, shadow_runs_dir: Path, out_path: Path) -> None:
    """Write the cross-packet comparison report."""
    _ = config_path  # currently unused; reserved for future grader-model-aware reports
    summary = aggregate_mod.aggregate(shadow_runs_dir, out_path)
    click.echo(
        f"[atlas-shadow] wrote {summary['report_path']} "
        f"(total_packets={summary['total_packets']})",
        err=True,
    )


@main.command("shadow-compare-runs")
@click.option(
    "--before",
    "before_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the BEFORE run's baseline-* directory.",
)
@click.option(
    "--after",
    "after_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to the AFTER run's baseline-* directory.",
)
@click.option(
    "--output-dir",
    "output_dir",
    required=False,
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help=(
        "Directory to write comparison-report.{md,json} into. Defaults "
        "to shadow-runs/_compare/<before>-vs-<after>/."
    ),
)
def shadow_compare_runs(
    before_dir: Path,
    after_dir: Path,
    output_dir: Optional[Path],
) -> None:
    """Diff two shadow-runs/baseline-* directories.

    Loads each run's manifest.json + per-packet JSONs, classifies
    per-receipt transitions (newly_passing / newly_failing /
    newly_skipped / un_skipped_* / etc.), and writes a Markdown
    report + JSON payload under ``--output-dir`` (or the default
    ``shadow-runs/_compare/<before>-vs-<after>/``).

    Typical workflow: re-run a probe after landing a tuning PR, then::

        .venv/bin/python -m atlas_shadow shadow-compare-runs \\
            --before shadow-runs/baseline-2026-05-15 \\
            --after  shadow-runs/baseline-2026-05-20

    The Markdown report leads with run-level deltas, then breaks down
    by skip category, evidence type, and per-packet receipt
    transitions — useful for confirming "did q12 exit the denominator
    via #20?" or "where did the new passes come from?"
    """
    comparison = compare_runs_mod.compare_runs(before_dir, after_dir)
    if output_dir is None:
        # Conventional default location alongside the runs themselves.
        # Both run dirs are expected to live under ``shadow-runs/`` so
        # walking up two parents from the after-run lands at
        # ``shadow-runs/``.
        shadow_runs_root = after_dir.parent
        slug = f"{comparison.before_run_name}-vs-{comparison.after_run_name}"
        output_dir = shadow_runs_root / "_compare" / slug
    md_path, json_path = compare_runs_mod.write_reports(comparison, output_dir)
    click.echo(
        f"[atlas-shadow] wrote {md_path} and {json_path}",
        err=True,
    )
    # Surface the headline numbers on stdout for quick scripting.
    click.echo(
        f"clean: {_fmt_pct(comparison.before_clean_pct)} → "
        f"{_fmt_pct(comparison.after_clean_pct)} "
        f"(Δ {comparison.clean_pct_delta_pp})"
    )


@main.command("shadow-counted-misses")
@click.option(
    "--run-dir",
    "run_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to a shadow-runs/<run>/ directory with manifest.json + artifacts/.",
)
@click.option(
    "--output-dir",
    "output_dir",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help=(
        "Directory to write counted-misses.{md,json}. Defaults to "
        "<run-dir>/_counted_misses/."
    ),
)
def shadow_counted_misses(run_dir: Path, output_dir: Optional[Path]) -> None:
    """Write the clean-denominator miss worklist for one run.

    The report contains only rows with ``score_status=counted`` and a
    non-pass grade. This is the high-signal queue for Atlas tuning after
    stale receipts, command snapshots, and non-retrieval evidence have
    been excluded from the clean denominator.
    """
    if output_dir is None:
        output_dir = run_dir / "_counted_misses"
    report = counted_misses_mod.build_report(run_dir)
    md_path, json_path = counted_misses_mod.write_reports(report, output_dir)
    click.echo(f"[atlas-shadow] wrote {md_path} and {json_path}", err=True)
    click.echo(
        f"counted_misses={report.total_misses} "
        f"by_lane={json.dumps(report.by_lane, sort_keys=True)}"
    )


@main.command("shadow-layered-report")
@click.option(
    "--packet-json",
    "packet_json_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to one grade-packet-batch packets/<packet>.json file.",
)
@click.option(
    "--oracle-spec",
    "oracle_spec_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the typed layered-oracle YAML spec for the packet.",
)
@click.option(
    "--output-dir",
    "output_dir",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help=(
        "Directory to write layered-shadow-report.{md,json}. Defaults "
        "to <packet-json-dir>/../_layered/<packet>/."
    ),
)
def shadow_layered_report(
    packet_json_path: Path,
    oracle_spec_path: Path,
    output_dir: Optional[Path],
) -> None:
    """Render a Phase-1 layered shadow report for one packet.

    This is the pilot command for workflow-level scoring. It keeps the
    current Atlas evidence rows intact, then overlays a typed oracle spec
    that separates evidence-oracle coverage, planner evidence, planner
    synthesis, Atlas evidence, Atlas synthesis, and cost.
    """
    report = layered_report_mod.build_report(
        spec_path=oracle_spec_path,
        packet_json_path=packet_json_path,
    )
    if output_dir is None:
        packet_slug = packet_json_path.stem
        output_dir = packet_json_path.parent.parent / "_layered" / packet_slug
    md_path, json_path = layered_report_mod.write_reports(report, output_dir)
    click.echo(f"[atlas-shadow] wrote {md_path} and {json_path}", err=True)
    click.echo(
        f"oracle={report.oracle_verified} verified + "
        f"{report.context_verified} context + "
        f"{report.command_verified} command + "
        f"{report.oracle_unresolved} unresolved; "
        f"planner_evidence={report.planner_evidence_pass}/"
        f"{report.planner_evidence_total}; "
        f"atlas_evidence={report.atlas_evidence_pass}/"
        f"{report.atlas_evidence_total}"
    )


@main.command("shadow-layered-batch")
@click.option(
    "--run-dir",
    "run_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Run directory containing packets/*.json.",
)
@click.option(
    "--oracle-dir",
    "oracle_dir",
    default=Path("docs/pilots"),
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory containing <packet>-layered-oracle.yaml specs.",
)
@click.option(
    "--output-dir",
    "output_dir",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Directory to write reports. Defaults to <run-dir>/_layered/.",
)
def shadow_layered_batch(
    run_dir: Path,
    oracle_dir: Path,
    output_dir: Optional[Path],
) -> None:
    """Render layered reports for every packet in a run directory."""
    packet_dir = run_dir / "packets"
    packet_paths = sorted(packet_dir.glob("*.json"))
    if not packet_paths:
        raise click.ClickException(f"no packet JSON files found under {packet_dir}")
    if output_dir is None:
        output_dir = run_dir / "_layered"

    reports = []
    missing_specs = []
    for packet_path in packet_paths:
        packet_slug = packet_path.stem
        spec_path = oracle_dir / f"{packet_slug}-layered-oracle.yaml"
        if not spec_path.exists():
            missing_specs.append(str(spec_path))
            continue
        report = layered_report_mod.build_report(
            spec_path=spec_path,
            packet_json_path=packet_path,
        )
        layered_report_mod.write_reports(report, output_dir / packet_slug)
        reports.append(report)

    if missing_specs:
        raise click.ClickException(
            "missing layered oracle specs:\n" + "\n".join(missing_specs)
        )
    md_path, json_path = layered_report_mod.write_run_summary(reports, output_dir)
    audit_md_path, audit_json_path = layered_report_mod.write_synthesis_audit(
        reports,
        output_dir,
    )
    click.echo(f"[atlas-shadow] wrote {md_path} and {json_path}", err=True)
    click.echo(
        f"[atlas-shadow] wrote {audit_md_path} and {audit_json_path}",
        err=True,
    )
    click.echo(f"layered_packets={len(reports)}")


def _fmt_pct(v: Optional[float]) -> str:
    """Tiny CLI-local helper. The renderer module has its own copy
    for its internal use; we don't re-export it to avoid a public
    surface that callers might depend on."""
    if not isinstance(v, (int, float)):
        return "n/a"
    return f"{v:.1f}%"


@main.command("purge-orphans")
@click.option(
    "--config",
    "config_path",
    default="shadow-config.yaml",
    show_default=True,
    type=click.Path(path_type=Path),
)
@click.option("--dry-run", is_flag=True, help="List orphans without deleting.")
@click.option(
    "--include-cached",
    is_flag=True,
    help=(
        "Also consider cached entries for deletion. Without this flag, "
        "purge only touches shadow orgs absent from .ingest-cache.json — "
        "i.e., orphans from crashes that escaped the auto-rollback path."
    ),
)
def purge_orphans(config_path: Path, dry_run: bool, include_cached: bool) -> None:
    """Find and delete orphan ``atlas_shadow_*`` orgs in Atlas's DB.

    Catches orgs that escaped the auto-rollback inside
    ``ensure_org_for_commit`` (e.g., crashes / kill signals during an
    ingest). Refuses to delete any org with rows in the ingest-touched
    tables — those go on a "manual review" list instead.

    Without ``--dry-run`` the command actually deletes orphans. Without
    ``--include-cached`` it skips orgs the local cache recognizes (those
    represent successful ingests and shouldn't be touched).
    """
    config = _load_config(config_path)
    core_repo_path = Path(config["core_repo_path"]).expanduser()

    cache = ingest_mod.load_cache()
    cached_org_ids = {entry["org_id"] for entry in cache.values()}

    shadow_orgs = ingest_mod.list_shadow_orgs(core_repo_path=core_repo_path)
    if include_cached:
        candidates = list(shadow_orgs)
    else:
        candidates = [o for o in shadow_orgs if o["org_id"] not in cached_org_ids]

    if not candidates:
        click.echo(
            f"[atlas-shadow] no orphan shadow orgs detected "
            f"(scanned {len(shadow_orgs)} matching 'atlas_shadow_*'; "
            f"{len(cached_org_ids)} in cache).",
            err=True,
        )
        return

    click.echo(
        f"[atlas-shadow] found {len(candidates)} orphan candidate(s):",
        err=True,
    )
    for o in candidates:
        click.echo(
            f"  - {o['name']} {o['org_id']} (created={o.get('created_at')})",
            err=True,
        )

    if dry_run:
        click.echo("[atlas-shadow] --dry-run: no deletions.", err=True)
        return

    deleted = 0
    kept = 0
    errored = 0
    for o in candidates:
        try:
            result = ingest_mod.delete_org(
                core_repo_path=core_repo_path, org_id=o["org_id"]
            )
        except Exception as exc:
            click.echo(f"  ERROR {o['org_id']}: {exc}", err=True)
            errored += 1
            continue
        if result.get("deleted"):
            click.echo(f"  DELETED {o['org_id']} ({result.get('name')})", err=True)
            deleted += 1
        else:
            click.echo(
                f"  KEPT {o['org_id']}: {result.get('reason')} "
                f"non_pristine={result.get('non_pristine')}",
                err=True,
            )
            kept += 1

    click.echo(
        f"[atlas-shadow] purge complete: deleted={deleted} "
        f"kept={kept} errored={errored}",
        err=True,
    )


if __name__ == "__main__":
    main()
