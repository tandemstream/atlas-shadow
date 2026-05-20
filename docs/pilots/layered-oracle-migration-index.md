# Layered Oracle Migration Index

Generated from `shadow-runs/baseline-2026-05-20-post28-post473`.

Historical packets use sidecar specs during migration. New packets should put typed oracle metadata in the packet Q&A file directly.

| Packet | Status | Rows | Evidence | Context | Command | Unresolved | Synthesis |
|---|---|---:|---:|---:|---:|---:|---|
| `2026-05-13-atlas-answer-synthesis-v1` | auto_drafted | 11 | 0 | 9 | 2 | 9 | draft |
| `2026-05-13-atlas-evidence-expansion-v1` | authored | 29 | 18 | 8 | 3 | 8 | authored |
| `2026-05-13-atlas-shadow-continuous-ingest-v1` | auto_drafted | 10 | 0 | 2 | 8 | 2 | draft |
| `2026-05-13-atlas-shadow-phase2-v1` | auto_drafted | 10 | 0 | 6 | 4 | 6 | draft |
| `2026-05-13-lookup-subagent-pattern-v1` | authored | 13 | 9 | 2 | 2 | 0 | authored |
| `2026-05-14-atlas-canonical-evidence-v1` | auto_drafted | 20 | 8 | 11 | 1 | 11 | draft |
| `2026-05-14-atlas-doc-section-expansion-v1` | auto_drafted | 23 | 18 | 4 | 1 | 3 | draft |
| `2026-05-14-atlas-shadow-pre-merge-grading-gate-v1` | auto_drafted | 12 | 0 | 5 | 7 | 5 | draft |
| `2026-05-14-atlas-shadow-substrate-enablers-v1` | auto_drafted | 23 | 0 | 12 | 11 | 11 | draft |
| `2026-05-14-mattergraph-code-pack-corpus-v1` | auto_drafted | 8 | 5 | 3 | 0 | 0 | draft |
| `2026-05-14-mattergraph-document-pack-v1` | auto_drafted | 13 | 13 | 0 | 0 | 0 | draft |
| `2026-05-14-mattergraph-pack-kernel-v1` | auto_drafted | 11 | 0 | 10 | 1 | 10 | draft |
| `2026-05-14-personas-codex-impl-loop-v1` | auto_drafted | 17 | 0 | 0 | 17 | 0 | draft |
| `2026-05-15-atlas-codepack-corpus-hardening-v1` | auto_drafted | 15 | 0 | 15 | 0 | 15 | draft |
| `2026-05-15-atlas-content-dedup-retrieval-v1` | authored | 12 | 0 | 0 | 12 | 0 | authored |
| `2026-05-15-atlas-document-authority-staleness-v1` | auto_drafted | 10 | 8 | 2 | 0 | 2 | draft |
| `2026-05-15-atlas-v3-citation-precision-v1` | auto_drafted | 10 | 2 | 8 | 0 | 8 | draft |
| `2026-05-15-codepack-document-semantics-v1` | auto_drafted | 13 | 0 | 0 | 13 | 0 | draft |
| `2026-05-19-atlas-find-code-path-only-fastpath-v1` | auto_drafted | 6 | 0 | 6 | 0 | 6 | draft |

## Notes

- `authored` specs have hand-written synthesis scoring and can produce Planner/Atlas synthesis scores.
- `draft` specs have migrated evidence buckets but require packet-owner synthesis review before synthesis scores are authoritative.
- Sidecars are for historical packets only; the forward path is typed Q&A authoring.
- Offline layered baselines should use `grade-packet-batch --revision-pin-mode receipt-source` before rendering this summary. That makes each Atlas query use the receipt's own indexed `source_commit`, keeping Planner Evidence and Atlas Evidence on the same source snapshot. Rows whose historical commit is missing from the ingest ledger are excluded as `skipped_revision_not_indexed` until that commit is replay-ingested.
- Under `run_commit` / `event-base` benchmarking, Planner Evidence is charged for receipt drift while Atlas retrieves the current corpus. If Atlas Evidence exceeds Planner Evidence, do not read that as Atlas beating an ideal planner; read it as a corpus-alignment warning.
