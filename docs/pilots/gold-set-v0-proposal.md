# Shadow Gold Set V0 Proposal

Status: proposal, not yet an enforced benchmark set.

Source run: `shadow-runs/baseline-2026-05-20-main-legacy-corpus-receipt-source-blessed`
after PRs #37, #38, #39, #40, #41, and #42.

## Purpose

Shadow grading should use a broad set of real workstream tasks, not synthetic
questions designed after the fact. The corpus is valuable because it captures
the kinds of planning, lookup, verification, and synthesis failures LLMs
actually make while doing product work.

This document proposes a first gold-set taxonomy over the historical layered
packets. It separates:

- packets that are ready to headline Atlas evidence quality;
- packets that are useful workflow/command benchmarks;
- packets that need hand rubric work before synthesis can be trusted; and
- packets that should remain corpus-hygiene/context only for now.

## Current Aggregate

The blessed receipt-source baseline currently reports:

| Dimension | Score |
|---|---:|
| Oracle Coverage | 79 evidence + 7 context + 82 command + 98 unresolved |
| Planner Evidence | 38/79 = 48.1% |
| Planner Synthesis | 55/68 = 80.9% |
| Atlas Evidence | 79/79 = 100.0% |
| Atlas Synthesis | 54/68 = 79.4% |
| Synthesis Readiness | 10/32 = 31.2% |

Read this as:

- Atlas evidence retrieval is clean on the verified evidence denominator.
- Planner evidence is still noisy on historical packets because old receipts
  were authored before the lookup-agent and strict receipt discipline.
- Synthesis is not gold yet. Most synthesis points are still
  `authored_static`, and only 10 of 32 required points have verified evidence
  support.

## Gold Tiers

### Tier A: Gold Evidence Candidates

These packets have verified Atlas evidence rows and represent real task types.
They can be used for Atlas Evidence, but only some are ready for Gold Synthesis.

| Packet | Task Type | Evidence | Atlas Evidence | Synthesis | Readiness | Proposed Role | Remaining Work |
|---|---|---:|---:|---:|---:|---|---|
| `2026-05-13-lookup-subagent-pattern-v1` | planning/tooling lookup | 9 | 100.0% | 80.0% | 0/5 | gold_evidence, synthesis_pilot | Link strict subcriteria to supporting qids or mark context-only. |
| `2026-05-14-atlas-doc-section-expansion-v1` | doc/schema/code synthesis | 18 | 100.0% | 80.0% | 1/6 | gold_evidence, synthesis_pilot | Finish strict-subcriteria evidence links for S2-S6. |
| `2026-05-14-mattergraph-document-pack-v1` | document retrieval | 13 | 100.0% | n/a | n/a | gold_evidence | Add hand-authored synthesis rubric if this should test answer quality. |
| `2026-05-14-mattergraph-code-pack-corpus-v1` | code-pack corpus behavior | 4 | 100.0% | 66.7% | 1/3 | gold_evidence_candidate | Strict-subcriteria pass; decide how to handle context rows. |
| `2026-05-15-atlas-document-authority-staleness-v1` | document authority/staleness | 8 | 100.0% | 88.9% | 1/3 | gold_evidence_candidate | Strict-subcriteria pass; preserve policy-heavy caveats. |

### Tier B: Broad-Coverage Synthesis Candidates

These packets are useful because they exercise distinct reasoning shapes, but
they need hand grading before their synthesis numbers should be trusted.

| Packet | Task Type | Current Issue | Proposed Role | Required Hand Work |
|---|---|---|---|---|
| `2026-05-14-atlas-canonical-evidence-v1` | canonical citation/evidence paths | low readiness, many unresolved historical rows | synthesis_candidate | Convert to strict binary subcriteria; attach S2-S4 to evidence or mark non-scoreable. |
| `2026-05-13-atlas-evidence-expansion-v1` | evidence expansion and prompt constraints | broad packet, red confidence, but readiness is 6/6 | synthesis_candidate | Review whether unresolved rows should be context only; rewrite vague/negative criteria. |
| `2026-05-15-atlas-content-dedup-retrieval-v1` | command-shaped content dedup | command-only, readiness 0/5 | workflow_synthesis_candidate | Decide whether synthesis should be scored from command evidence or left command-only. |

### Tier C: Workflow/Command Gold Candidates

These packets do not headline Atlas Evidence because they are command-heavy, but
they are useful for workflow correctness, implementation discipline, and future
planner-cost measurement.

| Packet | Task Type | Command Rows | Proposed Role | Remaining Work |
|---|---|---:|---|---|
| `2026-05-14-personas-codex-impl-loop-v1` | implementation workflow | 17 | workflow_command_gold | Add cost fields and optional implementation synthesis rubric. |
| `2026-05-15-codepack-document-semantics-v1` | document semantics checks | 13 | workflow_command_gold | Keep command oracle; add synthesis only if answer quality matters. |
| `2026-05-13-atlas-shadow-continuous-ingest-v1` | daemon/ingest operations | 8 | workflow_candidate | Resolve two unresolved rows or keep as caution-tier command packet. |
| `2026-05-14-atlas-shadow-pre-merge-grading-gate-v1` | pre-merge grading mechanics | 7 | workflow_candidate | Too many unresolved rows for gold; useful for operator regression tests. |

### Tier D: Not Gold Yet

These packets remain useful as corpus-hygiene diagnostics but should not be
headline gold until their unresolved rows are repaired or intentionally typed
out.

| Packet | Reason |
|---|---|
| `2026-05-13-atlas-answer-synthesis-v1` | 9 unresolved rows, no scoreable evidence denominator. |
| `2026-05-13-atlas-shadow-phase2-v1` | 6 unresolved rows and no evidence denominator. |
| `2026-05-14-atlas-shadow-substrate-enablers-v1` | 11 unresolved rows and no evidence denominator. |
| `2026-05-14-mattergraph-pack-kernel-v1` | 10 unresolved rows and no evidence denominator. |
| `2026-05-15-atlas-codepack-corpus-hardening-v1` | 15 unresolved rows and no evidence denominator. |
| `2026-05-15-atlas-v3-citation-precision-v1` | only 2 evidence rows, 8 unresolved rows. |
| `2026-05-19-atlas-find-code-path-only-fastpath-v1` | 6 unresolved rows and no evidence denominator. |

## Proposed V0 Gold Set

Use these as the first named benchmark slice:

| Gold Slice | Packets |
|---|---|
| `gold_evidence_v0` | `lookup-subagent-pattern-v1`, `atlas-doc-section-expansion-v1`, `mattergraph-document-pack-v1`, `mattergraph-code-pack-corpus-v1`, `atlas-document-authority-staleness-v1` |
| `gold_synthesis_pilot_v0` | `lookup-subagent-pattern-v1`, `atlas-doc-section-expansion-v1` |
| `workflow_command_gold_v0` | `personas-codex-impl-loop-v1`, `codepack-document-semantics-v1` |
| `synthesis_candidate_v0` | `atlas-canonical-evidence-v1`, `atlas-evidence-expansion-v1`, `atlas-content-dedup-retrieval-v1` |

This gives broad real-world coverage:

- planner/tool lookup;
- doc/schema/code synthesis;
- document retrieval;
- code-pack corpus behavior;
- document authority and staleness;
- implementation workflow verification;
- deterministic command/absence checks.

## Hand Grading Still Needed

Gold Evidence:

- No broad hand grading required when evidence rows are deterministic and
  receipt-source pinned.
- Spot-check any row with unresolved or legacy-correction history before adding
  it to a named gold slice.

Gold Synthesis:

- Required. Every gold synthesis packet needs strict binary subcriteria.
- Every subcriterion must have:
  - `id`
  - `text`
  - `points`
  - `supporting_qids` or `scoreable: context_only`
  - `required_point_id`
  - `planner_status`
  - `atlas_status`
  - `*_miss_class` only when the actor status is `missed`
- Negative criteria should be rewritten as positive observable requirements.
- Fractional points should be eliminated.

Workflow/Command Gold:

- Hand grading should focus on whether the command actually proves a
  load-bearing implementation or planning assertion.
- Synthesis should stay `n/a` unless a reviewer authors an answer-quality
  rubric.

## Separate LLM Grader Calibration

The next useful experiment is not to replace the deterministic oracle; it is to
calibrate the judge:

1. Select the gold synthesis pilot packets.
2. Run the current grader and one alternate LLM judge over the same strict
   subcriteria.
3. Classify disagreements:
   - current grader too harsh;
   - alternate grader too lenient;
   - rubric ambiguous;
   - answer genuinely missing evidence;
   - answer correct shape without evidence.

If an alternate judge scores higher, that is not automatically better. It is
better only when disagreement review shows it is more faithful to the strict
oracle.

## Next Actions

1. Finish strict-subcriteria passes for:
   - `atlas-canonical-evidence-v1`;
   - `atlas-document-authority-staleness-v1`;
   - `mattergraph-code-pack-corpus-v1`.
2. Add a small selector/config for named gold slices once this document is
   reviewed.
3. Run layered summaries filtered by `gold_evidence_v0`,
   `gold_synthesis_pilot_v0`, and `workflow_command_gold_v0`.
4. Revisit the set after two weeks of new forward-Q&A packets; the goal is a
   growing corpus of real tasks, not a frozen synthetic benchmark.
