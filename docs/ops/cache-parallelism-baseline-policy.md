# Ops note — when to use cache + parallelism on grade-packet-batch

**Status:** baseline policy as of 2026-05-20, post PR #31 (atlas-query cache) + PR #32 (receipt-grading parallelism).
**Evidence:** probe-twice validation comparing cold-cache and warm-cache parallel batches (cold→warm = 2.03× wall-time speedup, 511/511 deterministic-field comparisons identical, 4 explained row-level diffs across 73 receipts). Probe artifacts live under `shadow-runs/_probe/` (gitignored) on the validating operator's checkout; headline numbers + per-row diff explanation are in the originating PR comments.

---

## TL;DR

| Workload | Cache | `--max-workers` |
|---|---|---|
| **Official accuracy baseline** | enabled (allowed, but does not affect verdicts) | **`1` (serial)** |
| Speed-validation / smoketest probe | enabled | 4 (or higher; see below) |
| Live PR-grading webhook gate | n/a — cache is **batch-only** by design | 1 (default; live path passes no `max_workers`) |

The defaults in `shadow-config.yaml` keep both knobs at 1 / off — opt-in everywhere.

---

## Three rules

### 1. Cache + parallelism are safe for speed validation.

The probe-twice run (5 packets, 73 receipts, anchor `4f157927`) showed:

- Zero grading errors under `--max-workers 4`.
- 511/511 deterministic-field comparisons identical across cold and warm parallel runs (the 4 row-level diffs that did occur are fully explained by grader-LLM variance + one atlas-query timeout that succeeded on retry — none traceable to cache or threading).
- 2.03× wall-time speedup warm vs cold on the local `claude` CLI grader path; the cache portion is bounded by the **atlas-query** cost, so SDK-grader environments will see a larger ratio.

If you only need to know *how fast* a packet set runs, use cache + parallelism freely.

### 2. Official accuracy baselines stay **`--max-workers 1`**.

Two reasons:

- **Reproducibility under operator review.** Codex / Claude / Ray comparing baseline-to-baseline want deterministic row order in artifacts; the parallel path reassembles by source-order index but log lines + grader API call timings interleave, which makes side-by-side diffs harder than they need to be.
- **Rate-limit headroom.** Each worker holds one Anthropic-grader request in flight. Canonical baselines run hundreds of packets — serial mode keeps the request rate predictable so a slow grader response can't compound into a 429 wave.

The cache CAN be enabled for accuracy baselines (it doesn't change verdicts when entries hit), but it isn't required. If you re-run against the same SHA twice and want a clean control, truncate first:

```sh
make truncate-cache
```

### 3. Failed atlas-query calls must remain **uncached**.

PR #31 gates `cache.set` on `returncode == 0 and not exception`. The probe-twice run confirmed the gate works as designed:

- 22 atlas-query calls in the cold batch → 19 entries stored → 3 timeouts (rc=-1, `TimeoutExpired`) correctly skipped.
- Warm batch re-issued those 3 → 1 succeeded with content, 2 timed out again. **This is the behavior we want.** Caching the timeouts would silently bake the failure into every subsequent run.

**If you change `runner.run_one`'s cache-store rule, re-run the probe-twice harness and update FINDINGS.md.** The rule is invariant for the policy above to hold.

---

## How operators dial each knob

| Mechanism | Cache | Workers |
|---|---|---|
| `shadow-config.yaml` (`ingest_daemon:` block) | `query_cache_enabled: false` | `grading_max_workers: N` |
| Process env (winners over YAML) | `ATLAS_SHADOW_QUERY_CACHE=off` | `ATLAS_SHADOW_GRADING_MAX_WORKERS=N` |
| `grade-packet-batch` CLI flag (winners over env) | n/a (cache is on by default in batch) | `--max-workers N` |

Live PR-grading webhooks construct **no** cache and pass **no** `max_workers`, so live verdicts never see batch-only optimizations regardless of how YAML is configured (matches the design intent of both PRs).

---

## Cross-links

- PR #31 — atlas-query result cache (commit `6a9af44`)
- PR #32 — receipt-grading parallelism via ThreadPoolExecutor (commit `d1c9dff`)
- Daemon runbook — [`docs/shadow-mode-runbook.md`](../shadow-mode-runbook.md) (link this note from the "Run a baseline" section when next edited)
- Validation evidence: probe-twice findings + per-row diff live under `shadow-runs/_probe/` on the validating operator's local checkout (`shadow-runs/` is gitignored per repo policy). See the PR that introduces this note for the captured headline numbers.
