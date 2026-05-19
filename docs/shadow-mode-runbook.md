# Shadow-mode operator runbook

Captures the bring-up + ongoing-operation sequence for running atlas-shadow
as a continuous-grading observer of `tandemstream/core`. Based on the
2026-05-15 bring-up session that established the first baseline against
the `shadow_main` org.

This complements (does not replace):

- [`docs/ingest-daemon.md`](ingest-daemon.md) — daemon internals.
- [`docs/pre-merge-grading-gate.md`](pre-merge-grading-gate.md) — P2 live-PR-gate details.

---

## What "shadow mode" means

A dedicated atlas org (`shadow_main` UUID `36a5bde2-9de3-4278-b18c-1e2da1916034`)
that is **kept continuously current with `tandemstream/core@main` via
webhook-driven ingest**, and is graded periodically via batch runs against
every packet's `02-qna-log.md`.

Two surfaces consume the org:

1. **Live PR-grading gate** (P2 v1, see `pre-merge-grading-gate.md`):
   PRs touching `02-qna-log.md` fire a webhook → daemon → orchestrator →
   atlas queries → commit-status posted back to GitHub.
2. **Offline batch grading** (P3, this packet): a CLI walks every packet's
   qna log, runs the same orchestrator with GH calls stubbed, writes
   per-packet JSON + per-run summary + a cross-run dashboard at
   `shadow-runs/overall-summary.md`.

The shared orchestrator (`grader_service.run_pr_grading`) ensures grading
semantics are identical across the two surfaces — only inputs (live webhook
vs. synthetic per-packet PrEvent) and outputs (GH status vs. filesystem
JSON) differ.

---

## Operational state

After Phase 0–3 bring-up, these things exist on the operator's laptop:

| Component | Where | Owner |
|---|---|---|
| Shadow org row | atlas DB (in `orgs` table) | `python -m atlas_orgs create shadow_main` produced it. |
| Code corpus (SCIP) | atlas DB (`code_revisions`, `code_symbols`, etc.) | Daemon's worker via webhook + `make ingest-replay`. |
| Doc corpus (artifacts) | atlas DB (`artifacts`, `artifact_chunks`) | One-shot `scripts/shadow_ingest_docs.py` invocation. **Currently not auto-rerun on push** — see "Known gaps". |
| Daemon SQLite ledger | `~/.atlas-shadow/ingest.db` | Daemon's `bootstrap` + `serve`. |
| Daemon state file | `~/tandemstream/atlas-shadow/.daemon-state.json` | Daemon writes after each successful ingest. |
| Cache (worktrees + SCIP blobs) | `~/.atlas-shadow/cache/` | Daemon manages. |
| Webhook secret | `~/.atlas-shadow/webhook.secret` | Operator generates once via `openssl rand -hex 32`. |
| Webhook config on GH | `https://github.com/tandemstream/core/settings/hooks` | Created by `gh webhook forward` on first run. |
| Cross-run dashboard | `shadow-runs/overall-summary.md` | Regenerated after every batch. |

---

## Required env vars

The daemon, the batch CLI, and the doc-ingest script all read from the
process environment. Put these in your shell before starting any
component:

```bash
# Atlas DB DSNs (from atlas's embedded-PG runtime.env):
set -a
source "$HOME/Library/Application Support/TandemStream/Atlas/development/runtime.env"
set +a
# Provides: ATLAS_DB_URL, ATLAS_ADMIN_DB_URL

# Atlas API keys (from the core atlas leaf's .env):
set -a
source "/Users/ray/tandemstream/core--shadow-runtime/products/tandem/packages/python/atlas/.env"
set +a
# Provides: OPENAI_API_KEY, ANTHROPIC_API_KEY

# Webhook HMAC (stable across daemon restarts — generate ONCE):
export GITHUB_WEBHOOK_SECRET="$(cat ~/.atlas-shadow/webhook.secret)"

# GitHub token (for grader's read-side: PR files + commit statuses):
export GITHUB_ATLAS_SHADOW_TOKEN="$(gh auth token)"

# Grader backend (claude_cli uses your Claude Code subscription instead
# of paying ANTHROPIC API per-call):
export ATLAS_SHADOW_GRADER_BACKEND=claude_cli

# Workspace launcher — REQUIRED for runner subprocess to find workspace.py
# (since `workspace` is a zsh shell function, not a binary on PATH).
# Use the workspace.py from the SAME checkout the daemon is grading
# against — older workspace.py versions lack argparse.REMAINDER and
# silently mangle args after `--`.
export WORKSPACE_PY=/Users/ray/tandemstream/core--shadow-runtime/tools/workspace/workspace.py
export WORKSPACE_VENV_PY=/Users/ray/.config/workspace/venv/bin/python
```

---

## First-time bring-up

### Phase 0 — Org + worktree + config

```bash
# 1. Create the shadow org. Capture the UUID printed.
cd /Users/ray/tandemstream/core--shadow-runtime/products/tandem/packages/python/atlas
.venv/bin/python -m atlas_orgs create shadow_main
# -> 36a5bde2-9de3-4278-b18c-1e2da1916034   (or whatever UUID it prints)

# 2. Create a dedicated worktree pinned to origin/main. Daemon's worker
#    uses git fetch + per-commit worktrees under ~/.atlas-shadow/cache,
#    but the runner reads workspace.yaml + the doc-ingest script reads
#    file content from the checkout we configure here. Use a worktree
#    dedicated to shadow mode so it isn't disturbed by branch-switching
#    on the operator's main checkout.
git -C /Users/ray/tandemstream/core fetch origin
git -C /Users/ray/tandemstream/core worktree add /Users/ray/tandemstream/core--shadow-runtime origin/main

# 3. Bootstrap atlas venv in the new worktree.
cd /Users/ray/tandemstream/core--shadow-runtime/products/tandem/packages/python/atlas
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
# Copy the .env from your canonical checkout (provides OPENAI/ANTHROPIC keys):
cp /Users/ray/tandemstream/core--atlas/products/tandem/packages/python/atlas/.env .env

# 4. Edit atlas-shadow/shadow-config.yaml so:
#     - continuous_shadow_org_id matches the UUID from step 1
#     - core_repo_path points at /Users/ray/tandemstream/core--shadow-runtime
#     - continuous_shadow_code_revision_id: null  (daemon repopulates)

# 5. Generate a stable webhook secret (do once; reuse across daemon restarts).
mkdir -p ~/.atlas-shadow
openssl rand -hex 32 > ~/.atlas-shadow/webhook.secret
chmod 600 ~/.atlas-shadow/webhook.secret
```

### Phase 1a — Code ingest at HEAD

```bash
cd /Users/ray/tandemstream/atlas-shadow
# (Export all env vars from "Required env vars" above first.)

make ingest-bootstrap                       # init SQLite ledger schema
make ingest-up-detached                     # start daemon worker
make ingest-replay COMMIT=$(git -C /Users/ray/tandemstream/core--shadow-runtime rev-parse origin/main)

# Watch the worker drain:
tail -f .ingest-daemon.log

# Verify:
make ingest-status
# Should show: latest_commit_ingested = <origin/main SHA>,
#              latest_code_revision_id = <uuid>
```

### Phase 1b — Doc ingest at HEAD (one-shot)

```bash
cd /Users/ray/tandemstream/core--shadow-runtime/products/tandem/packages/python/atlas
.venv/bin/python -m scripts.shadow_ingest_docs \
    --org-id 36a5bde2-9de3-4278-b18c-1e2da1916034 \
    --commit-sha $(git -C /Users/ray/tandemstream/core--shadow-runtime rev-parse origin/main) \
    --repo-path /Users/ray/tandemstream/core--shadow-runtime \
    --quiet

# Should produce ~500-1500 artifacts, ~5-20k chunks, 11-20 min runtime.
# Final manifest is printed to stdout.
```

### Phase 2 — Webhook + diff ingestion

```bash
# Install the gh-webhook extension (once).
gh extension install cli/gh-webhook

# Start the webhook forwarder. Creates a transient webhook on the repo
# the first time it runs. The Makefile target wraps `gh webhook forward`
# in nohup + pidfile so the forwarder survives the operator's shell
# exit — the bare `gh webhook forward` is a child of the invoking shell
# and dies with it (this was the 2026-05-15 30-commit-gap incident).
make webhook-forward-up-detached

# Confirm forwarder is alive + webhook is registered on GH:
make webhook-forward-status
gh api /repos/tandemstream/core/hooks --jq '.[] | {id, active, events}'
```

### Phase 3 — Establish a baseline

```bash
cd /Users/ray/tandemstream/atlas-shadow
# (Env vars exported.)

.venv/bin/python -m atlas_shadow.ingest_daemon \
    --config shadow-config.yaml \
    grade-packet-batch \
    --core-repo-path /Users/ray/tandemstream/core--shadow-runtime \
    --output-dir shadow-runs/baseline-$(date +%Y-%m-%d)
```

Outputs:

```
shadow-runs/
├── overall-summary.md            ← exec dashboard (rolling, cross-run)
├── overall-summary.json
└── baseline-YYYY-MM-DD/
    ├── manifest.json
    ├── summary.md                ← per-packet table
    ├── packets/<slug>.json       ← per-receipt detail (atlas's response + grade)
    └── artifacts/                ← raw orchestrator output (mostly redundant)
```

#### Two scores: raw vs clean (PR #14)

Every PR #14+ run emits **two** percentages — `overall_pct` (raw, legacy)
and `clean_overall_pct` (PR #14's clean denominator). Both appear in
`manifest.json`, `summary.md`, and the cross-run `overall-summary.md`
dashboard.

| Field | What it counts | Use when |
|---|---|---|
| `overall_pct` | passes / (every row, including stale-receipt skips) | comparing against pre-PR-14 baselines for trend continuity |
| `clean_overall_pct` | passes / (rows where `score_status == "counted"` — excludes receipt-stale anchors and future similar bookkeeping skips) | judging Atlas's actual retrieval performance |

A receipt's `02-qna-log.md` anchor can drift out of sync with the run
commit (the cited file was refactored, deleted, or its lines moved).
PR #13 surfaces that via `source_snapshot_status: "git_source_missing"`
on the row. PR #14 turns the per-row signal into a per-run score: such
no_match rows get `score_status: "skipped_receipt_stale"` +
`clean_excluded_reason: "receipt_stale"`, and the clean denominator
drops them from BOTH numerator and denominator so the score reflects
retrieval performance, not receipt drift.

**Two snapshot fields tell the receipt-vs-grading-commit story:**

- `source_snapshot_status=git_source_hash_match` (PR #13) means **the
  receipt's pinned `source_commit` renders the expected excerpt** —
  the receipt is internally consistent at authoring time.
- `run_snapshot_status=run_commit_hash_match` (PR #15) means **the
  same path/lines still render the expected excerpt at the grading
  `run_commit_sha`** — the file hasn't moved between authoring and
  grading.
- `run_snapshot_status=run_commit_hash_mismatch` (PR #15) means **the
  file was edited between receipt commit and run commit** so the
  cited line numbers now point at different code. When this happens
  on a `no_match` row, the daemon emits
  `score_status=skipped_run_commit_line_drift` +
  `clean_excluded_reason=run_commit_line_drift`, and the clean
  denominator drops the row from BOTH numerator and denominator the
  same way it drops `skipped_receipt_stale`.

| Receipt snap | Run snap | Grade | Score status | Clean denominator |
|---|---|---|---|---|
| `git_source_hash_match` | `run_commit_hash_match` | `full/partial_match` | `counted` | included as PASS |
| `git_source_hash_match` | `run_commit_hash_match` | `no_match` | `counted` | included as FAIL — real Atlas miss |
| `git_source_hash_match` | `run_commit_hash_mismatch` | `no_match` | `skipped_run_commit_line_drift` | **excluded** |
| `git_source_missing` | (any) | `no_match` | `skipped_receipt_stale` | **excluded** |
| `git_source_hash_match` | `run_commit_hash_mismatch` | `full/partial_match` | `counted` | included as PASS |

The per-receipt fields land in `packets/<slug>.json` so the diagnostic
classifier under `scripts/lane_classifier.py` can split a `no_match`
into `run_commit_line_drift` vs. `exact_source_fast_path_didnt_fire`
based on the two snapshot statuses.

Both totals also surface in `manifest.json`
(`total_skipped_receipt_stale` + `total_skipped_run_commit_line_drift`)
and in `summary.md`'s exclusion breakout line.

The `grade` enum stays narrow (`full_match` / `partial_match` /
`no_match` / `atlas_not_found`) — the skip is bookkeeping on a
separate `score_status` field, not a fifth grade value. Downstream
code that branches on grade values continues to see no_match for
stale receipts; the clean filter happens at aggregation time via
`score_status`.

Legacy `baseline-*/manifest.json` files (pre-PR-14) don't carry
`clean_overall_pct`; the cross-run dashboard renders `n/a` for those
runs rather than zero so the trendline doesn't get corrupted.

---

## Ongoing operation

### Daily / weekly tasks

**Re-baseline grading** — Re-run the batch as often as you want a fresh
data point. Each run creates a new `baseline-YYYY-MM-DD/` folder; the
overall summary auto-aggregates. Recommended cadence: weekly or after
big atlas changes.

```bash
.venv/bin/python -m atlas_shadow.ingest_daemon \
    --config shadow-config.yaml \
    grade-packet-batch \
    --core-repo-path /Users/ray/tandemstream/core--shadow-runtime \
    --output-dir shadow-runs/baseline-$(date +%Y-%m-%d)
```

**Refresh doc corpus** — Until the doc-staleness P3 hook lands (see
"Known gaps"), the doc corpus only reflects the SHA at which Phase 1b
was last run. Re-run periodically:

```bash
DAEMON_STATE=/Users/ray/tandemstream/atlas-shadow/.daemon-state.json
LATEST_SHA=$(python3 -c "import json; print(json.load(open('$DAEMON_STATE'))['latest_commit_ingested'])")

cd /Users/ray/tandemstream/core--shadow-runtime/products/tandem/packages/python/atlas
.venv/bin/python -m scripts.shadow_ingest_docs \
    --org-id 36a5bde2-9de3-4278-b18c-1e2da1916034 \
    --commit-sha "$LATEST_SHA" \
    --repo-path /Users/ray/tandemstream/core--shadow-runtime \
    --quiet
```

### Per-event tasks

**Verify the daemon is healthy** —

```bash
ps -p $(cat /Users/ray/tandemstream/atlas-shadow/.ingest-daemon.pid) -o pid,etime,cmd
make ingest-status
# `latest_commit_ingested` should be at or close to origin/main HEAD.
# A gap of 0-2 commits is normal (webhook in flight).
```

**Verify the forwarder is alive** —

```bash
make webhook-forward-status
gh api /repos/tandemstream/core/hooks --jq '.[] | {id, active, last_response: .last_response}'
# last_response.status should be "active".
```

If the forwarder died, the daemon's reconciler thread (see "Reconciler
safety net" below) will still catch up the corpus to remote HEAD on the
next tick (default every 300s) — but PR-grading events will be missed
until the forwarder is back. Restart with:

```bash
make webhook-forward-down   # cleans stale pidfile, if any
make webhook-forward-up-detached
```

> **Secret hygiene**: ``gh webhook forward`` only accepts the HMAC
> secret via its ``--secret`` flag (no env/stdin support upstream), so
> the secret is visible to anyone with ``ps`` access on this machine.
> ``make webhook-forward-status`` deliberately uses ``ps -o comm``
> (binary name only) to avoid surfacing it in operator-facing output —
> but the broader exposure stays until upstream supports env-var or
> stdin secret input. Don't run shadow-mode as a different user than
> the one whose ``ps`` output you trust.

**Restart after a reboot** — the daemon + forwarder do NOT survive
reboot (no launchd plist yet — see "Known gaps"). After reboot:

```bash
# 1. Export all required env vars (see top of this doc).
# 2. Start daemon:
cd /Users/ray/tandemstream/atlas-shadow
make ingest-up-detached

# 3. Start forwarder (nohup + pidfile via Makefile so it survives shell exit):
make webhook-forward-up-detached

# 4. Backfill any commits that landed while down. The reconciler will
#    enqueue current origin/main on its next tick automatically, but
#    if you want every intermediate SHA in the ledger, run replay:
LAST=$(python3 -c "import json; print(json.load(open('.daemon-state.json'))['latest_commit_ingested'])")
make ingest-replay FROM=$LAST
```

---

## Reconciler safety net

The daemon runs a background **reconciler** thread that polls
``git ls-remote <core_repo_url> refs/heads/main`` every
``reconciler_interval_seconds`` (default 300s). When the remote head
SHA differs from ``.daemon-state.json:latest_commit_ingested`` it
enqueues the remote SHA with ``source='reconciler'`` so the worker
catches up — even if the webhook forwarder is dead.

This closes the 2026-05-15 failure mode: ``gh webhook forward`` was run
as a child of an interactive shell, the shell exited, the forwarder
died with it, and the daemon fell 30 commits behind ``origin/main``
before the operator noticed. With the reconciler running the same
incident would self-heal within one tick (≤5 min by default).

Behavior:

- The reconciler enqueues only the *current* remote HEAD, not every
  intermediate SHA — Atlas's ``file_memoization`` carries unchanged
  files forward, so per-SHA history is usually unnecessary. If you
  want every intermediate SHA in the ledger (e.g. for snapshot grading
  at a historical commit), still run ``make ingest-replay FROM=<sha>``.
- ``queue.enqueue`` is idempotent on ``commit_sha``; a reconciler tick
  that races with the webhook short-circuits at ``already-queued`` /
  ``already-running`` / ``dedup-succeeded`` (the outcome is logged to
  stderr so you can audit).
- The reconciler does NOT replace the webhook forwarder. PR-grading
  events (``pull_request`` webhooks) still need the forwarder to fire
  the grading orchestrator. The reconciler keeps the *code corpus*
  current; PR-grade availability still depends on
  ``make webhook-forward-up-detached``.

Tuning knobs in ``shadow-config.yaml``:

```yaml
ingest_daemon:
  reconciler_enabled: true               # set false for replay-only mode
  reconciler_interval_seconds: 300       # poll cadence
  reconciler_ls_remote_timeout_seconds: 60  # per-tick subprocess cap
```

Verifying the reconciler fired:

```bash
grep "reconciler:" .ingest-daemon.log
# Non-"in-sync" outcomes (enqueued, skipped-*, no-remote) get one line each.
```

---

## Modes

### Default: grade at `latest_commit_ingested`

Run batch with no `--commit-sha`. The CLI reads
`.daemon-state.json['latest_commit_ingested']` and grades every packet
against that SHA. Fast, efficient — the right default unless you have a
specific reason to look at history.

### Snapshot: grade everything at a specific historical SHA

Useful for: "what would atlas have said about these receipts if it had
been graded a month ago" or pinning the baseline to a known checkpoint.

```bash
TARGET_SHA="abcdef0123456789..."   # 40-char SHA you want to grade at

# 1. Ensure that SHA is in the daemon's ledger. If `latest_commit_ingested`
#    is past it, you may need to run ingest-replay explicitly:
make ingest-replay COMMIT=$TARGET_SHA
#    Wait for it to drain (check ingest-status).

# 2. Grade at that SHA:
.venv/bin/python -m atlas_shadow.ingest_daemon \
    --config shadow-config.yaml \
    grade-packet-batch \
    --core-repo-path /Users/ray/tandemstream/core--shadow-runtime \
    --commit-sha $TARGET_SHA \
    --output-dir shadow-runs/snapshot-$TARGET_SHA
```

The orchestrator's I2 invariant: if `TARGET_SHA` is not in the ledger,
each packet soft-passes with `status=revision_not_indexed` rather than
silently grading at the wrong revision. Pre-ingest first.

### Per-packet authoring SHA (not yet supported)

Grade each packet against the commit it was authored at (per-packet
respective base SHA, not a single global SHA). Useful for the highest-
precision evaluation but expensive (requires every authoring SHA to be
in the ledger). **Currently a TODO** — see "Known gaps".

---

## Known gaps / TODOs

1. **🔴 Doc-staleness auto-rerun (P3 work)**. Daemon's worker only invokes
   SCIP code ingest on push events; it does NOT auto-rerun
   `shadow_ingest_docs.py`. Doc corpus reflects whatever SHA Phase 1b was
   last run at. Manual rerun above is the workaround.
   - **Fix shape**: surgical PR (~230 LOC), no planning packet needed.
     Modify `worker.py` to invoke `scripts.shadow_ingest_docs` after
     successful SCIP ingest. Atlas's file_memoization makes repeat runs
     cheap (only changed files re-embed). Add `doc_ingest_enabled` +
     `doc_ingest_timeout_seconds` config knobs.
   - **Branch suggestion**: `ray/shadow-doc-ingest-hook`.

2. **🔴 Per-packet authoring-SHA grading**. The batch grades all packets
   against ONE SHA. To grade each packet against its own authoring SHA
   (so atlas isn't asked about symbols that have moved/renamed since the
   packet was written), the batch needs per-packet SHA resolution.
   - **Fix shape**: surgical PR (~100 LOC). Use
     `git log --diff-filter=A -- <packet>/02-qna-log.md` to find each
     packet's authoring commit. Pre-ensure each is in the ledger; thread
     each into its own synthetic PrEvent.
   - **Open question**: How to handle a packet whose authoring SHA is
     not in the ledger (auto-replay vs. soft-skip).
   - **Branch suggestion**: `ray/shadow-per-packet-base-sha`.

3. **🟡 Code-retrieval-lane diagnostic (`atlas-shadow-code-retrieval-lane-v1`)**.
   The 2026-05-15 baseline showed 6.3% with the dominant failure mode
   being `atlas_not_found` on `find_code`/`scan_search` (atlas returns
   `"(no code citations returned)"` even for symbols verifiably present
   in source). This isn't a shadow-mode bug — it's surfacing a real
   atlas-side retrieval issue. Plan exists; out of shadow-mode scope.

4. **🟡 Reboot persistence**. Daemon + forwarder are detached (`PPID=1`)
   so they survive shell exits, but they don't restart on reboot. Wrap
   each in a launchd plist when shadow-mode becomes "always on." (The
   reconciler safety net mitigates a downed *forwarder* but not a
   downed *daemon*; both still need a manual restart after reboot.)

5. **🟢 Stable Cloudflare tunnel** (instead of `gh webhook forward`).
   Required if shadow-mode needs to keep grading while operator's laptop
   is off. Documented in `pre-merge-grading-gate.md`.

---

## Troubleshooting

### "Atlas returned empty answer_text" / 0% scores everywhere

You probably forgot `WORKSPACE_PY` + `WORKSPACE_VENV_PY` env vars when
starting the daemon. The runner subprocesses `["workspace", ...]` which
fails with `FileNotFoundError` (workspace is a zsh function, not a
binary) and the orchestrator falls through to empty response.

```bash
# Check: env should have these set:
env | grep WORKSPACE
# WORKSPACE_PY=/Users/ray/tandemstream/core--shadow-runtime/tools/workspace/workspace.py
# WORKSPACE_VENV_PY=/Users/ray/.config/workspace/venv/bin/python

# If missing: stop daemon, export vars, restart:
cd /Users/ray/tandemstream/atlas-shadow
make ingest-down
export WORKSPACE_PY=...
export WORKSPACE_VENV_PY=...
make ingest-up-detached
```

### "workspace: error: unrecognized arguments: --question ..."

The workspace.py at the configured path doesn't have `argparse.REMAINDER`
for the `run` subcommand. Symptom: stderr shows usage / error about
unrecognized args.

```bash
grep -c "REMAINDER" $WORKSPACE_PY
# Should print > 0. If 0, update that core checkout to a recent main.
```

### Webhook events arrive but daemon ignores them

Check HMAC: webhook secret in `~/.atlas-shadow/webhook.secret` must
match the secret `gh webhook forward` was launched with. They drift if
you regenerate one without restarting both.

```bash
# Match secret to forwarder + daemon:
SECRET=$(cat ~/.atlas-shadow/webhook.secret)
echo "${SECRET:0:8}..."  # quick visual confirmation
# Both processes should have been started with this exact value.
```

### Daemon ledger has gaps after operator was offline

The reconciler will auto-enqueue current ``origin/main`` HEAD on its
next tick (default ≤5 min), so the corpus self-heals to the tip. But
the reconciler does NOT walk the intermediate SHAs — if you want every
commit in the ledger (for snapshot grading at a historical SHA), run
``make ingest-replay`` explicitly:

```bash
LAST=$(python3 -c "import json; print(json.load(open('.daemon-state.json'))['latest_commit_ingested'])")
make ingest-replay FROM=$LAST
```

### Daemon is in-sync but webhook forwarder is dead

Symptom: ``make webhook-forward-status`` shows DEAD / NOT RUNNING, but
``make ingest-status`` shows ``latest_commit_ingested`` at or near
``origin/main`` HEAD. This is the reconciler doing its job — the code
corpus is current. But PR-grading is not firing (``pull_request`` events
need the forwarder). Restart it:

```bash
make webhook-forward-down       # cleans pidfile
make webhook-forward-up-detached
```

If you see this happen often, check ``.webhook-forwarder.log`` for an
underlying ``gh webhook forward`` crash and consider the launchd plist
(see Known gap 4).

### Batch hangs on a single packet

Likely an atlas-query subprocess. Default timeout is 180s per receipt
(in `runner.invoke_atlas_query`). If atlas is genuinely slow on
something, the receipt times out and grades as `atlas_not_found` with
a timeout marker. Check `.ingest-daemon.log` for the receipt that
stalled.

### Grading produces `revision_not_indexed` for every receipt

The `--commit-sha` you passed isn't in the daemon's ledger. Pre-ingest:

```bash
make ingest-replay COMMIT=<your-target-sha>
```

---

## Reference

- **Org UUID**: `36a5bde2-9de3-4278-b18c-1e2da1916034` (`shadow_main`)
- **Daemon port**: `8765` (`http://127.0.0.1:8765/webhook`)
- **Webhook secret path**: `~/.atlas-shadow/webhook.secret` (chmod 600)
- **Daemon log**: `atlas-shadow/.ingest-daemon.log`
- **Daemon pid**: `atlas-shadow/.ingest-daemon.pid`
- **Daemon state**: `atlas-shadow/.daemon-state.json`
- **SQLite ledger**: `~/.atlas-shadow/ingest.db`
- **Worktrees + SCIP cache**: `~/.atlas-shadow/cache/`
- **Forwarder log**: `atlas-shadow/.webhook-forwarder.log`
- **Forwarder pid**: `atlas-shadow/.webhook-forwarder.pid`
- **Dashboard root**: `atlas-shadow/shadow-runs/`
