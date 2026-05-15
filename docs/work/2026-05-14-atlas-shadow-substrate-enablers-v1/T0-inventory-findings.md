# T0 — Pre-implementation inventory findings

**Packet:** `2026-05-14-atlas-shadow-substrate-enablers-v1` (P1)
**Gate:** `02-plan.md` §3a (Pre-implementation inventory gate — atlas-shadow state verification)
**Owner:** Implementer (Claude impl session)
**Run at:** 2026-05-15

---

## Repo state inspected

- **Repo:** `tandemstream/atlas-shadow`
- **HEAD commit:** `4cb928718447993f7f163f1e1c3c808e6d0b5ce2`
- **HEAD subject:** `[shadow] daemon: pass --commit-sha + --repo-url through to dogfood ingest (v2 follow-on) (#4)`
- **Tree state:** clean (no tracked modifications; impl branch `ray/atlas-shadow-substrate-enablers-v1-impl` branched from `origin/main` at this SHA)
- **Worktree path:** `/Users/ray/tandemstream/atlas-shadow--p1-impl`

This is the SHA against which all T2/T3 atlas-shadow edits are made. If the upstream atlas-shadow `main` advances before S2 merges, S2 PR rebases against the new tip; the verification below is re-runnable.

---

## File-by-file verification

### `atlas_shadow/ingest_daemon/worker.py` — for T2 (state-file read) + T3 (SCIP janitor)

**Plan assumption (`02-plan.md` §3a, item 2):** `process_one` exists; has a successful-ingest branch after which a try/except SCIP-delete block can be appended; `scip_path` is a local variable in scope at the success-branch site.

**Verified.**

- `process_one` is defined at `worker.py:46` with signature `process_one(cfg, claim, *, _build_scip, _run_ingest, _ensure_clone, _checkout_worktree, _write_state) -> dict[str, Any]`. The five `Callable` keyword-only injection points are exactly the seams T2/T3 tests will use.
- `scip_path` is bound at `worker.py:92–98` (`scip_path = _build_scip(...)`) and remains in local scope through the success path (used at `worker.py:155` for ledger row recording).
- The success branch begins at `worker.py:135` after `_run_ingest` returns. The ledger row is written at `worker.py:148–161`; state-file write at `worker.py:163–180`; queue row marked succeeded at `worker.py:183`; return at `worker.py:185–191`. The natural site for SCIP-blob delete-on-success is **between the queue-mark and the return** (i.e., after step 4c) so that all durable writes are committed before cleanup runs — matching the plan's "after step 4a ledger row written" intent (the plan allows anywhere in the success branch; later is safer).
- The failure branch at `worker.py:109–133` does NOT touch `scip_path.unlink`, so retain-on-failure (D-P1-4) is naturally satisfied as long as the janitor lands inside the success branch (post-line-184) and not in the `try`/`except` body.
- The plan's state-file-read site for T2 is **before** `_run_ingest` is called (so the parent UUID can be threaded into the ingest argv). The natural site is between `worker.py:98` (`scip_path` bound) and `worker.py:100` (`_run_ingest` called). The `_run_ingest` call will need a new `parent_code_revision_id` kwarg that defaults to `None` for cold-start; the wiring then forwards through to `scip_builder.dogfood_ingest_argv`.

**No divergence.**

### `atlas_shadow/ingest_daemon/scip_builder.py` — for T2 (argv shape)

**Plan assumption (`02-plan.md` §3a, item 3):** `dogfood_ingest_argv` builds a list of strings passed to the dogfood smoketest subprocess; there is an extension point for additional `--flag value` pairs.

**Verified.**

- `dogfood_ingest_argv` is defined at `scip_builder.py:129` with signature `dogfood_ingest_argv(*, core_repo_path, org_id, scip_path, source_root, commit_sha, repo_url) -> list[str]`. The function returns a single flat `list[str]` built from `[venv_py, "-m", "scripts.dogfood_v2_smoketest_ingest_code", ...flag/value pairs]`.
- The existing argv already includes `--commit-sha` and `--repo-url` (v2 follow-on, core PR #209). Adding two more keyword-only params (`parent_code_revision_id: str | None = None`, defaulting to None so cold-start callers don't supply it) is the natural extension point; when non-None, two strings (`"--parent-code-revision-id"`, `parent_code_revision_id`) and one string (`"--incremental"`) are appended.
- `run_dogfood_ingest` (scip_builder.py:185) is the subprocess wrapper that calls `dogfood_ingest_argv`; it forwards every named kwarg straight through. T2 adds `parent_code_revision_id` to both function signatures and forwards it through.
- The argv shape is also asserted by `tests/ingest_daemon/test_worker.py::test_argv_matches_dogfood_argparse` (amendment decision #10 from D5). T2 must update that argv-parity test (or add a complementary one) to keep it asserting bidirectional parity against the (now expanded) dogfood argparse declaration.

**No divergence.**

### `atlas_shadow/ingest_daemon/state_file.py` — for T2 (state read)

**Plan assumption (`02-plan.md` §3a, item 4):** state file is JSON-serialized with a `latest_code_revision_id` UUID-string field; `write_state` (or equivalent) writes on success; `read_state` (or equivalent) returns `None` on cold start.

**Verified.**

- `write_state(*, state_file_path, latest_commit_ingested, latest_code_revision_id, daemon_pid=None) -> None` at `state_file.py:36`. Payload schema at lines 51–56 explicitly carries `"latest_code_revision_id": latest_code_revision_id` (UUID-as-string per the docstring at lines 17–18). Atomic-rename via `tempfile.mkstemp` + `os.fsync` + `os.replace` at lines 57–68 (amendment decision #12).
- `read_state(state_file_path) -> Optional[dict[str, Any]]` at `state_file.py:78`. Returns `None` when file is absent (line 86–87) and `None` on `OSError | json.JSONDecodeError` (line 91–92). Cold-start semantics match the plan exactly.
- Field name `latest_code_revision_id` matches what T2 will rely on. No rename required.

**No divergence.**

---

## Summary

**No divergence observed across worker.py, scip_builder.py, or state_file.py.** All three planning assumptions hold against `atlas-shadow @ 4cb928718447993f7f163f1e1c3c808e6d0b5ce2`. The implementer proceeds with T2/T3 work as planned in `02-plan.md` §4.3 and §4.4 (proposal-side; renumbered T2/T3 post-amendment).

**Insertion-point notes for the implementer (mechanical, no plan change):**
- T2 state read site: between `worker.py:98` and `worker.py:100`, immediately before `_run_ingest` is called.
- T2 scip_builder change: add `parent_code_revision_id: str | None = None` kwarg to both `dogfood_ingest_argv` and `run_dogfood_ingest`; append `--incremental --parent-code-revision-id <uuid>` to the argv when non-None.
- T3 SCIP janitor site: between `worker.py:183` (queue mark succeeded) and `worker.py:185` (success return) — after all durable writes, before the success return. Failure branch at `worker.py:109–133` is untouched, naturally satisfying retain-on-failure.

This T0 record is committed to the impl branch before any code edits to `worker.py`, `scip_builder.py`, or `state_file.py` (or any test file) per the gate's audit-trail requirement.
