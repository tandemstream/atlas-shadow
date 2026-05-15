"""grade_batch — offline batch grading of every packet in core.

Loops over every ``02-qna-log.md`` file in a core checkout, runs the
same ``grader_service.run_pr_grading`` orchestrator used by the live
gate (P2 v1) — but with all GitHub side-effects stubbed out — and
writes one JSON file per packet under
``shadow-runs/baseline-<date>/packets/<packet>.json`` plus a top-level
``manifest.json`` + ``summary.md`` for the run.

After the batch finishes, ``overall_summary.regenerate`` is invoked
to rewrite the cross-run dashboard at
``shadow-runs/overall-summary.{md,json}`` from every
``baseline-*/manifest.json`` on disk. That dashboard is the "how is
atlas doing overall, over time" view — it carries aggregate per-run
totals + regression callouts (>10pp drops from the previous run).

This module is the offline batch counterpart to the live webhook-
driven path. The two share ``run_pr_grading`` so grading semantics
stay identical; only the inputs (synthetic ``PrEvent`` per packet
vs. real GitHub webhook payload) and outputs (filesystem JSON vs.
posted commit status + PR comment) differ.

Usage::

    cd <atlas-shadow-checkout>
    set -a && source <atlas-runtime.env> && set +a
    set -a && source <core-atlas-leaf>/.env && set +a
    export GITHUB_WEBHOOK_SECRET="$(cat ~/.atlas-shadow/webhook.secret)"
    export ATLAS_SHADOW_GRADER_BACKEND=claude_cli

    .venv/bin/python -m atlas_shadow.ingest_daemon \\
        --config shadow-config.yaml \\
        grade-packet-batch \\
        --core-repo-path /Users/ray/tandemstream/core--shadow-runtime \\
        --commit-sha 2344d204671f3c644ffef5a026eb273824ef77a4 \\
        --output-dir shadow-runs/baseline-2026-05-15

The script is idempotent: re-running with the same ``--output-dir``
overwrites that run's artifacts but preserves earlier runs. The
overall-summary regen is similarly idempotent — it always rebuilds
from on-disk ``baseline-*/manifest.json`` files.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import grader_service as gs
from . import overall_summary as os_mod
from .receiver import PrEvent


# Statuses set by ``grader_service.run_pr_grading`` that count as
# "successful" from the batch CLI's perspective. Anything else is a
# partial failure that should bump the exit code to 2.
_OK_STATUSES = frozenset(["ok"])


# ─── Synthesizing a PrEvent for a single packet ──────────────────────


def _synthesize_pr_event(
    *,
    repo_full_name: str,
    commit_sha: str,
    packet_qna_log_path: str,
    pr_number: int = 0,
) -> PrEvent:
    """Build a ``PrEvent`` that tells ``run_pr_grading`` "this packet's
    qna log is the only changed file".

    ``pr_number`` defaults to 0 so any code path that types it into a
    URL or comment context gets a clearly-synthetic value. The batch
    never actually posts to GitHub, so the value is cosmetic.
    """
    return PrEvent(
        action="opened",
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        base_sha=commit_sha,
        base_ref="main",
        head_sha=commit_sha,
        head_ref=f"batch-grade@{commit_sha[:7]}",
        title=f"[batch grade] {packet_qna_log_path}",
        html_url=f"file://{packet_qna_log_path}",
    )


def _stub_fetch_pr_files(
    *,
    repo_full_name: str,
    pr_number: int,
    github_token: str,
    qna_log_path: str,
) -> list[dict[str, Any]]:
    """Replacement for ``grader_service._fetch_pr_files``.

    The real call hits ``GET /repos/.../pulls/{n}/files``. For batch
    mode there's no real PR — we synthesize the file list as a single
    modified entry pointing at the packet's qna log. The orchestrator's
    ``detect_packet_qna_log`` then finds it and proceeds normally.
    """
    return [{"filename": qna_log_path, "status": "modified"}]


def _read_file_via_git_show(
    *,
    core_repo_path: Path,
    path: str,
    ref: str,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Optional[str]:
    """Read ``<ref>:<path>`` from the local git object DB.

    Returns the decoded text on success, ``None`` on git error (matches
    the contract of ``grader_service._fetch_file_at_ref``, which returns
    ``None`` on 404). Used as the offline replacement for the GitHub
    Contents API call in batch mode (codex r2 P1 fix). Reads from the
    object DB rather than the worktree so the bytes are commit-pinned
    even if the worktree drifted.
    """
    try:
        proc = _run(
            ["git", "-C", str(core_repo_path), "show", f"{ref}:{path}"],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        # "exists, but not '<path>'" / "does not exist in <ref>" → None
        # (matches the 404 fallthrough contract). Other errors → None
        # too; the caller will see the orchestrator skip the packet and
        # the manifest will reflect missing content.
        sys.stderr.write(
            f"[grade-batch] WARN: git show {ref}:{path} failed: "
            f"{stderr.strip()}\n"
        )
        return None
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"[grade-batch] WARN: git show {ref}:{path} timed out\n"
        )
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _noop_post_pending(**kwargs) -> None:
    """No-op stand-in for GH commit-status pending posts."""
    return None


def _noop_post_final(**kwargs) -> None:
    """No-op stand-in for GH commit-status final posts."""
    return None


def _noop_post_comment(**kwargs) -> None:
    """No-op stand-in for GH PR-comment posts."""
    return None


# ─── Walking the core repo for packet qna logs ───────────────────────


def discover_packet_qna_logs(
    core_repo_path: Path,
    *,
    packet_glob: str = "**/docs/work/*/02-qna-log.md",
) -> list[str]:
    """Return repo-relative paths of every ``02-qna-log.md`` under
    ``core_repo_path`` matching ``packet_glob`` (default = every packet).

    Pathlib's ``Path.glob`` handles the ``**`` recursive matching.
    Returns POSIX-style relpaths sorted for determinism.
    """
    root = core_repo_path.resolve()
    matches = []
    for path in root.glob(packet_glob):
        if path.is_file():
            matches.append(path.relative_to(root).as_posix())
    return sorted(matches)


def _packet_slug_from_qna_path(qna_log_path: str) -> str:
    """Extract the packet slug (the directory name immediately above
    ``02-qna-log.md``) from a relpath like
    ``products/.../docs/work/2026-05-14-foo-v1/02-qna-log.md``.

    Used as the per-packet JSON filename and as the packet identifier
    in the manifest.
    """
    parts = Path(qna_log_path).parts
    # Find "work" segment; slug is one segment after it.
    try:
        idx = parts.index("work")
        return parts[idx + 1]
    except (ValueError, IndexError):
        # Fallback: use parent directory name.
        return Path(qna_log_path).parent.name


# ─── Grading one packet ──────────────────────────────────────────────


def grade_one_packet(
    cfg,
    *,
    repo_full_name: str,
    commit_sha: str,
    qna_log_path: str,
    github_token: str,
    core_repo_path: Path,
    _run_pr_grading: Callable = gs.run_pr_grading,
    _read_file_at_commit: Callable = _read_file_via_git_show,
) -> dict[str, Any]:
    """Run grading for a single packet's qna log.

    All GitHub calls inside ``run_pr_grading`` are stubbed:
      - ``_fetch_pr_files`` → returns the qna log path (synthetic).
      - ``_fetch_file_at_ref`` → reads from local git object DB at
        ``<core_repo_path>:<ref>:<path>``. Codex r2 P1 fix — without
        this stub, the orchestrator would hit GitHub's Contents API
        and silently skip every packet on a 404 / rate limit / missing
        token, making the batch falsely report `status='ok'` with zero
        receipts.
      - ``_post_pending`` / ``_post_final`` / ``_post_comment`` → no-ops.

    Returns the orchestrator's ``outcome`` dict augmented with
    ``packet_slug`` and ``packet_qna_log_path`` so the caller can
    write a self-describing JSON.
    """
    event = _synthesize_pr_event(
        repo_full_name=repo_full_name,
        commit_sha=commit_sha,
        packet_qna_log_path=qna_log_path,
    )

    # Wire the stubbed fetcher so detect_packet_qna_log sees our packet.
    def _fetch_for_this_packet(**kwargs):
        return _stub_fetch_pr_files(**kwargs, qna_log_path=qna_log_path)

    # Wire the local file reader so qna log content comes from the
    # local worktree's git object DB, not the GitHub Contents API.
    def _read_file_local(**kwargs):
        return _read_file_at_commit(
            core_repo_path=core_repo_path,
            path=kwargs["path"],
            ref=kwargs["ref"],
        )

    outcome = _run_pr_grading(
        cfg,
        event,
        github_token=github_token,
        _fetch_pr_files=_fetch_for_this_packet,
        _fetch_file_at_ref=_read_file_local,
        _post_pending=_noop_post_pending,
        _post_final=_noop_post_final,
        _post_comment=_noop_post_comment,
    )

    outcome["packet_slug"] = _packet_slug_from_qna_path(qna_log_path)
    outcome["packet_qna_log_path"] = qna_log_path
    return outcome


# ─── Serializing one packet's outcome to JSON ────────────────────────


def _load_artifact_rows(artifact_path: Optional[str]) -> Optional[dict[str, Any]]:
    """Read the per-packet artifact JSON written by
    ``grader_service.write_grading_artifact``.

    Returns the full payload (including ``rows``) on success, or ``None``
    if the path is missing / unreadable / unparseable. Codex r1 (2026-05-15)
    flagged that the batch needs to consume the actual ``run_pr_grading``
    return shape — the ``summaries`` dicts there are aggregate-only and
    reference an external artifact file for per-receipt detail.
    """
    if not artifact_path:
        return None
    try:
        return json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Non-fatal: per-packet JSON falls back to aggregate-only.
        sys.stderr.write(
            f"[grade-batch] WARN: could not load artifact {artifact_path}: {exc}\n"
        )
        return None


def write_packet_artifact(
    outcome: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write a per-packet JSON under ``output_dir/packets/<slug>.json``.

    Inlines the per-receipt rows from each summary's ``artifact_path``
    (the file ``grader_service.write_grading_artifact`` writes per
    packet) so this single JSON is self-contained — drilling into a
    packet's results doesn't require chasing a separate file.

    Returns the written path. Creates parent dirs as needed.
    """
    packets_dir = output_dir / "packets"
    packets_dir.mkdir(parents=True, exist_ok=True)
    slug = outcome.get("packet_slug", "unknown-packet")
    target = packets_dir / f"{slug}.json"

    # Inline each summary's full artifact (with rows) alongside the
    # aggregate-only dict from run_pr_grading.
    enriched_summaries = []
    for summary_dict in outcome.get("summaries", []):
        if not isinstance(summary_dict, dict):
            # Defensive: if anyone in the future passes a GradingSummary
            # object instead of the dict shape, still serialize sanely.
            try:
                enriched = asdict(summary_dict)
            except TypeError:
                enriched = {"_repr": str(summary_dict)}
        else:
            enriched = dict(summary_dict)
            artifact_payload = _load_artifact_rows(enriched.get("artifact_path"))
            if artifact_payload is not None:
                # Embed the rows + other artifact fields under "artifact"
                # rather than overwriting the aggregate top-level keys.
                enriched["artifact"] = artifact_payload
        enriched_summaries.append(enriched)

    serializable_outcome = {
        **outcome,
        "summaries": enriched_summaries,
    }
    target.write_text(
        json.dumps(serializable_outcome, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return target


# ─── Per-run aggregation: manifest.json + summary.md ─────────────────


def _summary_total(summary) -> int:
    """Read ``total`` from either the production dict shape (from
    ``run_pr_grading``) or the dataclass shape (used in some tests)."""
    if isinstance(summary, dict):
        return int(summary.get("total", 0))
    return getattr(summary, "total", 0)


def _summary_pass_count(summary) -> int:
    """Read ``pass_count`` from either the production dict shape or
    the dataclass shape."""
    if isinstance(summary, dict):
        return int(summary.get("pass_count", 0))
    return getattr(summary, "pass_count", 0)


def _aggregate_run_totals(packet_outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum receipts/correct counts across every packet in this run.

    "Correct" mirrors ``GradingSummary.pass_count`` — full_match or
    partial_match. Reads from the dict shape ``run_pr_grading``
    returns (codex r1 fix).
    """
    total_packets = len(packet_outcomes)
    total_receipts = 0
    total_correct = 0
    per_packet_pct: dict[str, dict[str, Any]] = {}
    for outcome in packet_outcomes:
        slug = outcome.get("packet_slug", "unknown")
        packet_receipts = 0
        packet_correct = 0
        for summary in outcome.get("summaries", []):
            packet_receipts += _summary_total(summary)
            packet_correct += _summary_pass_count(summary)
        total_receipts += packet_receipts
        total_correct += packet_correct
        per_packet_pct[slug] = {
            "receipts": packet_receipts,
            "correct": packet_correct,
            "pct": (round(packet_correct * 100 / packet_receipts, 1)
                    if packet_receipts else 0.0),
            "status": outcome.get("status"),
        }
    overall_pct = (
        round(total_correct * 100 / total_receipts, 1) if total_receipts else 0.0
    )
    return {
        "total_packets": total_packets,
        "total_receipts": total_receipts,
        "total_correct": total_correct,
        "overall_pct": overall_pct,
        "per_packet_pct": per_packet_pct,
    }


def write_manifest(
    run_name: str,
    output_dir: Path,
    *,
    commit_sha: str,
    code_revision_id: Optional[str],
    packet_outcomes: list[dict[str, Any]],
    started_at: str,
    finished_at: str,
    grader_backend: str,
    grader_model: str,
) -> Path:
    """Write ``output_dir/manifest.json`` with this run's summary.

    The cross-run aggregator reads this file (across every
    ``baseline-*/`` folder) to build ``overall-summary.{md,json}``.
    """
    totals = _aggregate_run_totals(packet_outcomes)
    manifest = {
        "run_name": run_name,
        "commit_sha": commit_sha,
        "code_revision_id": code_revision_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "grader_backend": grader_backend,
        "grader_model": grader_model,
        **totals,
    }
    target = output_dir / "manifest.json"
    target.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


def write_per_run_summary_md(
    run_name: str,
    output_dir: Path,
    *,
    commit_sha: str,
    packet_outcomes: list[dict[str, Any]],
    overall_pct: float,
    total_receipts: int,
    total_correct: int,
) -> Path:
    """Write a human-readable per-run summary table to
    ``output_dir/summary.md``. Per-packet rows.
    """
    lines = [
        f"# {run_name}",
        "",
        f"- **Commit SHA:** `{commit_sha}`",
        f"- **Packets graded:** {len(packet_outcomes)}",
        f"- **Total receipts:** {total_receipts}",
        f"- **Correct:** {total_correct} ({overall_pct:.1f}%)",
        "",
        "## Per-packet results",
        "",
        "| Packet | Receipts | Correct | Pct | Status |",
        "|---|---|---|---|---|",
    ]
    # Sort by ascending pct (worst first) — easier to spot problems.
    rows = []
    for outcome in packet_outcomes:
        slug = outcome.get("packet_slug", "unknown")
        receipts = 0
        correct = 0
        for summary in outcome.get("summaries", []):
            receipts += _summary_total(summary)
            correct += _summary_pass_count(summary)
        pct = (round(correct * 100 / receipts, 1) if receipts else 0.0)
        rows.append((pct, slug, receipts, correct, outcome.get("status", "ok")))
    rows.sort(key=lambda r: r[0])
    for pct, slug, receipts, correct, status in rows:
        lines.append(f"| {slug} | {receipts} | {correct} | {pct:.1f}% | {status} |")
    lines.append("")

    target = output_dir / "summary.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


# ─── CLI entry point ─────────────────────────────────────────────────


def cmd_grade_packet_batch(cfg, args) -> int:
    """Main entry point invoked by ``entrypoint.cmd_grade_packet_batch``.

    Returns shell exit code: 0 on full success, 2 on partial failure
    (some packets errored but at least one graded), 1 on fatal setup
    error.
    """
    core_repo_path = Path(args.core_repo_path).resolve()
    if not core_repo_path.is_dir():
        print(
            f"ERROR: --core-repo-path {core_repo_path} is not a directory",
            file=sys.stderr,
        )
        return 1

    # Resolve commit_sha: --commit-sha overrides, else use the daemon's
    # latest_commit_ingested from the state file (avoids accidentally
    # grading against a SHA the shadow corpus doesn't reflect).
    commit_sha = args.commit_sha
    if not commit_sha:
        from .state_file import read_state

        state = read_state(cfg.state_file)
        commit_sha = state.get("latest_commit_ingested") if state else None
        if not commit_sha:
            print(
                "ERROR: --commit-sha not provided and daemon state file has "
                "no latest_commit_ingested. Run `make ingest-replay COMMIT=...` "
                "first, or pass --commit-sha explicitly.",
                file=sys.stderr,
            )
            return 1

    qna_logs = discover_packet_qna_logs(core_repo_path, packet_glob=args.packet_glob)
    if args.limit is not None:
        qna_logs = qna_logs[: args.limit]
    if not qna_logs:
        print(
            f"ERROR: no qna logs matched {args.packet_glob!r} under {core_repo_path}",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "core_repo_path": str(core_repo_path),
            "commit_sha": commit_sha,
            "packet_glob": args.packet_glob,
            "packets_matched": len(qna_logs),
            "packets": qna_logs,
        }, indent=2))
        return 0

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_name = output_dir.name

    # Read GitHub token even though we won't post — run_pr_grading still
    # passes it through to its (stubbed) callbacks, and we want the
    # stub to never accidentally see "" and degrade behavior. Operators
    # must export GITHUB_ATLAS_SHADOW_TOKEN regardless.
    github_token = os.environ.get("GITHUB_ATLAS_SHADOW_TOKEN", "")

    repo_full_name = args.repo_full_name

    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.perf_counter()
    packet_outcomes: list[dict[str, Any]] = []
    fatal_setup = 0
    partial_failures = 0

    for idx, qna_log in enumerate(qna_logs, start=1):
        slug = _packet_slug_from_qna_path(qna_log)
        if not args.quiet:
            print(f"[{idx}/{len(qna_logs)}] grading {slug}...", flush=True)
        try:
            outcome = grade_one_packet(
                cfg,
                repo_full_name=repo_full_name,
                commit_sha=commit_sha,
                qna_log_path=qna_log,
                github_token=github_token,
                core_repo_path=core_repo_path,
            )
        except Exception as exc:  # noqa: BLE001 — one bad packet shouldn't abort
            partial_failures += 1
            outcome = {
                "packet_slug": slug,
                "packet_qna_log_path": qna_log,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "summaries": [],
                "base_sha": commit_sha,
                "head_sha": commit_sha,
                "pr_number": 0,
                "repo_full_name": repo_full_name,
            }
            print(
                f"  WARN: {slug} crashed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        else:
            # Codex r1 P2 fix: run_pr_grading documents that operational
            # failures are RETURNED as status="error" (or other non-"ok"
            # statuses like "revision_not_indexed" or "skipped_not_packet")
            # rather than RAISED. Without this branch, the batch would
            # exit 0 even when packets failed silently.
            if outcome.get("status") not in _OK_STATUSES:
                partial_failures += 1
                if not args.quiet:
                    print(
                        f"  WARN: {slug} status={outcome.get('status')} "
                        f"error={outcome.get('error')}",
                        file=sys.stderr,
                    )

        write_packet_artifact(outcome, output_dir)
        packet_outcomes.append(outcome)

    finished_at = datetime.now(timezone.utc).isoformat()
    elapsed_ms = round((time.perf_counter() - started_perf) * 1000)

    # Get code_revision_id from any packet's outcome (all packets share it
    # since base_sha is the same).
    code_revision_id = None
    for outcome in packet_outcomes:
        if outcome.get("code_revision_id"):
            code_revision_id = outcome["code_revision_id"]
            break

    grader_backend = os.environ.get("ATLAS_SHADOW_GRADER_BACKEND") or (
        "anthropic_sdk" if os.environ.get("ANTHROPIC_API_KEY") else "unset"
    )
    grader_model = getattr(cfg, "grader_model", "sonnet")

    manifest_path = write_manifest(
        run_name,
        output_dir,
        commit_sha=commit_sha,
        code_revision_id=code_revision_id,
        packet_outcomes=packet_outcomes,
        started_at=started_at,
        finished_at=finished_at,
        grader_backend=grader_backend,
        grader_model=grader_model,
    )
    totals = _aggregate_run_totals(packet_outcomes)
    summary_path = write_per_run_summary_md(
        run_name,
        output_dir,
        commit_sha=commit_sha,
        packet_outcomes=packet_outcomes,
        overall_pct=totals["overall_pct"],
        total_receipts=totals["total_receipts"],
        total_correct=totals["total_correct"],
    )

    # Regenerate cross-run dashboard from on-disk manifests.
    shadow_runs_root = output_dir.parent
    overall_md_path, overall_json_path = os_mod.regenerate(shadow_runs_root)

    print(
        f"\nBatch finished in {elapsed_ms}ms.\n"
        f"  manifest:           {manifest_path}\n"
        f"  per-run summary:    {summary_path}\n"
        f"  overall dashboard:  {overall_md_path}\n"
        f"  overall (json):     {overall_json_path}\n"
        f"  packets graded:     {totals['total_packets']}\n"
        f"  total receipts:     {totals['total_receipts']}\n"
        f"  correct:            {totals['total_correct']} ({totals['overall_pct']:.1f}%)\n"
    )

    if fatal_setup:
        return 1
    if partial_failures:
        return 2
    return 0


def build_subparser(subparsers) -> argparse.ArgumentParser:
    """Register the ``grade-packet-batch`` subcommand on the daemon
    entry point's argparse subparsers."""
    p = subparsers.add_parser(
        "grade-packet-batch",
        help="Offline batch grading of every packet's qna log",
        description=(
            "Run the P2 grading pipeline against every packet's "
            "02-qna-log.md in a core repo checkout, without posting "
            "anything to GitHub. Writes per-packet JSON + a per-run "
            "summary + regenerates the cross-run shadow-runs/overall-"
            "summary dashboard."
        ),
    )
    p.add_argument(
        "--core-repo-path",
        required=True,
        help="Path to a core checkout pinned to --commit-sha (the "
             "worktree HEAD is read directly to discover qna logs).",
    )
    p.add_argument(
        "--commit-sha",
        default=None,
        help="Full 40-char SHA the grading is anchored at. If omitted, "
             "uses the daemon's latest_commit_ingested from the state file.",
    )
    p.add_argument(
        "--output-dir",
        required=True,
        help="Where to write this batch's artifacts (e.g. "
             "shadow-runs/baseline-2026-05-15).",
    )
    p.add_argument(
        "--packet-glob",
        default="**/docs/work/*/02-qna-log.md",
        help="Glob pattern (relative to --core-repo-path) for qna logs. "
             "Default matches every packet.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max packets to grade (debug). Default: all matched.",
    )
    p.add_argument(
        "--repo-full-name",
        default="tandemstream/core",
        help="repo_full_name to record in the synthetic PrEvent. "
             "Cosmetic — the batch never posts to GitHub.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List packets that would be graded without running anything.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-packet progress lines.",
    )
    return p
