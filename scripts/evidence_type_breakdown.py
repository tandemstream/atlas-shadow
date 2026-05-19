#!/usr/bin/env python3
"""Evidence-type breakdown — augments anchor-shape clustering with
the receipt's authoring intent (source_excerpt / external_tool_docs /
user_context / absence_search / etc.).

Surfaces receipts that should NOT be graded via find_code in the first
place (non-retrieval evidence types). Codex's classifier should route
these to a different lane or skip them.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ATLAS_SHADOW = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_SHADOW))

from atlas_shadow.ingest_daemon import grader_service as gs

PROBE_DEFAULT = ATLAS_SHADOW / "shadow-runs" / "probe-2026-05-19-post-pr12-check"
CORE_PATH = Path("/Users/ray/tandemstream/core--shadow-runtime")

# Codex's working assumption — receipts of these evidence_types are
# not graded against the repo; the grader should skip or route them
# to a manual-review lane.
NON_REPO_EVIDENCE_TYPES = {
    "external_tool_docs",
    "user_context",
    # absence_search has a command_text but no source_path/lines; it's
    # asking "show me that X is absent from the codebase." find_code
    # can't answer affirmatively for a negative claim — it needs an
    # explicit grep-and-assert-empty path.
    "absence_search",
}


def _load_receipts_for_packet(packet_slug):
    candidates = list(CORE_PATH.glob(f"products/**/docs/work/{packet_slug}/02-qna-log.md"))
    if not candidates:
        return {}
    try:
        result = gs.parse_packet_receipts(candidates[0])
    except Exception as exc:
        sys.stderr.write(f"WARN: {candidates[0]}: {exc}\n")
        return {}
    receipts = result[0] if isinstance(result, tuple) else result
    return {r.question_id: r for r in receipts}


def main(probe_root):
    rows = []
    for pkt_dir in sorted(probe_root.glob("*/")):
        pkt = pkt_dir.name
        pj = pkt_dir / "packets" / f"{pkt}.json"
        if not pj.exists():
            continue
        d = json.loads(pj.read_text())
        receipts = _load_receipts_for_packet(pkt)
        for s in d.get("summaries", []):
            for r in (s.get("artifact") or {}).get("rows") or []:
                qid = r.get("question_id")
                rec = receipts.get(qid)
                rows.append({
                    "packet": pkt,
                    "qid": qid,
                    "tool": r.get("tool"),
                    "grade": r.get("grade"),
                    "snap": r.get("source_snapshot_status"),
                    "evidence_type": rec.evidence_type if rec else None,
                    "source_path": rec.source_path if rec else None,
                    "source_lines": rec.source_lines if rec else None,
                    "command_text": rec.command_text if rec else None,
                })

    print(f"# Evidence-type breakdown — {len(rows)} rows\n")

    # Pass rate by evidence_type
    et_stats = defaultdict(lambda: {"pass": 0, "fail": 0})
    for r in rows:
        et = r["evidence_type"] or "missing"
        bucket = "pass" if r["grade"] in ("full_match", "partial_match") else "fail"
        et_stats[et][bucket] += 1

    print("## Pass rate by evidence_type\n")
    print(f"{'evidence_type':<24} {'n':>3} {'pass':>5} {'fail':>5} {'pass%':>7}   {'note':<30}")
    print("-" * 76)
    for et in sorted(et_stats.keys(), key=lambda k: -(et_stats[k]["pass"] + et_stats[k]["fail"])):
        s = et_stats[et]
        n = s["pass"] + s["fail"]
        pct = (100 * s["pass"] / n) if n else 0
        note = "non-repo, grader-routing" if et in NON_REPO_EVIDENCE_TYPES else ""
        print(f"{et:<24} {n:>3} {s['pass']:>5} {s['fail']:>5} {pct:>6.1f}%   {note:<30}")

    # The actual "Atlas-fixable" denominator
    print("\n## Adjusted pass rate after removing non-repo evidence types")
    print()
    in_scope = [r for r in rows if (r["evidence_type"] or "missing") not in NON_REPO_EVIDENCE_TYPES]
    in_scope_pass = sum(1 for r in in_scope if r["grade"] in ("full_match", "partial_match"))
    print(f"  total rows:                 {len(rows)}")
    print(f"  non-repo evidence types:    {len(rows) - len(in_scope)}")
    print(f"  in-scope (Atlas-fixable):   {len(in_scope)}")
    print(f"  in-scope pass:              {in_scope_pass}/{len(in_scope)} "
          f"({100*in_scope_pass/len(in_scope):.1f}%)")
    print(f"  in-scope fail:              {len(in_scope) - in_scope_pass}")

    # Listing of non-repo receipts that need grader routing
    print("\n## Receipts the grader should NOT route to find_code/scan_search\n")
    non_repo = [r for r in rows if (r["evidence_type"] or "missing") in NON_REPO_EVIDENCE_TYPES]
    for r in non_repo:
        cmd = (r.get("command_text") or "").strip()
        print(f"  {r['packet']}/{r['qid']} [evidence_type={r['evidence_type']}]")
        print(f"    grade was: {r['grade']}, snap: {r['snap']}")
        if cmd:
            print(f"    command:   {cmd[:120]}")

    # The remaining in-scope failures — these are the real Atlas-fix targets
    print("\n## In-scope failures (real Atlas-fix targets)\n")
    in_scope_fails = [r for r in in_scope if r["grade"] not in ("full_match", "partial_match")]
    for r in in_scope_fails:
        sp = r.get("source_path") or "—"
        sl = r.get("source_lines") or "—"
        print(f"  {r['packet']}/{r['qid']} via {r['tool']} "
              f"[{r['evidence_type'] or 'missing'}]")
        print(f"    grade={r['grade']}  snap={r['snap']}  anchor={sp}:{sl}")


if __name__ == "__main__":
    probe = Path(sys.argv[1]) if len(sys.argv) > 1 else PROBE_DEFAULT
    main(probe)
