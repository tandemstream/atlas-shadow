#!/usr/bin/env python3
"""Generate layered-oracle sidecars for historical packet artifacts.

This is intentionally a migration tool, not the long-term authoring path.
New packets should carry typed oracle metadata in their packet Q&A.  Historical
packets can use these sidecars so the layered report can run without rewriting
old audit logs.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


PASS_GRADES = {"full_match", "partial_match"}
CONTEXT_EVIDENCE_TYPES = {"external_tool_docs", "user_context"}
COMMAND_STATUSES = {
    "skipped_command_snapshot",
    "skipped_absence_search",
}
UNRESOLVED_STATUSES = {
    "skipped_doc_corpus_excluded",
    "skipped_unavailable_source_ref",
    "skipped_receipt_stale",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="Run directory containing packets/*.json.",
    )
    parser.add_argument(
        "--output-dir",
        default=Path("docs/pilots"),
        type=Path,
        help="Directory for *-layered-oracle.yaml sidecars.",
    )
    parser.add_argument(
        "--index-path",
        default=Path("docs/pilots/layered-oracle-migration-index.md"),
        type=Path,
        help="Markdown index summarizing generated sidecars.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing sidecars, including hand-authored pilots.",
    )
    args = parser.parse_args()

    packet_paths = sorted((args.run_dir / "packets").glob("*.json"))
    if not packet_paths:
        raise SystemExit(f"no packet JSON files found under {args.run_dir / 'packets'}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for packet_path in packet_paths:
        spec = build_spec(packet_path)
        out_path = args.output_dir / f"{spec['packet_id']}-layered-oracle.yaml"
        existed = out_path.exists()
        if existed and not args.force:
            summaries.append(summarize_existing(packet_path, out_path))
            continue
        out_path.write_text(
            yaml.safe_dump(spec, sort_keys=False, width=96),
            encoding="utf-8",
        )
        summaries.append(summarize_spec(spec, packet_path, out_path, existed=existed))

    write_index(args.index_path, summaries, args.run_dir)
    print(f"wrote migration index: {args.index_path}")
    for item in summaries:
        print(
            f"{item['packet_id']}: {item['status']} "
            f"evidence={item['evidence']} context={item['context']} "
            f"command={item['command']} unresolved={item['unresolved']}"
        )
    return 0


def build_spec(packet_path: Path) -> dict[str, Any]:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet_id = packet.get("packet_slug") or packet_path.stem
    rows = packet_rows(packet)
    oracle_rows = [classify_row(row) for row in rows]
    counts = Counter(row["oracle_bucket"] for row in oracle_rows)
    unresolved = sum(1 for row in oracle_rows if row["oracle_status"] != "verified")
    verified = len(oracle_rows) - unresolved

    return {
        "schema_version": 1,
        "packet_id": packet_id,
        "title": title_from_slug(packet_id),
        "migration_status": "auto_drafted",
        "migration_source": {
            "run_dir": str(packet_path.parent.parent),
            "packet_json": str(packet_path),
            "note": (
                "Generated from shadow artifact rows for historical migration. "
                "Review synthesis_oracle before treating synthesis scores as authoritative."
            ),
        },
        "cost": {
            "claim_count": len(oracle_rows),
            "verified_claim_count": verified,
            "unresolved_claim_count": unresolved,
            "tool_calls": None,
            "model_calls": None,
            "notes": "Auto-drafted historical sidecar; real cost tracking is future work.",
        },
        "evidence_oracle": {"rows": oracle_rows},
        "synthesis_oracle": draft_synthesis_oracle(packet_id, counts),
    }


def packet_rows(packet: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for summary in packet.get("summaries", []):
        artifact = summary.get("artifact") or {}
        rows.extend(artifact.get("rows", []))
    return rows


def classify_row(row: dict[str, Any]) -> dict[str, Any]:
    qid = str(row.get("question_id") or "")
    evidence_type = str(row.get("evidence_type") or "source_excerpt")
    score_status = str(row.get("score_status") or "counted")
    grade = str(row.get("grade") or "not_run")
    question = str(row.get("question") or "")

    bucket = "evidence"
    oracle_status = "verified"
    oracle_failure_type = None
    planner_status = "pass"
    planner_failure_type = None
    synthesis_role = "required_point"
    claim_type = infer_claim_type(row)

    if evidence_type in CONTEXT_EVIDENCE_TYPES:
        bucket = "context"
        planner_status = "not_scored"
        synthesis_role = "context_only"
    elif score_status in COMMAND_STATUSES or evidence_type == "absence_search":
        bucket = "command"
        planner_status = "not_scored"
        synthesis_role = "required_point"
    elif score_status in UNRESOLVED_STATUSES:
        bucket = "context"
        oracle_status = "unresolved"
        oracle_failure_type = unresolved_reason(row)
        planner_status = "not_scored"
        synthesis_role = "context_only"
    elif score_status == "skipped_run_commit_line_drift":
        bucket = "evidence"
        planner_status = "fail"
        planner_failure_type = "current_benchmark_line_drift"
    elif row.get("source_snapshot_status") == "git_source_hash_mismatch":
        bucket = "evidence"
        planner_status = "fail"
        planner_failure_type = "receipt_hash_mismatch"
    elif row.get("run_snapshot_status") in {
        "run_commit_hash_mismatch",
        "run_commit_source_missing",
    }:
        bucket = "evidence"
        planner_status = "fail"
        planner_failure_type = "current_benchmark_line_drift"

    if bucket == "evidence" and grade not in PASS_GRADES and score_status == "counted":
        note_suffix = " Atlas currently misses this verified evidence row."
    else:
        note_suffix = ""

    item: dict[str, Any] = {
        "qid": qid,
        "claim_type": claim_type,
        "evidence_type": evidence_type,
        "oracle_bucket": bucket,
        "oracle_status": oracle_status,
        "planner_evidence_status": planner_status,
        "synthesis_role": synthesis_role,
        "required_point_ids": ["S1"] if synthesis_role == "required_point" else [],
        "notes": compact_note(question + note_suffix),
    }
    if oracle_failure_type:
        item["oracle_failure_type"] = oracle_failure_type
    if planner_failure_type:
        item["planner_failure_type"] = planner_failure_type
    return item


def infer_claim_type(row: dict[str, Any]) -> str:
    evidence_type = str(row.get("evidence_type") or "")
    question = str(row.get("question") or "").lower()
    if evidence_type == "absence_search" or "no " in question or "does not" in question:
        return "absence_claim"
    if evidence_type in CONTEXT_EVIDENCE_TYPES:
        return evidence_type
    if "table" in question or "schema" in question or ".sql" in question:
        return "schema_fact"
    if "doc" in question or "markdown" in question:
        return "doc_structure"
    if "decision" in question or "proposal" in question or "plan" in question:
        return "design_context"
    return "current_behavior"


def unresolved_reason(row: dict[str, Any]) -> str:
    status = str(row.get("score_status") or "")
    if status == "skipped_doc_corpus_excluded":
        return "doc_corpus_excluded"
    if status == "skipped_receipt_stale":
        return "receipt_source_missing"
    if status == "skipped_unavailable_source_ref":
        return "unavailable_source_ref"
    return status or "unresolved"


def draft_synthesis_oracle(packet_id: str, counts: Counter[str]) -> dict[str, Any]:
    return {
        "status": "draft",
        "ideal_conclusion": (
            f"Historical migration draft for {packet_id}. A packet owner should replace "
            "this with the intended planning conclusion before synthesis scores are used."
        ),
        "required_points": [
            {
                "id": "S1",
                "text": (
                    "Use verified evidence rows to support the packet's main planning "
                    "or implementation conclusion."
                ),
            }
        ],
        "forbidden_claims": [
            {
                "id": "F1",
                "text": "Do not treat unresolved or context-only rows as verified Atlas evidence.",
            }
        ],
        "uncertainty_notes": [
            {
                "id": "U1",
                "text": (
                    f"Auto-drafted bucket counts: evidence={counts.get('evidence', 0)}, "
                    f"context={counts.get('context', 0)}, command={counts.get('command', 0)}."
                ),
            }
        ],
        "criteria": [],
    }


def title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


def compact_note(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= 180:
        return text
    return text[:177].rstrip() + "..."


def summarize_existing(packet_path: Path, out_path: Path) -> dict[str, Any]:
    spec = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
    return summarize_spec(spec, packet_path, out_path, existed=True, status="kept_existing")


def summarize_spec(
    spec: dict[str, Any],
    packet_path: Path,
    out_path: Path,
    *,
    existed: bool,
    status: str | None = None,
) -> dict[str, Any]:
    rows = spec.get("evidence_oracle", {}).get("rows", []) or []
    counts = Counter(row.get("oracle_bucket", "evidence") for row in rows)
    unresolved = sum(1 for row in rows if row.get("oracle_status") != "verified")
    migration_status = str(spec.get("migration_status") or "authored")
    return {
        "packet_id": spec.get("packet_id") or packet_path.stem,
        "path": str(out_path),
        "source": str(packet_path),
        "status": migration_status,
        "rows": len(rows),
        "evidence": counts.get("evidence", 0),
        "context": counts.get("context", 0),
        "command": counts.get("command", 0),
        "unresolved": unresolved,
        "synthesis_status": (spec.get("synthesis_oracle") or {}).get("status", "authored"),
    }


def write_index(index_path: Path, summaries: list[dict[str, Any]], run_dir: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Layered Oracle Migration Index",
        "",
        f"Generated from `{run_dir}`.",
        "",
        "Historical packets use sidecar specs during migration. New packets should put typed oracle metadata in the packet Q&A file directly.",
        "",
        "| Packet | Status | Rows | Evidence | Context | Command | Unresolved | Synthesis |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in sorted(summaries, key=lambda x: str(x["packet_id"])):
        lines.append(
            f"| `{item['packet_id']}` | {item['status']} | {item['rows']} | "
            f"{item['evidence']} | {item['context']} | {item['command']} | "
            f"{item['unresolved']} | {item['synthesis_status']} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `authored` specs have hand-written synthesis scoring and can produce Planner/Atlas synthesis scores.",
            "- `draft` specs have migrated evidence buckets but require packet-owner synthesis review before synthesis scores are authoritative.",
            "- Sidecars are for historical packets only; the forward path is typed Q&A authoring.",
            "- Under `run_commit` benchmarking, Planner Evidence is charged for receipt drift while Atlas retrieves the current corpus. If Atlas Evidence exceeds Planner Evidence, do not read that as Atlas beating an ideal planner; read it as a corpus-alignment warning. The long-term comparison target is `receipt_commit` grading.",
            "",
        ]
    )
    index_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
