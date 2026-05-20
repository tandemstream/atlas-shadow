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
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import atlas_query_cache as atlas_query_cache_mod
from . import grader_service as gs
from . import legacy_receipt_corrections as legacy_corrections_mod
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
    commit_sha: Optional[str] = None,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    """Return repo-relative paths of every ``02-qna-log.md`` matching
    ``packet_glob`` (default = every packet).

    When ``commit_sha`` is provided (the production path), enumerates
    files via ``git ls-tree -r --name-only <commit_sha>`` so the
    discovery is pinned to the actual commit being graded. Codex r3
    P2 fix: prior pass walked the WORKTREE, which can have files the
    commit doesn't (just-added) or miss files the commit has (just-
    deleted). The divergence caused `run_pr_grading` to silently
    skip with status="ok" + zero receipts.

    When ``commit_sha`` is omitted, falls back to ``Path.glob`` against
    the worktree (useful for dry-run + tests).

    Returns POSIX-style relpaths sorted for determinism.
    """
    if commit_sha:
        return _discover_via_ls_tree(
            core_repo_path, packet_glob, commit_sha, _run=_run
        )

    # Worktree fallback (dry-run + tests).
    root = core_repo_path.resolve()
    matches = []
    for path in root.glob(packet_glob):
        if path.is_file():
            matches.append(path.relative_to(root).as_posix())
    return sorted(matches)


def _discover_via_ls_tree(
    core_repo_path: Path,
    packet_glob: str,
    commit_sha: str,
    *,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    """Enumerate files at ``commit_sha`` via ``git ls-tree -r``, then
    filter by ``packet_glob``. Uses ``Path.match`` for glob matching,
    which (Python 3.13+) supports ``**`` recursive globs. For 3.12
    compatibility we translate the glob to a regex (same trick we use
    in core-side ``shadow_ingest_docs.py``).
    """
    try:
        proc = _run(
            ["git", "-C", str(core_repo_path), "ls-tree", "-r",
             "--name-only", commit_sha],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"ERROR: git ls-tree failed for {commit_sha} in {core_repo_path}: "
            f"{exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"ERROR: git ls-tree timed out for {commit_sha}: {exc}"
        ) from exc

    import re
    rx = _glob_to_regex(packet_glob)
    matches = [
        line for line in proc.stdout.splitlines()
        if line and rx.match(line)
    ]
    return sorted(matches)


def _glob_to_regex(pat: str):
    """Translate a glob (with ``**`` recursion) to a compiled regex.

    Mirrors the helper in core-side ``shadow_ingest_docs.py`` —
    ``fnmatch`` treats ``**`` as single-segment so wouldn't match
    e.g. ``docs/work/<packet>/02-qna-log.md`` at any depth.
    """
    import re
    out: list[str] = []
    i = 0
    n = len(pat)
    while i < n:
        if pat[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pat[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


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


def resolve_packet_commit_sha(
    core_repo_path: Path,
    *,
    run_commit_sha: str,
    qna_log_path: str,
    mode: str,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Resolve the historical SHA used for one packet's synthetic PR.

    ``run_commit_sha`` remains the discovery tree for the batch. The
    packet's synthetic base/head can be older, which lets shadow grading
    separate stale-current-HEAD methodology failures from retrieval
    failures.
    """
    if mode == "run-commit":
        return run_commit_sha
    if mode not in {"created", "latest-change"}:
        raise ValueError(f"unknown packet SHA mode: {mode}")

    cmd = ["git", "-C", str(core_repo_path), "log", "--format=%H"]
    if mode == "created":
        cmd.append("--diff-filter=A")
    else:
        cmd.extend(["-n", "1"])
    cmd.extend([run_commit_sha, "--", qna_log_path])

    try:
        proc = _run(cmd, capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        sys.stderr.write(
            f"[grade-batch] WARN: could not resolve packet SHA for "
            f"{qna_log_path} using mode={mode}: {stderr}; falling back "
            f"to run commit {run_commit_sha}\n"
        )
        return run_commit_sha
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"[grade-batch] WARN: packet SHA resolution timed out for "
            f"{qna_log_path} using mode={mode}; falling back to run commit "
            f"{run_commit_sha}\n"
        )
        return run_commit_sha

    shas = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not shas:
        sys.stderr.write(
            f"[grade-batch] WARN: no packet SHA found for {qna_log_path} "
            f"using mode={mode}; falling back to run commit {run_commit_sha}\n"
        )
        return run_commit_sha
    if mode == "created":
        return shas[-1]
    return shas[0]


# ─── Grading one packet ──────────────────────────────────────────────


def grade_one_packet(
    cfg,
    *,
    repo_full_name: str,
    commit_sha: str,
    qna_log_path: str,
    github_token: str,
    core_repo_path: Path,
    atlas_query_cache: Any = None,
    max_workers: Optional[int] = None,
    revision_pin_mode: str = "receipt-source",
    legacy_receipt_corrections: Any = None,
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
        atlas_query_cache=atlas_query_cache,
        max_workers=max_workers,
        revision_pin_mode=revision_pin_mode,
        legacy_receipt_corrections=legacy_receipt_corrections,
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


def _summary_excluded_count(summary) -> int:
    """Read ``excluded_count`` (PR #14) with backward-compat zero.

    Pre-PR-14 baselines + tests-using-dataclasses don't carry this
    field — treat them as zero-excluded so legacy aggregates keep
    working without rewriting old artifacts.
    """
    if isinstance(summary, dict):
        return int(summary.get("excluded_count", 0) or 0)
    return getattr(summary, "excluded_count", 0) or 0


def _summary_skipped_receipt_stale_count(summary) -> int:
    """Read ``skipped_receipt_stale_count`` (PR #14) with backward-compat zero."""
    if isinstance(summary, dict):
        return int(summary.get("skipped_receipt_stale_count", 0) or 0)
    return getattr(summary, "skipped_receipt_stale_count", 0) or 0


def _summary_skipped_run_commit_line_drift_count(summary) -> int:
    """Read ``skipped_run_commit_line_drift_count`` (PR #15) with
    backward-compat zero. Pre-PR-15 summaries lack this field and
    legitimately contribute zero drift-skips."""
    if isinstance(summary, dict):
        return int(summary.get("skipped_run_commit_line_drift_count", 0) or 0)
    return getattr(summary, "skipped_run_commit_line_drift_count", 0) or 0


# ─── PR #17: four new non-retrieval skip counters ─────────────────────


def _summary_skipped_non_repo_evidence_count(summary) -> int:
    if isinstance(summary, dict):
        return int(summary.get("skipped_non_repo_evidence_count", 0) or 0)
    return getattr(summary, "skipped_non_repo_evidence_count", 0) or 0


def _summary_skipped_absence_search_count(summary) -> int:
    if isinstance(summary, dict):
        return int(summary.get("skipped_absence_search_count", 0) or 0)
    return getattr(summary, "skipped_absence_search_count", 0) or 0


def _summary_skipped_unavailable_source_ref_count(summary) -> int:
    if isinstance(summary, dict):
        return int(summary.get("skipped_unavailable_source_ref_count", 0) or 0)
    return getattr(summary, "skipped_unavailable_source_ref_count", 0) or 0


def _summary_skipped_doc_corpus_excluded_count(summary) -> int:
    if isinstance(summary, dict):
        return int(summary.get("skipped_doc_corpus_excluded_count", 0) or 0)
    return getattr(summary, "skipped_doc_corpus_excluded_count", 0) or 0


def _summary_skipped_command_snapshot_count(summary) -> int:
    """PR #20: command-lane skip count, back-compat zero for legacy."""
    if isinstance(summary, dict):
        return int(summary.get("skipped_command_snapshot_count", 0) or 0)
    return getattr(summary, "skipped_command_snapshot_count", 0) or 0


# ─── PR atlas-shadow-query-cache-v1: cache observability counts ──────


def _summary_atlas_cache_hit_count(summary) -> int:
    """Cache-hit count, back-compat zero for legacy summaries."""
    if isinstance(summary, dict):
        return int(summary.get("atlas_cache_hit_count", 0) or 0)
    return getattr(summary, "atlas_cache_hit_count", 0) or 0


def _summary_atlas_cache_miss_count(summary) -> int:
    """Cache-miss count, back-compat zero for legacy summaries."""
    if isinstance(summary, dict):
        return int(summary.get("atlas_cache_miss_count", 0) or 0)
    return getattr(summary, "atlas_cache_miss_count", 0) or 0


def _summary_atlas_cache_disabled_count(summary) -> int:
    """Cache-disabled count (subprocess fired without checking cache).
    Back-compat zero."""
    if isinstance(summary, dict):
        return int(summary.get("atlas_cache_disabled_count", 0) or 0)
    return getattr(summary, "atlas_cache_disabled_count", 0) or 0


def _summary_skipped_revision_not_indexed_count(summary) -> int:
    """Receipt-SHA pinning: historical SHA missing from ingest ledger."""
    if isinstance(summary, dict):
        return int(summary.get("skipped_revision_not_indexed_count", 0) or 0)
    return getattr(summary, "skipped_revision_not_indexed_count", 0) or 0


def _summary_skipped_legacy_receipt_defect_count(summary) -> int:
    """Explicit frozen-corpus correction sidecar skips."""
    if isinstance(summary, dict):
        return int(summary.get("skipped_legacy_receipt_defect_count", 0) or 0)
    return getattr(summary, "skipped_legacy_receipt_defect_count", 0) or 0


# ─── Per-evidence-type rollup (PR-evidence-breakdown) ────────────────


# Canonical bucket order — matches GradingSummary.by_evidence_type and
# grader_service's routing constants. Defined once here so the
# aggregator, manifest writer, and dashboard renderer agree on
# column ordering across runs.
EVIDENCE_TYPE_BUCKETS: tuple[str, ...] = (
    "source_excerpt",
    "external_tool_docs",
    "user_context",
    "absence_search",
    "other",
)


def _empty_evidence_bucket() -> dict[str, Any]:
    """Zero-filled bucket. Always returns all 5 fields so consumers can
    rely on the shape regardless of whether any rows landed here."""
    return {
        "receipts": 0,
        "correct": 0,
        "excluded": 0,
        "clean_total": 0,
        "clean_pct": None,
    }


def _empty_evidence_breakdown() -> dict[str, dict[str, Any]]:
    """Zero-filled breakdown across all 5 canonical buckets."""
    return {b: _empty_evidence_bucket() for b in EVIDENCE_TYPE_BUCKETS}


def _summary_by_evidence_type(summary) -> dict[str, dict[str, Any]]:
    """Read ``by_evidence_type`` with back-compat zeros.

    Pre-evidence-breakdown summaries (and test fixtures using bare
    dataclasses without rows that have evidence_type set) won't
    carry this field — return a zero-filled breakdown so the
    aggregator's sum-of-zeros stays well-defined.
    """
    if isinstance(summary, dict):
        raw = summary.get("by_evidence_type") or {}
    else:
        raw = getattr(summary, "by_evidence_type", None) or {}
    if not isinstance(raw, dict):
        return _empty_evidence_breakdown()
    # Normalize: fill in missing buckets so downstream code can index
    # without KeyError, even if a packet only emitted receipts in one
    # bucket.
    out = _empty_evidence_breakdown()
    for bucket, vals in raw.items():
        if not isinstance(vals, dict):
            continue
        key = bucket if bucket in EVIDENCE_TYPE_BUCKETS else "other"
        out[key]["receipts"] += int(vals.get("receipts", 0) or 0)
        out[key]["correct"] += int(vals.get("correct", 0) or 0)
        out[key]["excluded"] += int(vals.get("excluded", 0) or 0)
    # Recompute clean_total / clean_pct from the summed receipts/excluded;
    # don't trust the per-packet value here — when summing across packets
    # we need the run-level denominator, not a sum-of-percentages.
    for vals in out.values():
        clean_total = vals["receipts"] - vals["excluded"]
        vals["clean_total"] = clean_total
        vals["clean_pct"] = (
            round(vals["correct"] * 100 / clean_total, 1)
            if clean_total > 0 else None
        )
    return out


def _accumulate_evidence_breakdown(
    target: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
) -> None:
    """Add ``incoming``'s receipts/correct/excluded into ``target`` in
    place. Does NOT recompute clean_total / clean_pct — those are
    derived by :func:`_finalize_evidence_breakdown` after all packets
    have been accumulated, so percentages reflect the run-level (not
    sum-of-per-packet) denominator.
    """
    for bucket in EVIDENCE_TYPE_BUCKETS:
        src = incoming.get(bucket, {})
        tgt = target[bucket]
        tgt["receipts"] += int(src.get("receipts", 0) or 0)
        tgt["correct"] += int(src.get("correct", 0) or 0)
        tgt["excluded"] += int(src.get("excluded", 0) or 0)


def _finalize_evidence_breakdown(
    breakdown: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compute clean_total + clean_pct for each bucket from
    receipts/excluded. Returns the same dict (mutates in place)
    for chaining."""
    for vals in breakdown.values():
        clean_total = vals["receipts"] - vals["excluded"]
        vals["clean_total"] = clean_total
        vals["clean_pct"] = (
            round(vals["correct"] * 100 / clean_total, 1)
            if clean_total > 0 else None
        )
    return breakdown


# ─── Per-lane rollup (sibling of evidence-type rollup) ────────────────


# Canonical bucket order — matches GradingSummary.by_lane and the
# values :mod:`grader_service._infer_lane` emits today. Defined once
# here so the aggregator, manifest writer, and dashboard renderer
# agree on column ordering across runs. ``other`` is always last —
# unknown lane values land there without inventing a classification.
LANE_BUCKETS: tuple[str, ...] = (
    "explicit_source_fast_path",
    "fuzzy_find_code",
    "scan_search",
    "doc_resolver",
    "non_retrieval",
    "other",
)


def _empty_lane_bucket() -> dict[str, Any]:
    """Zero-filled bucket. Always returns all 5 fields so consumers can
    rely on the shape regardless of whether any rows landed here."""
    return {
        "receipts": 0,
        "correct": 0,
        "excluded": 0,
        "clean_total": 0,
        "clean_pct": None,
    }


def _empty_lane_breakdown() -> dict[str, dict[str, Any]]:
    """Zero-filled breakdown across all 6 canonical lane buckets."""
    return {b: _empty_lane_bucket() for b in LANE_BUCKETS}


def _summary_by_lane(summary) -> dict[str, dict[str, Any]]:
    """Read ``by_lane`` with back-compat zeros.

    Pre-by-lane summaries (and test fixtures using bare dataclasses
    without rows that have lane set) won't carry this field — return
    a zero-filled breakdown so the aggregator's sum-of-zeros stays
    well-defined. Mirror of :func:`_summary_by_evidence_type`.
    """
    if isinstance(summary, dict):
        raw = summary.get("by_lane") or {}
    else:
        raw = getattr(summary, "by_lane", None) or {}
    if not isinstance(raw, dict):
        return _empty_lane_breakdown()
    out = _empty_lane_breakdown()
    for bucket, vals in raw.items():
        if not isinstance(vals, dict):
            continue
        key = bucket if bucket in LANE_BUCKETS else "other"
        out[key]["receipts"] += int(vals.get("receipts", 0) or 0)
        out[key]["correct"] += int(vals.get("correct", 0) or 0)
        out[key]["excluded"] += int(vals.get("excluded", 0) or 0)
    for vals in out.values():
        clean_total = vals["receipts"] - vals["excluded"]
        vals["clean_total"] = clean_total
        vals["clean_pct"] = (
            round(vals["correct"] * 100 / clean_total, 1)
            if clean_total > 0 else None
        )
    return out


def _accumulate_lane_breakdown(
    target: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
) -> None:
    """Add ``incoming``'s receipts/correct/excluded into ``target`` in
    place. Mirror of :func:`_accumulate_evidence_breakdown`.

    Does NOT recompute clean_total / clean_pct — those are derived
    by :func:`_finalize_lane_breakdown` after all packets have been
    accumulated, so percentages reflect the run-level (not
    sum-of-per-packet) denominator.
    """
    for bucket in LANE_BUCKETS:
        src = incoming.get(bucket, {})
        tgt = target[bucket]
        tgt["receipts"] += int(src.get("receipts", 0) or 0)
        tgt["correct"] += int(src.get("correct", 0) or 0)
        tgt["excluded"] += int(src.get("excluded", 0) or 0)


def _finalize_lane_breakdown(
    breakdown: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compute clean_total + clean_pct for each lane bucket from
    receipts/excluded. Mirror of :func:`_finalize_evidence_breakdown`.
    """
    for vals in breakdown.values():
        clean_total = vals["receipts"] - vals["excluded"]
        vals["clean_total"] = clean_total
        vals["clean_pct"] = (
            round(vals["correct"] * 100 / clean_total, 1)
            if clean_total > 0 else None
        )
    return breakdown


def _aggregate_run_totals(packet_outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum receipts/correct counts across every packet in this run.

    "Correct" mirrors ``GradingSummary.pass_count`` — full_match or
    partial_match. Reads from the dict shape ``run_pr_grading``
    returns (codex r1 fix).

    PR #14 added the clean-denominator score: rows whose
    ``score_status != "counted"`` (today: receipt-stale skips) are
    removed from BOTH numerator and denominator so the operator score
    reflects retrieval performance, not receipt drift. The raw
    ``overall_pct`` is preserved for trend continuity with pre-PR-14
    baselines. Both scores are emitted in the manifest; consumers can
    choose which to chart.
    """
    total_packets = len(packet_outcomes)
    total_receipts = 0
    total_correct = 0
    total_excluded = 0
    total_skipped_receipt_stale = 0
    total_skipped_run_commit_line_drift = 0
    # PR #17: four new non-retrieval skip totals.
    total_skipped_non_repo_evidence = 0
    total_skipped_absence_search = 0
    total_skipped_unavailable_source_ref = 0
    total_skipped_doc_corpus_excluded = 0
    # PR #20: command-snapshot lane total.
    total_skipped_command_snapshot = 0
    total_skipped_revision_not_indexed = 0
    total_skipped_legacy_receipt_defect = 0
    # PR atlas-shadow-query-cache-v1: cache observability totals.
    # Together they answer "did this run get faster because Atlas
    # improved, or because the cache hid the work?"
    total_atlas_cache_hits = 0
    total_atlas_cache_misses = 0
    total_atlas_cache_disabled = 0
    # Per-evidence-type rollup (run level). Per-packet breakdowns are
    # stored under per_packet_pct[slug]["by_evidence_type"] below.
    total_by_evidence_type = _empty_evidence_breakdown()
    # Per-lane rollup — symmetric with by_evidence_type. Per-packet
    # breakdowns are stored under per_packet_pct[slug]["by_lane"].
    total_by_lane = _empty_lane_breakdown()
    per_packet_pct: dict[str, dict[str, Any]] = {}
    for outcome in packet_outcomes:
        slug = outcome.get("packet_slug", "unknown")
        packet_receipts = 0
        packet_correct = 0
        packet_excluded = 0
        packet_stale = 0
        packet_run_drift = 0
        packet_non_repo = 0
        packet_absence = 0
        packet_unavailable = 0
        packet_corpus_excluded = 0
        packet_command_snapshot = 0
        packet_revision_not_indexed = 0
        packet_legacy_receipt_defect = 0
        packet_atlas_cache_hits = 0
        packet_atlas_cache_misses = 0
        packet_atlas_cache_disabled = 0
        packet_by_evidence_type = _empty_evidence_breakdown()
        packet_by_lane = _empty_lane_breakdown()
        for summary in outcome.get("summaries", []):
            packet_receipts += _summary_total(summary)
            packet_correct += _summary_pass_count(summary)
            packet_excluded += _summary_excluded_count(summary)
            packet_stale += _summary_skipped_receipt_stale_count(summary)
            packet_run_drift += _summary_skipped_run_commit_line_drift_count(summary)
            packet_non_repo += _summary_skipped_non_repo_evidence_count(summary)
            packet_absence += _summary_skipped_absence_search_count(summary)
            packet_unavailable += _summary_skipped_unavailable_source_ref_count(summary)
            packet_corpus_excluded += _summary_skipped_doc_corpus_excluded_count(summary)
            packet_command_snapshot += _summary_skipped_command_snapshot_count(summary)
            packet_revision_not_indexed += _summary_skipped_revision_not_indexed_count(summary)
            packet_legacy_receipt_defect += _summary_skipped_legacy_receipt_defect_count(summary)
            packet_atlas_cache_hits += _summary_atlas_cache_hit_count(summary)
            packet_atlas_cache_misses += _summary_atlas_cache_miss_count(summary)
            packet_atlas_cache_disabled += _summary_atlas_cache_disabled_count(summary)
            summary_breakdown = _summary_by_evidence_type(summary)
            _accumulate_evidence_breakdown(packet_by_evidence_type, summary_breakdown)
            _accumulate_evidence_breakdown(total_by_evidence_type, summary_breakdown)
            summary_lane_breakdown = _summary_by_lane(summary)
            _accumulate_lane_breakdown(packet_by_lane, summary_lane_breakdown)
            _accumulate_lane_breakdown(total_by_lane, summary_lane_breakdown)
        total_receipts += packet_receipts
        total_correct += packet_correct
        total_excluded += packet_excluded
        total_skipped_receipt_stale += packet_stale
        total_skipped_run_commit_line_drift += packet_run_drift
        total_skipped_non_repo_evidence += packet_non_repo
        total_skipped_absence_search += packet_absence
        total_skipped_unavailable_source_ref += packet_unavailable
        total_skipped_doc_corpus_excluded += packet_corpus_excluded
        total_skipped_command_snapshot += packet_command_snapshot
        total_skipped_revision_not_indexed += packet_revision_not_indexed
        total_skipped_legacy_receipt_defect += packet_legacy_receipt_defect
        total_atlas_cache_hits += packet_atlas_cache_hits
        total_atlas_cache_misses += packet_atlas_cache_misses
        total_atlas_cache_disabled += packet_atlas_cache_disabled
        packet_clean_total = packet_receipts - packet_excluded
        packet_clean_pct = (
            round(packet_correct * 100 / packet_clean_total, 1)
            if packet_clean_total > 0 else None
        )
        per_packet_pct[slug] = {
            "receipts": packet_receipts,
            "correct": packet_correct,
            "pct": (round(packet_correct * 100 / packet_receipts, 1)
                    if packet_receipts else 0.0),
            "clean_pct": packet_clean_pct,
            "clean_total": packet_clean_total,
            "excluded": packet_excluded,
            "skipped_receipt_stale": packet_stale,
            # PR #15: per-packet run-commit drift count.
            "skipped_run_commit_line_drift": packet_run_drift,
            # PR #17: per-packet non-retrieval skip breakouts.
            "skipped_non_repo_evidence": packet_non_repo,
            "skipped_absence_search": packet_absence,
            "skipped_unavailable_source_ref": packet_unavailable,
            "skipped_doc_corpus_excluded": packet_corpus_excluded,
            # PR #20: command_snapshot lane per-packet count.
            "skipped_command_snapshot": packet_command_snapshot,
            "skipped_revision_not_indexed": packet_revision_not_indexed,
            "skipped_legacy_receipt_defect": packet_legacy_receipt_defect,
            # PR atlas-shadow-query-cache-v1: cache observability
            # per packet.
            "atlas_cache_hits": packet_atlas_cache_hits,
            "atlas_cache_misses": packet_atlas_cache_misses,
            "atlas_cache_disabled": packet_atlas_cache_disabled,
            # Per-evidence-type breakdown for this packet — same
            # bucket shape as total_by_evidence_type, scoped to this
            # packet's rows only.
            "by_evidence_type": _finalize_evidence_breakdown(
                packet_by_evidence_type
            ),
            # Per-lane breakdown for this packet — symmetric with
            # by_evidence_type, scoped to this packet's rows only.
            "by_lane": _finalize_lane_breakdown(packet_by_lane),
            "status": outcome.get("status"),
        }
    overall_pct = (
        round(total_correct * 100 / total_receipts, 1) if total_receipts else 0.0
    )
    clean_total = total_receipts - total_excluded
    clean_pass_pct = (
        round(total_correct * 100 / clean_total, 1) if clean_total > 0 else None
    )
    return {
        "total_packets": total_packets,
        "total_receipts": total_receipts,
        "total_correct": total_correct,
        # Raw score — kept for legacy comparison with pre-PR-14 runs.
        "overall_pct": overall_pct,
        # PR #14/#15/#17: clean-denominator score now excludes all
        # six categories of non-measurement: receipt-stale, run-commit
        # drift, non-repo evidence, absence search, unavailable source
        # ref, doc-corpus-excluded.
        "clean_overall_pct": clean_pass_pct,
        "clean_total": clean_total,
        "total_excluded": total_excluded,
        "total_skipped_receipt_stale": total_skipped_receipt_stale,
        # PR #15: distinct totals for run-commit drift so operators can
        # chart it independently of receipt-staleness.
        "total_skipped_run_commit_line_drift": total_skipped_run_commit_line_drift,
        # PR #17: four new non-retrieval skip totals. Each has a
        # distinct upstream fix (corpus completeness, grader routing,
        # source-ref repair, exclusion-policy review) so they're
        # surfaced separately rather than lumped.
        "total_skipped_non_repo_evidence": total_skipped_non_repo_evidence,
        "total_skipped_absence_search": total_skipped_absence_search,
        "total_skipped_unavailable_source_ref": total_skipped_unavailable_source_ref,
        "total_skipped_doc_corpus_excluded": total_skipped_doc_corpus_excluded,
        # PR #20: command-snapshot lane total.
        "total_skipped_command_snapshot": total_skipped_command_snapshot,
        "total_skipped_revision_not_indexed": total_skipped_revision_not_indexed,
        "total_skipped_legacy_receipt_defect": total_skipped_legacy_receipt_defect,
        # PR atlas-shadow-query-cache-v1: per-run cache observability.
        # The three counters together answer "did this run get faster
        # because Atlas improved, or because the cache hid the work?"
        # Sum to (total_receipts - receipts_that_never_called_atlas).
        "total_atlas_cache_hits": total_atlas_cache_hits,
        "total_atlas_cache_misses": total_atlas_cache_misses,
        "total_atlas_cache_disabled": total_atlas_cache_disabled,
        # Per-evidence-type breakdown across the whole run. Same bucket
        # shape as the per-packet by_evidence_type. Surfaces "where is
        # Atlas actually being measured?" at the run level so the
        # dashboard can chart non-retrieval volume separately from the
        # source_excerpt denominator that PR #14 onwards has been
        # cleaning.
        "total_by_evidence_type": _finalize_evidence_breakdown(
            total_by_evidence_type
        ),
        # Per-lane breakdown across the whole run. Symmetric with
        # total_by_evidence_type. Surfaces which retrieval surface
        # (doc_resolver / explicit_source_fast_path / etc.) the
        # clean denominator is dominated by — so a doc-resolver fix
        # can be attributed directly when its lane's clean_pct moves.
        "total_by_lane": _finalize_lane_breakdown(total_by_lane),
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
    packet_sha_mode: str = "run-commit",
    revision_pin_mode: str = "receipt-source",
) -> Path:
    """Write ``output_dir/manifest.json`` with this run's summary.

    The cross-run aggregator reads this file (across every
    ``baseline-*/`` folder) to build ``overall-summary.{md,json}``.
    """
    totals = _aggregate_run_totals(packet_outcomes)
    manifest = {
        "run_name": run_name,
        "commit_sha": commit_sha,
        "run_commit_sha": commit_sha,
        "packet_sha_mode": packet_sha_mode,
        "revision_pin_mode": revision_pin_mode,
        "packet_base_shas": {
            outcome.get("packet_slug", "unknown"): outcome.get("base_sha")
            for outcome in packet_outcomes
        },
        "packet_code_revision_ids": {
            outcome.get("packet_slug", "unknown"): outcome.get("code_revision_id")
            for outcome in packet_outcomes
            if outcome.get("code_revision_id")
        },
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
    packet_sha_mode: str = "run-commit",
    revision_pin_mode: str = "receipt-source",
    packet_outcomes: list[dict[str, Any]],
    overall_pct: float,
    total_receipts: int,
    total_correct: int,
    clean_overall_pct: Optional[float] = None,
    clean_total: Optional[int] = None,
    total_excluded: int = 0,
    total_skipped_receipt_stale: int = 0,
    total_skipped_run_commit_line_drift: int = 0,
    # PR #17: four new non-retrieval skip totals.
    total_skipped_non_repo_evidence: int = 0,
    total_skipped_absence_search: int = 0,
    total_skipped_unavailable_source_ref: int = 0,
    total_skipped_doc_corpus_excluded: int = 0,
    # PR #20: command-snapshot lane skip total.
    total_skipped_command_snapshot: int = 0,
    total_skipped_revision_not_indexed: int = 0,
    total_skipped_legacy_receipt_defect: int = 0,
) -> Path:
    """Write a human-readable per-run summary table to
    ``output_dir/summary.md``. Per-packet rows.

    PR #14: surfaces both ``overall_pct`` (raw — denominator includes
    excluded rows) and ``clean_overall_pct`` (denominator excludes
    receipt-stale skips and other non-counted bookkeeping). Per-packet
    rows show both columns so operators can see where staleness is
    moving the score.

    PR #15: ``total_skipped_run_commit_line_drift`` (new) breaks out
    the run-commit drift component of the exclusion total alongside
    the receipt-stale component.
    """
    clean_line = ""
    if clean_overall_pct is not None and clean_total is not None:
        # PR #17: surface all six skip components when non-zero so
        # operators can spot which category is driving exclusions.
        # Drop zero-count components from the line to keep it readable.
        components = [
            (total_skipped_receipt_stale, "receipt-stale"),
            (total_skipped_run_commit_line_drift, "run-commit-line-drift"),
            (total_skipped_unavailable_source_ref, "unavailable-source-ref"),
            (total_skipped_doc_corpus_excluded, "doc-corpus-excluded"),
            (total_skipped_non_repo_evidence, "non-repo-evidence"),
            (total_skipped_absence_search, "absence-search"),
            # PR #20: command-snapshot lane.
            (total_skipped_command_snapshot, "command-snapshot"),
            (total_skipped_revision_not_indexed, "revision-not-indexed"),
            (total_skipped_legacy_receipt_defect, "legacy-receipt-defect"),
        ]
        non_zero = [f"{n} {label}" for n, label in components if n > 0]
        breakdown = ", ".join(non_zero) if non_zero else "no breakdown"
        clean_line = (
            f"- **Clean correct:** {total_correct} of {clean_total} "
            f"({clean_overall_pct:.1f}%) "
            f"_(excludes {total_excluded} row(s): {breakdown})_"
        )
    else:
        clean_line = "- **Clean score:** _n/a (no rows counted)_"
    lines = [
        f"# {run_name}",
        "",
        f"- **Commit SHA:** `{commit_sha}`",
        f"- **Packet SHA mode:** `{packet_sha_mode}`",
        f"- **Revision pin mode:** `{revision_pin_mode}`",
        f"- **Packets graded:** {len(packet_outcomes)}",
        f"- **Total receipts:** {total_receipts}",
        f"- **Raw correct:** {total_correct} ({overall_pct:.1f}%)",
        clean_line,
        "",
        "## Per-packet results",
        "",
        "| Packet | Receipts | Correct | Raw % | Clean % | Excluded | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    # Sort by ascending raw pct (worst first) — easier to spot problems.
    rows = []
    for outcome in packet_outcomes:
        slug = outcome.get("packet_slug", "unknown")
        receipts = 0
        correct = 0
        excluded = 0
        for summary in outcome.get("summaries", []):
            receipts += _summary_total(summary)
            correct += _summary_pass_count(summary)
            excluded += _summary_excluded_count(summary)
        raw_pct = (round(correct * 100 / receipts, 1) if receipts else 0.0)
        clean_denom = receipts - excluded
        clean_pct = (round(correct * 100 / clean_denom, 1)
                     if clean_denom > 0 else None)
        rows.append((raw_pct, slug, receipts, correct, clean_pct,
                     excluded, outcome.get("status", "ok")))
    rows.sort(key=lambda r: r[0])
    for raw_pct, slug, receipts, correct, clean_pct, excluded, status in rows:
        clean_str = f"{clean_pct:.1f}%" if clean_pct is not None else "n/a"
        lines.append(
            f"| {slug} | {receipts} | {correct} | "
            f"{raw_pct:.1f}% | {clean_str} | {excluded} | {status} |"
        )
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

    # Discover from the COMMIT tree (not the worktree). Codex r3 fix:
    # worktree drift would have caused run_pr_grading to silently skip
    # packets whose qna log doesn't exist at --commit-sha but does exist
    # in the worktree (or vice versa). git ls-tree pins discovery to the
    # exact SHA we're grading at.
    qna_logs = discover_packet_qna_logs(
        core_repo_path,
        packet_glob=args.packet_glob,
        commit_sha=commit_sha,
    )
    if args.limit is not None:
        qna_logs = qna_logs[: args.limit]
    if not qna_logs:
        print(
            f"ERROR: no qna logs matched {args.packet_glob!r} at {commit_sha} "
            f"in {core_repo_path}",
            file=sys.stderr,
        )
        return 1

    packet_sha_mode = getattr(args, "packet_sha_mode", "run-commit")
    revision_pin_mode = getattr(args, "revision_pin_mode", "receipt-source")
    packet_commit_shas = {
        qna_log: resolve_packet_commit_sha(
            core_repo_path,
            run_commit_sha=commit_sha,
            qna_log_path=qna_log,
            mode=packet_sha_mode,
        )
        for qna_log in qna_logs
    }

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "core_repo_path": str(core_repo_path),
            "commit_sha": commit_sha,
            "run_commit_sha": commit_sha,
            "packet_sha_mode": packet_sha_mode,
            "revision_pin_mode": revision_pin_mode,
            "packet_glob": args.packet_glob,
            "packets_matched": len(qna_logs),
            "packets": qna_logs,
            "packet_base_shas": packet_commit_shas,
        }, indent=2))
        return 0

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_name = output_dir.name

    # Codex r5 + r6 P2 fixes: build a batch-specific cfg.
    #
    # r5: when --core-repo-path or --output-dir differs from
    # shadow-config.yaml, the orchestrator would otherwise use
    # cfg.core_repo_path (atlas runner / doc fallback) and
    # cfg.shadow_runs_dir (per-packet artifact writes) from the YAML
    # — meaning we'd discover qna logs from /A but grade against /B
    # and write artifacts to /B. Override both.
    #
    # r6: the orchestrator's acquire_pin/release_pin lifecycle for
    # synthetic PR=0 writes to cfg.state_file (.daemon-state.json by
    # default). If grade-packet-batch runs while the live ingest daemon
    # is also writing that file (after every successful webhook
    # ingest), the two processes race: read-modify-write isn't
    # cross-process locked, and a batch packet could rewrite the
    # state with a stale latest_commit_ingested. Override
    # cfg.state_file to a per-batch path inside output_dir so batch
    # writes are isolated from the daemon's state.
    #
    # Raw orchestrator artifacts go in a subdir of output_dir so they
    # don't clutter the operator-facing files.
    batch_cfg = replace(
        cfg,
        core_repo_path=core_repo_path,
        shadow_runs_dir=output_dir / "artifacts",
        state_file=output_dir / ".batch-state.json",
    )

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

    # PR atlas-shadow-query-cache-v1: build the cache once for the
    # whole batch. ``build_query_cache_if_enabled`` returns ``None``
    # when ``ATLAS_SHADOW_QUERY_CACHE=off`` (or cfg disables it), in
    # which case every receipt's row carries ``atlas_cache_status="disabled"``.
    # The cache is BATCH-only: the live webhook PR-grading path
    # constructs no cache, so production gates never observe a
    # cached result.
    atlas_query_cache = atlas_query_cache_mod.build_query_cache_if_enabled(
        batch_cfg
    )
    if atlas_query_cache is not None and not args.quiet:
        print(
            f"[batch] atlas-query cache enabled at "
            f"{atlas_query_cache.db_path}",
            flush=True,
        )

    # Frozen-corpus correction sidecar. This is deliberately batch-only: live
    # PR grading should never silently apply historical receipt repairs.
    legacy_receipt_corrections = (
        legacy_corrections_mod.load_legacy_receipt_corrections()
    )
    if legacy_receipt_corrections and not args.quiet:
        print("[batch] legacy receipt corrections enabled", flush=True)

    # PR atlas-shadow-receipt-parallelism-v1: resolve effective worker
    # count for the inner receipt loop. ``--max-workers`` overrides
    # the cfg default; otherwise we honor ``batch_cfg.grading_max_workers``
    # (which already reflects ATLAS_SHADOW_GRADING_MAX_WORKERS via the
    # config loader). ``None`` means "use cfg" — pass it through
    # untouched so grade_one_packet -> run_pr_grading can apply the
    # documented resolution order.
    cli_max_workers = getattr(args, "max_workers", None)
    effective_max_workers: Optional[int]
    if cli_max_workers is not None:
        effective_max_workers = cli_max_workers
    else:
        effective_max_workers = None  # delegate to cfg
    if not args.quiet:
        resolved_workers = (
            effective_max_workers
            if effective_max_workers is not None
            else getattr(batch_cfg, "grading_max_workers", 1)
        )
        print(
            f"[batch] receipt-grading workers: {resolved_workers}",
            flush=True,
        )

    for idx, qna_log in enumerate(qna_logs, start=1):
        slug = _packet_slug_from_qna_path(qna_log)
        packet_commit_sha = packet_commit_shas.get(qna_log, commit_sha)
        if not args.quiet:
            print(
                f"[{idx}/{len(qna_logs)}] grading {slug} "
                f"@ {packet_commit_sha[:12]}...",
                flush=True,
            )
        try:
            outcome = grade_one_packet(
                batch_cfg,
                repo_full_name=repo_full_name,
                commit_sha=packet_commit_sha,
                qna_log_path=qna_log,
                github_token=github_token,
                core_repo_path=core_repo_path,
                atlas_query_cache=atlas_query_cache,
                max_workers=effective_max_workers,
                revision_pin_mode=revision_pin_mode,
                legacy_receipt_corrections=legacy_receipt_corrections,
            )
        except Exception as exc:  # noqa: BLE001 — one bad packet shouldn't abort
            partial_failures += 1
            outcome = {
                "packet_slug": slug,
                "packet_qna_log_path": qna_log,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "summaries": [],
                "base_sha": packet_commit_sha,
                "head_sha": packet_commit_sha,
                "run_commit_sha": commit_sha,
                "packet_base_sha": packet_commit_sha,
                "packet_sha_mode": packet_sha_mode,
                "revision_pin_mode": revision_pin_mode,
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
            elif sum(_summary_total(s) for s in outcome.get("summaries", [])) == 0:
                # Codex r3 + r4 P2 defensive: an "ok" outcome with zero
                # total receipts means the packet wasn't actually graded.
                # Two sub-cases collapse into this check:
                #   (a) outcome.summaries == []           (no summary appended)
                #   (b) outcome.summaries has entries     (summary appended
                #       but each has total=0 — e.g. qna log parsed but
                #       receipt parser found zero usable receipts)
                # Both look like "0% of nothing = success" to the aggregate
                # math otherwise. Catches future failure modes too.
                partial_failures += 1
                outcome["status"] = "ok_but_no_receipts"
                if not args.quiet:
                    print(
                        f"  WARN: {slug} ok but produced 0 receipts "
                        f"(likely unreadable qna log or unparseable receipts)",
                        file=sys.stderr,
                    )

        outcome.setdefault("run_commit_sha", commit_sha)
        outcome.setdefault("packet_base_sha", packet_commit_sha)
        outcome.setdefault("packet_sha_mode", packet_sha_mode)
        outcome.setdefault("revision_pin_mode", revision_pin_mode)
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
        packet_sha_mode=packet_sha_mode,
        revision_pin_mode=revision_pin_mode,
    )
    totals = _aggregate_run_totals(packet_outcomes)
    summary_path = write_per_run_summary_md(
        run_name,
        output_dir,
        commit_sha=commit_sha,
        packet_sha_mode=packet_sha_mode,
        revision_pin_mode=revision_pin_mode,
        packet_outcomes=packet_outcomes,
        overall_pct=totals["overall_pct"],
        total_receipts=totals["total_receipts"],
        total_correct=totals["total_correct"],
        # PR #14: clean denominator passthrough.
        clean_overall_pct=totals.get("clean_overall_pct"),
        clean_total=totals.get("clean_total"),
        total_excluded=totals.get("total_excluded", 0),
        total_skipped_receipt_stale=totals.get("total_skipped_receipt_stale", 0),
        # PR #15: run-commit drift breakout for the per-run summary line.
        total_skipped_run_commit_line_drift=totals.get(
            "total_skipped_run_commit_line_drift", 0
        ),
        # PR #17: four non-retrieval skip breakouts.
        total_skipped_non_repo_evidence=totals.get(
            "total_skipped_non_repo_evidence", 0
        ),
        total_skipped_absence_search=totals.get(
            "total_skipped_absence_search", 0
        ),
        total_skipped_unavailable_source_ref=totals.get(
            "total_skipped_unavailable_source_ref", 0
        ),
        total_skipped_doc_corpus_excluded=totals.get(
            "total_skipped_doc_corpus_excluded", 0
        ),
        # PR #20: command-snapshot lane.
        total_skipped_command_snapshot=totals.get(
            "total_skipped_command_snapshot", 0
        ),
        total_skipped_revision_not_indexed=totals.get(
            "total_skipped_revision_not_indexed", 0
        ),
        total_skipped_legacy_receipt_defect=totals.get(
            "total_skipped_legacy_receipt_defect", 0
        ),
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
        "--packet-sha-mode",
        choices=["created", "latest-change", "run-commit"],
        default="run-commit",
        help=(
            "How to choose each packet's synthetic PR base/head SHA. "
            "'run-commit' (default) grades every packet at --commit-sha — "
            "fast, efficient, and matches the documented baseline workflow "
            "where only the latest commit is in the ledger. "
            "'created' uses the commit where that packet's 02-qna-log.md "
            "was added; 'latest-change' uses the latest commit touching "
            "the qna log at or before --commit-sha. The historical modes "
            "require EVERY resolved per-packet SHA to be in the daemon's "
            "ingest ledger; otherwise the orchestrator's I2 invariant "
            "soft-passes each unindexed packet as `revision_not_indexed`. "
            "Pre-ingest those SHAs via `make ingest-replay COMMIT=<sha>` "
            "before enabling historical modes (codex r1 PR #9 fix — the "
            "prior `created` default caused default batch runs to skip "
            "every packet)."
        ),
    )
    p.add_argument(
        "--revision-pin-mode",
        choices=["receipt-source", "event-base"],
        default="receipt-source",
        help=(
            "How to pin Atlas queries inside each packet. "
            "'receipt-source' (default for offline baselines) looks up each "
            "code receipt's source_commit in the ingest ledger and queries "
            "that exact code_revision_id, so Planner and Atlas evidence are "
            "scored against the same snapshot. Missing historical SHAs are "
            "reported as skipped_revision_not_indexed. 'event-base' preserves "
            "the live PR-gate behavior: every receipt uses the synthetic PR "
            "base SHA selected by --packet-sha-mode."
        ),
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
    p.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help=(
            "Per-packet receipt-grading concurrency for this batch. "
            "When unset, falls back to "
            "`ingest_daemon.grading_max_workers` in shadow-config.yaml "
            "(default 1 = serial). `ATLAS_SHADOW_GRADING_MAX_WORKERS` "
            "in the process env overrides the YAML default. This flag "
            "overrides both for the current invocation. Each worker "
            "owns one atlas-query subprocess + one Anthropic API call "
            "per receipt; recommended range 2-8 (PR atlas-shadow-receipt-"
            "parallelism-v1)."
        ),
    )
    return p
