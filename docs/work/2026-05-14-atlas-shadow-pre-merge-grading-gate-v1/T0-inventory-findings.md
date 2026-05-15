# T0 inventory gate — atlas-shadow pre-merge grading gate (P2 v1)

**Packet:** `2026-05-14-atlas-shadow-pre-merge-grading-gate-v1` (planning packet on
`ray/atlas-shadow-pre-merge-grading-gate-v1` in `tandemstream/core` at `b1e4e44`).

**Run date:** 2026-05-15.

**Atlas-shadow HEAD at gate run:** `27024ce` (origin/main of `tandemstream/atlas-shadow`,
P1 impl: T0 inventory + T2 incremental wiring + T3 SCIP janitor + AG3/AG4 tests).

**Branch:** `ray/atlas-shadow-pre-merge-grading-gate-v1-impl` (worktree at
`/Users/ray/tandemstream/atlas-shadow--p2-impl`).

**Verdict:** **No divergence observed.** All 10 verification items confirmed; proceed to T1.

---

## Per-item findings

### 1. Atlas-shadow HEAD commit SHA

`27024ce` — P1 implementation PR #6, merged 2026-05-15. Working tree clean on
freshly-created `ray/atlas-shadow-pre-merge-grading-gate-v1-impl` tracking
`origin/main`.

### 2. P1 substrate presence on core main (q10/q11/q12 cross-checks)

Verified against the packet worktree's read of core source files (the packet branch
was rebased after P1 merged, so its source tree mirrors `origin/main @ 5f2cf87`).

- **(2a) `core/models/artifacts.py:130-167` — `chunk_artifact` writes
  `metadata.chunk_headings` JSONB UPDATE for `.md`/`.markdown` artifacts.** Confirmed.
  Lines 130-143 invoke `MarkdownChunker(...).chunk_with_metadata(raw_text)` when
  `artifact_path.lower().endswith((".md", ".markdown"))` and build a
  `chunk_headings: {str(idx): meta}` dict keyed on chunk index. Lines 158-166 emit
  `UPDATE artifacts SET metadata = coalesce(metadata, '{}'::jsonb) || %s::jsonb`
  with `{"chunk_headings": chunk_headings}` JSON payload, scoped by `org_id` +
  `artifact_id`. Matches receipt q10 exactly.
- **(2b) `core/ingest/chunkers/markdown_chunker.py:13-43` —
  `MarkdownChunker.chunk_with_metadata` returns
  `(chunks: list[str], metadata: list[{heading_path, heading_level, ...}])`.**
  Confirmed. Class declaration at line 13; method `chunk_with_metadata` at lines 18-40.
  Returns the documented tuple shape. Matches receipt q11 exactly.
- **(2c) `schema_v0.2.sql:243-258` — `artifact_chunks` has `start_offset INT`
  and `end_offset INT`.** Confirmed. Lines 252-253 declare
  `start_offset INT,` and `end_offset INT,` on the `artifact_chunks` table.
  Matches receipt q12 exactly.

Substrate is present and matches the planning packet's expectations. No regression
on core main.

### 3. `atlas_shadow/ingest_daemon/receiver.py`

`create_app(cfg)` at line 71 returns a `FastAPI` app instance. `POST /webhook`
handler at line 84 calls `_verify_hmac(...)` (line 89) before processing payload.
**No PR-event handler exists yet** — current handler only branches on
`refs/heads/main` via `_extract_commit_for_main_push`. Matches plan §3a item 3.

### 4. `atlas_shadow/ingest_daemon/worker.py`

`process_one(cfg, claim, *, _build_scip, _run_ingest, _ensure_clone,
_checkout_worktree, _write_state, _read_state)` at line 46. Reads state-file
`latest_code_revision_id` (lines 109-112) and threads
`parent_code_revision_id=...` into `_run_ingest` (line 120). SCIP-blob janitor
at line 208 deletes `scip_path` on the success branch with `missing_ok=True`.
Both P1 outputs (T2 incremental wiring + T3 SCIP janitor) are present. Matches
plan §3a item 4.

### 5. `atlas_shadow/ingest_daemon/state_file.py`

`write_state(*, state_file_path, latest_commit_ingested, latest_code_revision_id,
daemon_pid=None)` at line 36. `read_state(state_file_path)` at line 78. State
schema (lines 51-56) includes `latest_commit_ingested`, `latest_code_revision_id`,
`updated_at`, and `daemon_pid` keys. Matches plan §3a item 5.

### 6. `atlas_shadow/ingest_daemon/ledger.py`

`latest_succeeded(db_path)` at line 81; returns a dict whose `_row_to_dict`
serialization (line 125) includes `code_revision_id` (from the
`ingest_ledger.code_revision_id` column, declared in `schema.sql`). **No
`find_by_commit_sha` exists today.** `get_by_commit_sha(db_path, commit_sha)`
at line 111 exists but returns a `list[dict]` — T6 adds a new
`find_by_commit_sha` helper that returns a single dict (most recent succeeded
attempt) or `None`. Matches plan §3a item 6.

### 7. `atlas_shadow/grader.py`

`grade(*, question, oracle_excerpt, oracle_claim, atlas_answer_text, model,
api_key=None, max_tokens=512, _client=None)` at line 218. Returns
`GraderResponse(grade, confidence, rationale, latency_ms, raw)` at line 33-39.
`ATLAS_SHADOW_GRADER_STUB=1` env-var short-circuit at line 238. Rubric in
`SYSTEM_PROMPT` at line 42. Matches plan §3a item 7 + Spike A.

### 8. `atlas_shadow/parser.py`

`parse_qna_log_markdown(path)` at line 101 returns `list[Receipt]`. `Receipt`
dataclass at line 26 has `question_id`, `question`, `oracle_excerpt`,
`oracle_claim`, `source_path`, `source_lines`, `commit_sha`, `class_label`, and
`extra`. Permissive shape (skips malformed blocks). Matches plan §3a item 8.

### 9. `atlas_shadow/runner.py`

`run_batch(receipts, *, fixture_id, org_id, core_repo_path, tool='auto',
principal_id=None, domain_pack=None, code_revision_id=None, timeout=180,
progress_cb=None, _invoke=invoke_atlas_query)` at line 321. `code_revision_id`
is plumbed through `build_atlas_query_argv` (line 184) and emitted as
`--code-revision-id <uuid>` to `workspace run atlas-query`. Matches plan §3a
item 9 + Spike B.

### 10. Atlas-DB connection config discovery (T4a setup)

**Existing pattern:**

- `atlas_shadow/ingest.py:316-319` uses
  `os.environ.get("ATLAS_ADMIN_DB_URL") or os.environ.get("ATLAS_DB_URL")
  or "postgresql://atlas:atlas_dev@localhost:5432/atlas"` as the connection
  string. Same pattern repeated at `ingest.py:406-409` for the orphan-org
  listing script.
- DB driver: `psycopg2` (NOT `psycopg`/psycopg3). Imported at
  `ingest.py:314` and `ingest.py:404`.
- **Current invocation pattern:** the DB scripts shell out to the Atlas venv's
  Python interpreter (path resolved via `_atlas_leaf(core_repo_path) /
  ".venv/bin/python"`) because **atlas-shadow's own `requirements.txt` does
  NOT declare `psycopg2`** (today's deps: `anthropic>=0.40.0`, `pyyaml>=6.0`,
  `click>=8.1`, `fastapi>=0.110`, `uvicorn>=0.27`).

**T4a will:**

1. Add `psycopg2-binary>=2.9` to `atlas-shadow/requirements.txt` so the
   new `doc_resolver.py` module can import psycopg2 in-process (the plan's
   amendment-3 direct-psycopg contract is explicit: `imports psycopg directly.
   NO core.* imports.`). Using `psycopg2-binary` (not `psycopg2`) avoids
   requiring local libpq build deps and matches what most consumer libraries
   do; matches the import name (`import psycopg2`) of the existing atlas-shadow
   scripts so the convention stays uniform.
2. Read the connection string from env-var chain
   `ATLAS_SHADOW_DOC_RESOLVER_DB_URL` (T4a-specific override) →
   `ATLAS_ADMIN_DB_URL` → `ATLAS_DB_URL` → no default (raise if all absent —
   credentials are NEVER inlined per plan).
3. Set `connect_timeout=5` and
   `options='-c statement_timeout=<ms>'` where the ms value comes from env
   `ATLAS_SHADOW_DOC_RESOLVER_QUERY_TIMEOUT_MS` (default `10000`).
4. Scope every query by `org_id` as the first WHERE-clause predicate; no
   defaulting. Module raises if `org_id` is None.

The decision to use `psycopg2` (matching existing atlas-shadow ingest scripts)
rather than `psycopg` (psycopg3) is a faithful interpretation of the plan's
amendment-3: the planning text uses "psycopg" generically (could mean either
library); the existing atlas-shadow convention is `psycopg2`. Keeping the
convention is the minimum-friction read of the plan.

---

## No-divergence summary

All 10 verification items confirmed against the planning packet's assumptions:

1. HEAD SHA recorded.
2. P1 substrate (q10/q11/q12 cross-checks) present on core main.
3. `receiver.py` has `create_app` + HMAC + push handler; no PR-event handler.
4. `worker.py` has post-P1 state-file read + parent thread + SCIP janitor.
5. `state_file.py` exposes `read_state` / `write_state` with
   `latest_code_revision_id` schema field.
6. `ledger.py` exposes `latest_succeeded` returning a dict with
   `code_revision_id`; `find_by_commit_sha` is absent (T6 adds it).
7. `grader.py` exposes `grade(...)` with the documented signature + stub mode.
8. `parser.py` exposes `parse_qna_log_markdown(path)`.
9. `runner.py` exposes `run_batch(..., code_revision_id=None, ...)`.
10. DB connection state lives in `ATLAS_ADMIN_DB_URL` / `ATLAS_DB_URL` env vars;
    existing atlas-shadow scripts use psycopg2. T4a follows the same convention.

**Proceeding to T1.**
