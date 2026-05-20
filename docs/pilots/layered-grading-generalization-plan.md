# Layered Shadow Grading Generalization Plan

This follows the Phase-1 pilot on
`2026-05-13-atlas-evidence-expansion-v1`.

The important design correction from the pilot:

> Planner Evidence and Atlas Evidence use the same denominator.

If a row is in the **Evidence Oracle**, Atlas is accountable for it.
Rows that are useful but not retrieval evidence belong in **Context
Oracle** or **Command Oracle**, not in a separate "not eligible" branch
inside the evidence score.

## Phase 1 Result

The pilot command:

```bash
.venv/bin/python -m atlas_shadow.cli shadow-layered-report \
  --packet-json shadow-runs/<run>/packets/2026-05-13-atlas-evidence-expansion-v1.json \
  --oracle-spec docs/pilots/2026-05-13-atlas-evidence-expansion-v1-layered-oracle.yaml
```

produces:

- Evidence Oracle coverage: `evidence verified + context verified +
  command verified + unresolved`
- Planner Evidence: `planner_pass / evidence_verified`
- Atlas Evidence: `atlas_pass / evidence_verified`
- Planner Synthesis: rubric score against the synthesis oracle
- Atlas Synthesis: rubric score against the synthesis oracle
- Cost: pilot cost fields
- Failure counts by layer

## Full Historical Migration Result

The Phase-1 branch now includes sidecar specs for every packet present
in `shadow-runs/baseline-2026-05-20-post28-post473`:

```bash
python3 scripts/generate_layered_oracle_specs.py \
  --run-dir shadow-runs/baseline-2026-05-20-post28-post473

.venv/bin/python -m atlas_shadow.cli shadow-layered-batch \
  --run-dir shadow-runs/baseline-2026-05-20-post28-post473 \
  --oracle-dir docs/pilots
```

The generated run summary renders 19 packet reports and one aggregate
summary:

| Dimension | Score |
|---|---:|
| Oracle Coverage | 81 evidence + 7 context + 82 command + 96 unresolved |
| Planner Evidence | 40/81 = 49.4% |
| Planner Synthesis | 27/30 = 90.0% |
| Atlas Evidence | 63/81 = 77.8% |
| Atlas Synthesis | 24.5/30 = 81.7% |

Interpretation:

- The evidence rows now use one shared Planner/Atlas denominator.
- `command` and `context` rows are visible but do not inflate Atlas
  Evidence.
- Auto-generated historical sidecars have draft synthesis oracles; their
  synthesis scores stay `0/0 (n/a)` until a packet owner authors the
  synthesis rubric.
- The sidecars are migration artifacts for old packets only.

The full migration also confirms that Planner Evidence is not a stable
"upper bound" under `run_commit` benchmarking. On this run, aggregate
Atlas Evidence is higher than Planner Evidence (`63/81 = 77.8%` vs.
`40/81 = 49.4%`) because many planner rows are charged for receipt drift
while Atlas retrieves the current corpus. That is useful diagnostic data,
but it is not an apples-to-apples capability comparison. The
Planner-vs-Atlas comparison should target `receipt_commit` once
historical indexed revisions are cheap enough.

The migration index lives at
`docs/pilots/layered-oracle-migration-index.md`.

## Phase 2 Goals

Generalize the pilot without a flag-day migration.

### 1. Dual-format typed Q&A support

Existing Markdown receipts continue to work. New packet Q&A entries may
add a typed metadata block directly in `02-qna-log.md`; sidecar specs
are only for historical packets.

```yaml
oracle:
  schema_version: 1
claim_type: current_behavior
evidence_type: source_excerpt
oracle_bucket: evidence
synthesis_role: required_point
source_ref:
  path: core/query/evidence_expansion.py
  lines: 120-148
cost:
  expected_lookup_type: source_read
```

Forward packet authors should not create `docs/pilots/*-layered-oracle.yaml`
for new work. The packet's typed Q&A block is the oracle source of truth;
the shadow grader may materialize a derived report artifact, but not a
separate editable sidecar.

Allowed `oracle_bucket` values:

- `evidence`: enters the shared Planner/Atlas evidence denominator.
- `context`: verified context for synthesis, not retrieval evidence.
- `command`: deterministic command/absence checks.
- `unresolved`: cannot form a reliable oracle row.

### 2. Evidence Oracle builder

For each packet:

- Parse existing receipt fields.
- Apply typed overrides when present.
- Deterministically resolve source, doc, and command references.
- Emit `verified`, `context`, `command`, and `unresolved` rows.

Do not compute a misleading "oracle score". Display:

```text
18 evidence verified + 0 context + 3 command + 8 unresolved
```

Unresolved rows lower **Benchmark Confidence**, not Planner/Atlas
correctness.

### 3. Synthesis Oracle builder

Start with hand-authored synthesis oracle specs. Only automate after
reviewers agree that the hand-authored target is fair.

The synthesis oracle contains:

- ideal conclusion
- required points
- forbidden claims
- uncertainty notes
- rubric criteria

### 4. Corpus-alignment policy

The pilot exposed an important issue: a receipt can be valid at its
pinned source commit while the Atlas corpus is graded at a later run
commit where those lines moved.

Phase 2 must pick one policy per run:

- `run_commit`: evidence rows are expected to resolve against the
  current indexed corpus. Line drift counts against Atlas unless the
  oracle source is repaired.
- `receipt_commit`: grade each evidence row against the corpus revision
  matching its source commit. This is cleaner but requires every receipt
  commit to be indexed.
- `latest_packet_change`: grade each packet at the latest commit touching
  its Q&A file. This is a compromise but still needs indexed historical
  revisions.

Recommendation after the Phase-1 pilot: layered reports should target
`receipt_commit` once historical coverage is cheap enough. Receipts are
authored against specific source states, and the layered score should
not mix "Atlas missed this evidence" with "the repository moved since
the receipt was authored."

Until then, a `run_commit` layered report is still useful, but it must
surface `run_commit_line_drift` as both:

- a Planner Evidence failure when the planner's evidence is no longer
  valid for the current benchmark; and
- an Atlas Evidence failure when Atlas is asked to retrieve the same
  now-drifted evidence.

That behavior is intentionally strict. It makes corpus alignment visible
instead of hiding it in a separate denominator.

### 5. Global summary

The global dashboard should show one row per packet:

| Packet | Oracle Coverage | Benchmark Confidence | Planner Evidence | Planner Synthesis | Atlas Evidence | Atlas Synthesis | Cost | Top Failure Modes |
|---|---:|---|---:|---:|---:|---:|---:|---|
| packet-x | 18 evidence + 3 command + 8 unresolved | caution | 18/18 = 100.0% | 9/12 = 75.0% | 10/18 = 55.6% | 8/12 = 66.7% | 29 claims | run_commit_line_drift=6 |

Raw Atlas score remains available only as legacy/debug data.

The two-packet sanity check added one refinement: command-heavy packets
can be valuable workflow benchmarks while having no Atlas Evidence
denominator. The global summary should therefore keep command/context
coverage visible, even when Planner Evidence and Atlas Evidence are
`0/0`.

Example:

| Packet | Oracle Coverage | Command/Context Oracle | Planner Evidence | Atlas Evidence |
|---|---:|---:|---:|---:|
| content-dedup-retrieval-v1 | 0 evidence + 12 command + 0 unresolved | 12 command + 0 context | 0/0 | 0/0 |

This is not a failed Atlas benchmark; it is a command-verified planning
benchmark. Phase 2 may add a dedicated `Command Checks` score, but it
must not smuggle command rows into the shared Planner/Atlas evidence
denominator.

### 6. Cost

Actual cost should be measured, not guessed:

- claim_count
- evidence_verified_count
- context_verified_count
- command_verified_count
- unresolved_count
- tool_calls
- model_calls
- wall_time_ms
- review_rounds
- cost_per_verified_evidence_row
- cost_per_synthesis_point

Expected effort can be added later as a calibration target, not as the
primary cost signal.

## Implementation Questions

The same structure should handle implementation packets by adding claim
types:

- `implementation_result`
- `test_coverage`
- `acceptance_gate`
- `regression_guard`
- `known_limitation`
- `operator_behavior`
- `integration_result`

Implementation rows still choose an `oracle_bucket`. A test assertion is
usually `evidence`; a manual smoke checklist might be `command`; a known
limitation might be `context`.

## Acceptance Criteria For Generalization

1. Every packet in the latest baseline run has a historical sidecar or
   a native typed Q&A oracle.
2. `shadow-layered-batch` renders every packet report plus a run summary.
3. Planner Evidence and Atlas Evidence always share the same evidence
   denominator.
4. Context/command/unresolved rows appear in oracle coverage and synthesis,
   but never in the evidence denominator.
5. New packets author typed oracle metadata in `02-qna-log.md`, not in
   sidecars.
6. The dashboard hides raw Atlas score from the headline and keeps it in
   legacy/debug output.
