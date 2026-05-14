# atlas-shadow

Offline Atlas-vs-grep shadow benchmark runner.

This repo is a **separate Python project** that benchmarks Atlas's retrieval
surfaces (`answer` / `find_code` / `scan_search`) against a fixture of graded
questions. It is *not* an Atlas component — it has no Atlas dependencies and
reaches Atlas exclusively by shelling out to `workspace run atlas-query` in a
checkout of `tandemstream/core`.

The package was introduced in the Phase 2 packet
`2026-05-13-atlas-shadow-phase2-v1` (planning + implementation in
`tandemstream/core` at
`products/tandem/packages/python/atlas/docs/work/2026-05-13-atlas-shadow-phase2-v1/`).

## Scope and modes

Default mode (`make shadow-run`) reads `continuous_shadow_org_id` from
`shadow-config.yaml` and queries Atlas under that org. There is **no
ingestion on the critical path** — the configured org's index may be stale,
and that is an honest signal the grader records (a `partial_match` or
`atlas_not_found` outcome). Continuous ingest of `tandemstream/core`'s main
into the shadow org is owned by a separate packet (`2026-05-13-atlas-shadow-
continuous-ingest-v1`) and is explicitly *out of scope* here.

Out-of-band mode (`make shadow-run COMMIT=<sha>`) invokes `atlas_shadow.ingest`
to spin up a fresh Atlas org, ingest source at the target commit, and route
queries against that new org_id. This mode exists so a packet author can
benchmark Atlas at a historical commit without waiting for the continuous-
ingest stream to catch up. The ingest implementation mirrors
`scripts/dogfood_v2_smoketest_ingest_code.py` in the Atlas leaf — the
authoritative source-of-truth for what an SCIP-based commit ingest looks
like.

## Install

```
make setup
```

Creates `.venv/`, installs `requirements.txt` (`anthropic`, `pyyaml`, `click`).
No Atlas deps.

For Atlas spawn-out (`make shadow-run`), you ALSO need a working `tandemstream/
core` checkout with the Atlas venv bootstrapped (`workspace up` from the Atlas
leaf), and the `workspace` CLI on `$PATH`. Atlas-shadow shells out to:

```
workspace run atlas-query -- \
    --question "<text>" \
    --org-id <uuid> \
    --tool <answer|find_code|scan_search|auto> \
    --output-format json
```

The trailing-args forwarding (`--`) requires the workspace CLI changes shipped
in `tandemstream/core` commit `51a13fa` (Phase 2.A).

Set `ANTHROPIC_API_KEY` for grader calls. The grader model is named in
`shadow-config.yaml`.

## Usage

```bash
# Default run against the configured continuous-shadow org
make shadow-run FIXTURE=dogfood-v2-questions

# Out-of-band ingest mode (creates a fresh org at <sha>, queries against it)
make shadow-run FIXTURE=dogfood-v2-questions COMMIT=87aa9fa

# Inspect grade distribution
jq -r '.grader_response.grade' shadow-runs/dogfood-v2-questions/atlas-qa-shadow.jsonl | sort | uniq -c

# Cross-packet aggregate
make shadow-aggregate

# Recovery: find/delete leaked atlas_shadow_* orgs (--dry-run is recommended first)
make purge-orphans DRY_RUN=1
make purge-orphans
```

Output paths:

- `shadow-runs/<fixture-id>/atlas-qa-shadow.jsonl` — one record per question
  (graded by the LLM grader).
- `shadow-runs/_aggregate/comparison-report.md` — cross-packet summary.

### Out-of-band ingest: rollback + recovery contract

`make shadow-run COMMIT=<sha>` creates a fresh Atlas org and runs the
dogfood ingest script against it. The caller is responsible for
pre-staging the SCIP blob at `/tmp/dogfood-v2-<sha>.scip` and the
source checkout at `/tmp/dogfood-v2-playground-<sha>` (or pass explicit
paths — see `atlas_shadow.ingest.ensure_org_for_commit`).

**Rollback (automatic):** if the ingest script fails (missing SCIP,
bad source root, DB connection drop, etc.) AND the org was created
fresh by this invocation (not via `template_org_id`), the runner
attempts to delete that org before re-raising the ingest exception.
The rollback is gated by a pristine-check inside `delete_org` — if
the org already has rows in any of `code_revisions`, `code_chunk_refs`,
`code_symbols`, `instructions`, `policy_entries`, or `artifacts`, the
rollback declines and emits a stderr warning instead. The org is left
for manual review (these surfaces mean the ingest got partway through
and the data may matter).

**Recovery (manual):** `make purge-orphans` lists every
`atlas_shadow_*`-prefixed org in Atlas's DB that's NOT in the local
`.ingest-cache.json` (i.e., from crashes or kill signals that escaped
the auto-rollback). With `DRY_RUN=1` it only reports; without, it
deletes (subject to the same pristine-check). `--include-cached`
also considers tracked orgs — use this only when the cache itself is
known to be stale.

## Output schema

Each line in `atlas-qa-shadow.jsonl` is a JSON object:

```json
{
  "question_id": "Q01",
  "question": "...",
  "fixture_id": "dogfood-v2-questions",
  "atlas_response": { "tool_used": "find_code", "answer_text": "...", "metrics": {"atlas_latency_ms": 421} },
  "grader_response": {
    "grade": "full_match | partial_match | no_match | atlas_not_found",
    "confidence": 0.0,
    "rationale": "..."
  },
  "captured_at": "2026-05-13T20:30:00+00:00"
}
```

## Repo layout

```
atlas_shadow/        # python package
  __init__.py
  parser.py          # reads 02-qna-log.md receipts + dogfood-v2 JSONL fixture
  runner.py          # shells out to `workspace run atlas-query`
  grader.py          # LLM-as-judge via the Anthropic SDK
  aggregate.py       # cross-packet metrics writer
  ingest.py          # out-of-band one-off ingest (D4)
  cli.py             # entry points for make targets
tests/
  test_parser.py
  test_runner.py
  test_grader.py
  test_aggregate.py
  test_ingest.py
  fixtures/
    dogfood-v2-questions.jsonl   # 22 sanitized oracle entries
shadow-config.yaml   # names continuous_shadow_org_id, one_off_template_org_id, grader_model
shadow-runs/         # output; per-fixture subdirs (gitignored except .gitkeep)
Makefile             # setup / shadow-run / shadow-grade / shadow-aggregate / clean
requirements.txt
LICENSE
```

## License

MIT. See `LICENSE`.
