# CLAUDE.md — atlas-shadow

## What this repo is

`tandemstream/atlas-shadow` is the **offline benchmark runner** for Atlas. It
shells out to a sibling `tandemstream/core` checkout via
`workspace run atlas-query` and grades the returned answers with an LLM
judge. It is *not* an Atlas component.

## What this repo is NOT

- Not Atlas. No Atlas Python imports, no Postgres dependency, no SCIP code.
- Not `tandemstream/core`. The packet directory, receipts, the workspace
  CLI, and the atlas-query wrapper all live in `tandemstream/core` at
  `products/tandem/packages/python/atlas/`.
- Not the continuous-ingest workstream. That's a separate packet
  (`2026-05-13-atlas-shadow-continuous-ingest-v1`) which maintains
  `continuous_shadow_org_id`'s index. Atlas-shadow consumes whatever org
  state is current; it does not push to it.

## Where related docs live

The Phase 2 planning packet is in `tandemstream/core` at:

```
products/tandem/packages/python/atlas/docs/work/2026-05-13-atlas-shadow-phase2-v1/
  00-prompt.md
  01-proposal.md
  02-plan.md
  02-qna-log.md
  03-review-log.md
  04-postmortem.md
```

Read those for architecture, design rationale, reviewer rounds, and
post-merge findings.

## How to invoke Atlas from here

Atlas-shadow assumes a working `tandemstream/core` checkout at a path
configurable via `shadow-config.yaml`'s `core_repo_path` (default:
`/Users/ray/tandemstream/core--atlas`). It shells out via:

```
workspace run atlas-query -- \
    --question "<text>" \
    --org-id <uuid> \
    --tool <answer|find_code|scan_search|auto> \
    --output-format json
```

`--org-id` is REQUIRED in the wrapper — atlas-shadow's `shadow-config.yaml`
names the org explicitly. Never default an org id in this repo's Python
code.

## Hard rules

- Do not import Atlas modules. All Atlas access is via subprocess.
- Do not hardcode org ids in Python code. Resolve from `shadow-config.yaml`.
- The grader is an LLM-as-judge that must NEVER see ground-truth Atlas-
  internal state. It compares the wrapper's `answer_text` to the fixture's
  oracle excerpt and emits one of `full_match / partial_match / no_match /
  atlas_not_found` plus confidence + rationale.
- Output goes to `shadow-runs/<fixture-id>/atlas-qa-shadow.jsonl`.

## Test convention

Each module has a fixture-based test in `tests/`. Atlas spawn-out is stubbed
in unit tests via subprocess monkeypatching; end-to-end smoketests require a
live core checkout and the Atlas venv.

## Phase 2 reference (D4 ingest)

The out-of-band ingest (`atlas_shadow/ingest.py`) mirrors
`scripts/dogfood_v2_smoketest_ingest_code.py` in the Atlas leaf — that
script is the authoritative reference for org-resolve-or-create + SCIP
ingest + chunking. Atlas-shadow shells out to that script (or a thin core-
side bridge with the same kwargs); it does not reimplement the ingest
pipeline.
