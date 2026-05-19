#!/usr/bin/env python3
"""Anchor-shape clusterer — Codex feedback round-2.

Clusters receipts by anchor shape and reports pass rate per cluster ×
lane. The acceptance signal: the failure pattern should localize to
specific anchor types (e.g. `.sql` DDL or path-only), so the next
tuning loop knows exactly which anchor shape to optimize first.

Clusters (per Codex):
  - source_path + source_lines + line_count_small  (sed-range eligible)
  - source_path + source_lines + line_count_large
  - path + symbol (no line range)
  - path-only
  - sql/schema (path ends in .sql)
  - python code (path ends in .py)
  - doc markdown (path ends in .md outside docs/work)
  - docs/work excluded (PR #277 exclusion)
  - missing source_path
  - missing source_lines but has source_path
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

ATLAS_SHADOW = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_SHADOW))

from atlas_shadow.ingest_daemon import grader_service as gs

PROBE_DEFAULT = ATLAS_SHADOW / "shadow-runs" / "probe-2026-05-19-post-pr12-check"
CORE_PATH = Path("/Users/ray/tandemstream/core--shadow-runtime")


def _line_count(spec: Optional[str]) -> Optional[int]:
    if not spec:
        return None
    s = spec.strip().strip("`").strip()
    if "-" in s:
        try:
            lo, hi = s.split("-", 1)
            return int(hi) - int(lo) + 1
        except ValueError:
            return None
    try:
        return 1  # single-line anchor
    except ValueError:
        return None


def _anchor_clusters(rec: Optional[gs.PacketReceipt], tool: Optional[str]) -> list[str]:
    """Return one or more cluster tags for a receipt. Multi-tag enables
    secondary breakdowns (e.g., 'sql/schema' + 'path+lines')."""
    tags: list[str] = []
    if rec is None:
        tags.append("no_receipt")
        return tags

    sp = (rec.source_path or "").strip()
    sl = (rec.source_lines or "").strip()
    sym = (rec.source_symbol or "").strip() if hasattr(rec, "source_symbol") else ""

    # Docs/work exclusion takes precedence — PR #277 deliberately omits.
    if sp and ("docs/work/" in sp or sp.startswith("docs/work/")):
        tags.append("docs_work_excluded")
        return tags

    if not sp:
        tags.append("no_source_path")
        return tags

    # Primary anchor shape
    if sl:
        lc = _line_count(sl)
        if lc is not None and lc <= 30:
            tags.append("path+lines_small")
        elif lc is not None and lc <= 100:
            tags.append("path+lines_medium")
        else:
            tags.append("path+lines_large")
    elif sym:
        tags.append("path+symbol")
    else:
        tags.append("path_only")

    # Secondary type tags (by file extension)
    sp_low = sp.lower()
    if sp_low.endswith(".sql"):
        tags.append("sql_schema")
    elif sp_low.endswith(".py"):
        tags.append("python_code")
    elif sp_low.endswith(".md"):
        tags.append("doc_markdown")
    elif "/makefile" in sp_low or sp_low.endswith("/makefile"):
        tags.append("makefile")
    elif sp_low.endswith(".sh"):
        tags.append("shell_script")
    elif sp_low.endswith(".yaml") or sp_low.endswith(".yml"):
        tags.append("yaml")

    return tags


def _load_receipts_for_packet(packet_slug: str) -> dict[str, gs.PacketReceipt]:
    candidates = list(CORE_PATH.glob(f"products/**/docs/work/{packet_slug}/02-qna-log.md"))
    if not candidates:
        return {}
    try:
        result = gs.parse_packet_receipts(candidates[0])
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[anchor-cluster] WARN: {candidates[0]}: {exc}\n")
        return {}
    receipts = result[0] if isinstance(result, tuple) else result
    return {r.question_id: r for r in receipts}


def main(probe_root: Path):
    print(f"# Anchor-shape clustering — probe: {probe_root}\n")

    rows_all = []
    for packet_dir in sorted(probe_root.glob("*/")):
        packet = packet_dir.name
        packet_json = packet_dir / "packets" / f"{packet}.json"
        if not packet_json.exists():
            continue
        d = json.loads(packet_json.read_text())
        receipts = _load_receipts_for_packet(packet)
        for s in d.get("summaries", []):
            art = s.get("artifact") or {}
            for r in art.get("rows") or []:
                qid = r.get("question_id")
                tool = r.get("tool")
                rec = receipts.get(qid)
                clusters = _anchor_clusters(rec, tool)
                rows_all.append({
                    "packet": packet,
                    "qid": qid,
                    "tool": tool,
                    "grade": r.get("grade"),
                    "snap": r.get("source_snapshot_status"),
                    "clusters": clusters,
                    "source_path": rec.source_path if rec else None,
                    "source_lines": rec.source_lines if rec else None,
                })

    print(f"## {len(rows_all)} rows clustered\n")

    # Cluster pass-rate (each row may contribute to multiple cluster tags)
    cluster_stats = defaultdict(lambda: {"pass": 0, "fail": 0})
    for r in rows_all:
        is_pass = r["grade"] in ("full_match", "partial_match")
        for tag in r["clusters"]:
            cluster_stats[tag]["pass" if is_pass else "fail"] += 1

    print("## Pass rate per anchor-shape tag\n")
    print(f"{'cluster':<28} {'n':>4} {'pass':>5} {'fail':>5} {'pass%':>7}")
    print("-" * 56)
    for tag in sorted(cluster_stats.keys(),
                       key=lambda t: -(cluster_stats[t]["pass"] + cluster_stats[t]["fail"])):
        s = cluster_stats[tag]
        n = s["pass"] + s["fail"]
        pct = (100 * s["pass"] / n) if n else 0
        print(f"{tag:<28} {n:>4} {s['pass']:>5} {s['fail']:>5} {pct:>6.1f}%")

    # Per-cluster failed receipts (so we can target tuning)
    print("\n## Failures per cluster\n")
    for tag in sorted(cluster_stats.keys()):
        if cluster_stats[tag]["fail"] == 0:
            continue
        fails = [r for r in rows_all if tag in r["clusters"]
                 and r["grade"] not in ("full_match", "partial_match")]
        if not fails:
            continue
        print(f"\n### cluster: {tag} ({len(fails)} fails)")
        for r in fails:
            sp = r.get("source_path") or "—"
            sl = r.get("source_lines") or "—"
            print(f"  - {r['packet']}/{r['qid']} via {r['tool']} "
                  f"grade={r['grade']} snap={r['snap']} "
                  f"anchor={sp}:{sl}")

    # Cross-tab cluster × snap status
    print("\n## Cross-tab: primary cluster × source_snapshot_status\n")
    primary_clusters = {"path+lines_small", "path+lines_medium", "path+lines_large",
                        "path+symbol", "path_only", "no_source_path",
                        "docs_work_excluded", "no_receipt"}
    snaps = sorted({r["snap"] or "null" for r in rows_all})
    print(f"{'cluster':<28}", end="")
    for s in snaps:
        print(f" {s[:14]:>15}", end="")
    print()
    print("-" * (28 + 16 * len(snaps)))
    for tag in primary_clusters:
        row_count = sum(1 for r in rows_all if tag in r["clusters"])
        if row_count == 0:
            continue
        print(f"{tag:<28}", end="")
        for s in snaps:
            n = sum(1 for r in rows_all if tag in r["clusters"]
                    and (r["snap"] or "null") == s)
            print(f" {n if n else '.':>15}", end="")
        print()


if __name__ == "__main__":
    probe = Path(sys.argv[1]) if len(sys.argv) > 1 else PROBE_DEFAULT
    main(probe)
