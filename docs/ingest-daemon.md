# atlas-shadow continuous-ingest daemon — runbook

Status: D5 of packet `2026-05-13-atlas-shadow-continuous-ingest-v1`.

The daemon is **off by default**. Atlas-shadow runs the benchmark against
`continuous_shadow_org_id` whether or not the daemon is running — when
it's off, the runner uses the pinned `continuous_shadow_code_revision_id`
from `shadow-config.yaml`. When it's on, the runner reads the fresher
`code_revision_id` from `.daemon-state.json`.

## What it does

For every push to `tandemstream/core@main`:

1. GitHub fires a webhook to `POST /webhook`.
2. The receiver verifies the HMAC against `GITHUB_WEBHOOK_SECRET`.
3. The commit SHA is enqueued in a SQLite queue at `~/.atlas-shadow/ingest.db`.
4. A background worker drains the queue serially:
   - `git fetch` + `git worktree` at the target SHA into `~/.atlas-shadow/cache/worktrees/<short-sha>/`.
   - `scip-python index --output ~/.atlas-shadow/cache/scip/core-<sha>.scip`.
   - Shell out to `<core_repo_path>/.venv/bin/python -m scripts.dogfood_v2_smoketest_ingest_code --org-id <continuous_shadow_org_id> --scip-path <scip> --source-root <worktree>` — one subprocess does both `ingest_scip_upload` and `chunk_code_revision`.
   - Write a `succeeded` row in `ingest_ledger`.
   - Atomically rewrite `<atlas-shadow>/.daemon-state.json`.

The runner reads that state file before each benchmark run; absence
gracefully falls back to the pinned config value.

## First-time setup

```sh
# 1. Install deps (adds fastapi + uvicorn on top of the Phase 2 venv).
make setup

# 2. Make sure shadow-config.yaml's `ingest_daemon:` section + the
#    existing `continuous_shadow_org_id`, `core_repo_path` keys are
#    populated.

# 3. Apply the daemon's SQLite schema (idempotent).
make ingest-bootstrap

# 4. Set the webhook secret. Production: put in `.env`, load with
#    `set -a && source .env && set +a` before `make ingest-up`.
export GITHUB_WEBHOOK_SECRET=<random-32-bytes-hex>

# 5. (One-time, optional) Backfill against a known SHA to prime the
#    state file + cache.
make ingest-replay COMMIT=$(cd /Users/ray/tandemstream/core && git rev-parse origin/main)
```

## Running

Foreground (dev):

```sh
make ingest-up
```

Detached (uses `nohup`, pid in `.ingest-daemon.pid`, log in `.ingest-daemon.log`):

```sh
make ingest-up-detached
make ingest-down       # stops it
```

Replay a single SHA (no webhook required):

```sh
make ingest-replay COMMIT=<40-char-sha>
```

Replay a range (every commit between `<sha>` and `origin/main`, oldest first):

```sh
make ingest-replay FROM=<sha-of-known-good>
```

Inspect state without HTTP:

```sh
make ingest-status
```

## Operator-facing endpoints

### `GET /status`

```sh
curl -s http://127.0.0.1:8765/status | jq .
```

```json
{
  "latest_commit_ingested": "abc123…",
  "latest_commit_ingested_at": "2026-05-14T16:30:00+00:00",
  "latest_code_revision_id": "fe98af79-23a7-4718-bae4-fa3f349878c8",
  "queue_depth": 0,
  "last_error": null,
  "daemon": {
    "org_id": "3ec689a0-678b-47ed-af17-a72e5adbfad8",
    "state_file": "/Users/ray/tandemstream/atlas-shadow/.daemon-state.json",
    "db_path": "/Users/ray/.atlas-shadow/ingest.db"
  }
}
```

`GET /status` is for operators. The **runner** reads
`<atlas-shadow>/.daemon-state.json` directly; it does **not** call
`/status` (file-as-IPC; no runtime dep on the daemon being up).

### `POST /webhook`

Configure the webhook on the `tandemstream/core` repo (or test fork):

- Payload URL: `https://<your-host>:8765/webhook`
- Content type: `application/json`
- Secret: `GITHUB_WEBHOOK_SECRET` (same value the daemon process has)
- Events: `Just the push event`.

The daemon ACKs in <10s; the heavy work (clone + scip + ingest + chunker)
happens in the background worker.

## State-file contract

`<atlas-shadow>/.daemon-state.json`, atomically replaced after each
successful ingest:

```json
{
  "latest_commit_ingested": "abc123…",
  "latest_code_revision_id": "fe98af79-23a7-4718-bae4-fa3f349878c8",
  "updated_at": "2026-05-14T16:30:00+00:00",
  "daemon_pid": 12345
}
```

Atomic rename: write tempfile → fsync → `os.rename`. A crash mid-write
leaves either the prior good file or no file — never a half-written one.

If the file is missing or unparseable, the runner falls back to
`shadow-config.yaml:continuous_shadow_code_revision_id`. This is by
design — the benchmark still runs when the daemon is off.

## Restart semantics

On boot the daemon:

1. Resets any `ingest_queue` row in `running` → `queued` (crash recovery).
2. Compares `state_file.latest_commit_ingested` to `origin/main` of the
   local core clone. If N commits behind, emits **one** stderr warning
   recommending `make ingest-replay FROM=<latest>`. **No auto-backfill**
   (amendment decision #11).

## Verifying it works

After `make ingest-up` and at least one ingest:

```sh
# 1. /status reports a recent SHA
curl -s http://127.0.0.1:8765/status | jq -r .latest_commit_ingested

# 2. The ledger has a row
sqlite3 ~/.atlas-shadow/ingest.db \
    "SELECT id, commit_sha, status, code_revision_id, latency_ms FROM ingest_ledger ORDER BY id DESC LIMIT 5;"

# 3. The state file is fresh
cat /Users/ray/tandemstream/atlas-shadow/.daemon-state.json

# 4. The runner picks up the new id
make shadow-run FIXTURE=dogfood-v2-questions LIMIT=1
# stderr line should reference `code_revision_id=<the-state-file's-id>`
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `make ingest-up` exits with "uvicorn not installed" | venv predates this packet | `make setup` |
| `/status` shows growing `queue_depth` and `last_error` filled | `scip-python` build failing or Atlas venv missing | run `make ingest-status`; check `~/.atlas-shadow/ingest.db` ledger; ensure `<core_repo_path>/.venv/bin/python` exists |
| `state_file` never updates | worker keeps failing — check `last_error` on the most recent ledger row | depends on the error |
| Runner uses stale `code_revision_id` even with daemon up | state file path mismatch | confirm `shadow-config.yaml:ingest_daemon.state_file` matches what the runner resolves (run `python -c "from atlas_shadow.runner import resolve_code_revision_id; ..."`) |
| Ledger row succeeded but `code_revision_id` is identical to a prior ingest | Atlas's cache-hit path fired (idempotency on `org_id, repo_url, commit_sha, indexer_version` — and the dogfood CLI hardcodes `commit_sha=87aa9fa`) | expected for v1 if the SCIP blob's bytes are unchanged; semantically each push that changes Python code in `core/` should still produce a fresh revision because the SCIP blob differs |

## Cleanup

Manually drop the cache when it gets large:

```sh
rm -rf ~/.atlas-shadow/cache/scip/        # SCIP blobs only
rm -rf ~/.atlas-shadow/cache/worktrees/   # checked-out worktrees
# Keep ~/.atlas-shadow/cache/core/ — the long-lived clone.

# Or, nuke everything (queue + ledger lost):
rm -rf ~/.atlas-shadow/
make ingest-bootstrap
```

The Atlas-side data (orgs, code_revisions, code_chunk_refs) is **not**
touched by these operations — that's Atlas's own DB.

## What this packet did NOT ship

- PR-branch ingest (only `main` is webhooked; a follow-on packet adds PR branches).
- Multi-repo coverage (only `tandemstream/core`).
- Production deployment surface (Cloud Run / Fly / Docker).
- Atlas-side modifications (the daemon shells out to a CLI that already exists).
- Incremental-ingest path — deferred to a follow-on if per-commit wall-time exceeds 10 min in operation (plan §12).
