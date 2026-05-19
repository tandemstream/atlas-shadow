#!/usr/bin/env python3
"""Lane-aware precision classifier — Codex feedback round-2.

Assigns each receipt a lane and a failure-mode bucket per Codex's
taxonomy:

  Lanes:
    - explicit_source_fast_path  (receipt had source_path + source_lines;
                                  core PR #426 emits exact citation directly)
    - fuzzy_code_retrieval       (find_code/scan_search w/o exact anchors)
    - doc_resolver               (doc artifact lookup)

  Buckets:
    fast-path: exact_source_hit | exact_source_path_missing
               | exact_source_render_drift
    fuzzy:     fuzzy_wrong_file | fuzzy_right_file_no_overlap
               | fuzzy_right_file_partial_overlap
               | fuzzy_right_chunk_below_cutoff | fuzzy_score_gap
    doc:       doc_resolver_hit | doc_resolver_excluded_ground_truth
    misc:      stale_line_range | atlas_returned_nothing
               | grader_or_rubric_issue | other:<grade>

Phase 1 (this commit) infers lane from existing artifact fields +
qna-log anchor data only. Phase 2 will re-issue queries to capture
raw_result.retrieval_plan / reranker_trace for fuzzy-lane diagnostics.

Usage::

  python scripts/lane_classifier.py [<probe-output-dir>]

Defaults to shadow-runs/probe-2026-05-19-post-pr12-check/.

Acceptance: every failed receipt has a machine-assigned (lane, bucket)
tuple that tells us the next fix layer.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ATLAS_SHADOW = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_SHADOW))

from atlas_shadow.ingest_daemon import grader_service as gs

PROBE_DEFAULT = ATLAS_SHADOW / "shadow-runs" / "probe-2026-05-19-post-pr12-check"
CORE_PATH = Path("/Users/ray/tandemstream/core--shadow-runtime")

DOCS_WORK_PREFIX_PATTERNS = ("docs/work/", "/docs/work/")


@dataclass
class Classified:
    packet: str
    qid: str
    tool: Optional[str]
    grade: str
    lane: str
    bucket: str
    receipt_anchors: dict  # source_path / source_lines / source_commit / excerpt_sha
    snap_status: Optional[str]
    run_snap_status: Optional[str]  # PR #15
    answer_len: Optional[int]
    rationale: str
    confidence: Optional[float]


def _load_receipts_for_packet(packet_slug: str) -> dict[str, gs.PacketReceipt]:
    """Read the packet's qna log via grader_service.parse_packet_receipts.

    Returns dict {question_id: PacketReceipt}.
    """
    candidates = list(CORE_PATH.glob(f"products/**/docs/work/{packet_slug}/02-qna-log.md"))
    if not candidates:
        return {}
    qna_path = candidates[0]
    try:
        result = gs.parse_packet_receipts(qna_path)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[classifier] WARN: could not parse {qna_path}: {exc}\n")
        return {}
    # parse_packet_receipts returns (receipts, threshold_pct)
    receipts = result[0] if isinstance(result, tuple) else result
    return {r.question_id: r for r in receipts}


def _is_doc_resolver_excluded_ground_truth(receipt_path: Optional[str]) -> bool:
    """PR #277 excludes docs/work/** from shadow_ingest_docs to prevent
    grading ground-truth leakage. A doc_resolver query against such a
    path is expected to return empty — bucket as 'excluded' not as a fail.
    """
    if not receipt_path:
        return False
    return any(prefix in receipt_path for prefix in DOCS_WORK_PREFIX_PATTERNS)


def _classify_one(
    row: dict,
    receipts_by_qid: dict[str, gs.PacketReceipt],
    packet: str,
) -> Classified:
    qid = row.get("question_id", "?")
    tool = row.get("tool")
    grade = row.get("grade", "")
    snap = row.get("source_snapshot_status")
    run_snap = row.get("run_snapshot_status")  # PR #15
    rationale = (row.get("rationale") or "")
    confidence = row.get("confidence")
    rec = receipts_by_qid.get(qid)
    anchors = {
        "source_path": rec.source_path if rec else None,
        "source_lines": rec.source_lines if rec else None,
        "source_commit": rec.source_commit if rec else None,
        "excerpt_sha256": rec.excerpt_sha256 if rec else None,
    }

    # ----- lane inference -----
    if tool == "doc_resolver":
        lane = "doc_resolver"
    elif anchors["source_path"] and anchors["source_lines"]:
        # PR #426 fast-path-eligible. Whether it actually fired is a Phase 2
        # question (need raw_result.retrieval_plan). Phase 1 assumes the
        # path is followed when the prereqs are present.
        lane = "explicit_source_fast_path"
    else:
        lane = "fuzzy_code_retrieval"

    # ----- bucket within lane -----
    rat_l = rationale.lower()

    # Universal short-circuits
    if grade in ("error", "grader_error") or any("exception:" in w for w in (row.get("warnings") or [])):
        bucket = "grader_or_rubric_issue"
    elif grade == "revision_not_indexed":
        bucket = "revision_not_indexed"
    elif lane == "doc_resolver":
        if grade in ("full_match", "partial_match"):
            bucket = "doc_resolver_hit"
        elif _is_doc_resolver_excluded_ground_truth(anchors["source_path"]):
            bucket = "doc_resolver_excluded_ground_truth"
        elif grade == "atlas_not_found":
            # Doc not in corpus, but not because of the docs/work exclusion.
            bucket = "atlas_returned_nothing"
        else:
            bucket = f"doc_resolver_miss:{grade}"
    elif lane == "explicit_source_fast_path":
        if grade in ("full_match", "partial_match"):
            # Pass — but distinguish "fast path rendered cited bytes
            # exactly" from "atlas found right content despite the
            # cited path being absent at run commit (drift)."
            if snap == "git_source_hash_match":
                bucket = "exact_source_hit"
            elif snap == "git_source_missing":
                # Receipt anchor doesn't render at run commit, but
                # grader still passed — atlas's retrieval was good
                # enough to recover the cited content even though the
                # path/lines drifted. Pass-with-caveat.
                bucket = "exact_source_drift_pass"
            else:
                bucket = "exact_source_hit"
        elif grade == "no_match":
            # The fast path *should* have given a deterministic answer.
            # If it didn't pass, something stopped it firing OR atlas
            # returned wrong content within the right file.
            if snap == "git_source_missing":
                # Cited path/lines aren't at run commit. If rationale
                # confirms staleness ("older", "current version"),
                # split out as receipt-stale rather than atlas-precision.
                if any(s in rat_l for s in ("older", "newer version", "current version",
                                            "no longer", "depicts an older")):
                    bucket = "stale_line_range"
                else:
                    bucket = "exact_source_path_missing"
            elif snap == "git_source_hash_match":
                # Receipt is internally consistent at authoring time.
                # PR #15's run_snapshot lets us tell run-commit drift
                # apart from a genuine fast-path failure.
                if run_snap == "run_commit_hash_mismatch":
                    # File was edited between receipt commit and run
                    # commit. Atlas returned the right line range at
                    # the run commit but those lines now contain
                    # different code than the receipt described. Not
                    # an Atlas miss — the receipt's line anchor moved.
                    # (At grading time this row also lands
                    # ``score_status=skipped_run_commit_line_drift``
                    # and is excluded from the clean denominator.)
                    bucket = "run_commit_line_drift"
                elif run_snap == "run_commit_hash_match":
                    # Both snapshots match — receipt is internally
                    # consistent AND the same line range still renders
                    # the expected content at the run commit. Atlas
                    # still returned wrong content. This is the
                    # genuine fast-path failure case.
                    bucket = "exact_source_fast_path_didnt_fire"
                elif run_snap == "run_commit_source_missing":
                    # Path itself disappeared at run commit. Edge case —
                    # bucket as drift too (the receipt's anchor target
                    # is gone, not Atlas's fault).
                    bucket = "run_commit_line_drift"
                else:
                    # PR #15 field absent (pre-#15 artifact) or
                    # not_applicable / no_line_range. Fall back to the
                    # PR #14 interim bucket name so legacy artifacts
                    # still parse without crashing.
                    bucket = "needs_run_commit_snapshot"
            else:
                # snap is None / not_applicable / no_line_range — the
                # receipt didn't carry enough anchor for the
                # receipt-side snapshot check, so we can't reliably
                # compare to the run-side either.
                bucket = "needs_run_commit_snapshot"
        elif grade == "atlas_not_found":
            bucket = "atlas_returned_nothing"
        else:
            bucket = f"fast_path_other:{grade}"
    else:  # fuzzy_code_retrieval
        if grade in ("full_match", "partial_match"):
            bucket = "fuzzy_hit"  # collapsed pass-bucket; refine in Phase 2 with rank/score
        elif grade == "no_match":
            # Phase 1 heuristics based on rationale.
            if any(s in rat_l for s in ("wrong file", "different file", "another module")):
                bucket = "fuzzy_wrong_file"
            elif any(s in rat_l for s in ("wrong lines", "outside the cited", "different lines",
                                          "lines don't")):
                bucket = "fuzzy_right_file_no_overlap"
            elif any(s in rat_l for s in ("nearby", "neighbor", "approximat", "close to")):
                bucket = "fuzzy_right_file_partial_overlap"
            elif any(s in rat_l for s in ("rank", "below the cutoff", "ranked below")):
                bucket = "fuzzy_right_chunk_below_cutoff"
            elif any(s in rat_l for s in ("score gap", "lower score")):
                bucket = "fuzzy_score_gap"
            else:
                # Default fuzzy miss when we can't classify. This is the
                # bucket Codex's Phase 2 raw_result extraction will refine.
                bucket = "fuzzy_unclassified"
        elif grade == "atlas_not_found":
            bucket = "atlas_returned_nothing"
        else:
            bucket = f"fuzzy_other:{grade}"

    return Classified(
        packet=packet,
        qid=qid,
        tool=tool,
        grade=grade,
        lane=lane,
        bucket=bucket,
        receipt_anchors=anchors,
        snap_status=snap,
        run_snap_status=run_snap,
        answer_len=row.get("atlas_answer_len"),
        rationale=rationale[:400],
        confidence=confidence,
    )


def main(probe_root: Path):
    print(f"# Lane-aware classifier — probe: {probe_root}\n")
    if not probe_root.is_dir():
        sys.exit(f"ERROR: probe dir not found: {probe_root}")

    classified: list[Classified] = []
    for packet_dir in sorted(probe_root.glob("*/")):
        packet = packet_dir.name
        packet_json = packet_dir / "packets" / f"{packet}.json"
        if not packet_json.exists():
            continue  # skipped/incomplete packet
        rows = []
        d = json.loads(packet_json.read_text())
        for s in d.get("summaries", []):
            art = s.get("artifact") or {}
            rows.extend(art.get("rows") or [])
        receipts = _load_receipts_for_packet(packet)
        if not receipts:
            sys.stderr.write(f"[classifier] WARN: no receipts parsed for {packet}\n")
        for r in rows:
            classified.append(_classify_one(r, receipts, packet))

    if not classified:
        sys.exit("ERROR: no rows classified")

    # Lane × bucket cross-tab
    lane_bucket = defaultdict(Counter)
    for c in classified:
        lane_bucket[c.lane][c.bucket] += 1

    print("## Lane × bucket distribution\n")
    print(f"{'lane':<32} {'bucket':<42} {'count':>6}  {'%':>5}")
    print("-" * 90)
    total = len(classified)
    for lane in sorted(lane_bucket.keys()):
        sub = lane_bucket[lane]
        for bucket, n in sub.most_common():
            pct = 100 * n / total
            print(f"{lane:<32} {bucket:<42} {n:>6}  {pct:>4.1f}%")

    # Pass / fail summary per lane
    print("\n## Lane-level pass/fail\n")
    print(f"{'lane':<32} {'pass':>6} {'fail':>6} {'pass%':>7}")
    pass_buckets = {"exact_source_hit", "exact_source_drift_pass",
                    "doc_resolver_hit", "fuzzy_hit",
                    "doc_resolver_excluded_ground_truth"}
    for lane in sorted(lane_bucket.keys()):
        sub = lane_bucket[lane]
        passes = sum(n for b, n in sub.items() if b in pass_buckets)
        total_l = sum(sub.values())
        fails = total_l - passes
        pp = 100 * passes / total_l if total_l else 0
        print(f"{lane:<32} {passes:>6} {fails:>6} {pp:>6.1f}%")

    # Per-packet breakdown
    print("\n## Per-packet (lane, bucket) for non-pass receipts\n")
    by_packet = defaultdict(list)
    for c in classified:
        if c.bucket not in pass_buckets:
            by_packet[c.packet].append(c)
    for pkt in sorted(by_packet.keys()):
        items = by_packet[pkt]
        print(f"\n### {pkt} — {len(items)} non-pass")
        for c in items:
            anch = c.receipt_anchors
            sp = anch.get("source_path") or "—"
            sl = anch.get("source_lines") or "—"
            print(f"  - {c.qid}/{c.tool} [{c.lane} / {c.bucket}] "
                  f"snap={c.snap_status} "
                  f"anchor={sp}:{sl}")
            if c.rationale:
                print(f"      \"{c.rationale[:140]}...\"")

    # Lane stats compared to raw scores
    print("\n## Acceptance check: every failed receipt has a (lane, bucket)")
    unclassified = [c for c in classified if c.bucket in ("fuzzy_unclassified",)]
    print(f"  total receipts:         {len(classified)}")
    print(f"  pass (any pass bucket): {sum(1 for c in classified if c.bucket in pass_buckets)}")
    print(f"  fail:                   {sum(1 for c in classified if c.bucket not in pass_buckets)}")
    print(f"  unclassified (Phase 2 target): {len(unclassified)}")
    if unclassified:
        print("\n  ↑ These need raw_result.retrieval_plan/reranker_trace to refine:")
        for c in unclassified[:8]:
            print(f"    - {c.packet}/{c.qid} ({c.tool}) — snap={c.snap_status}")


if __name__ == "__main__":
    probe = Path(sys.argv[1]) if len(sys.argv) > 1 else PROBE_DEFAULT
    main(probe)
