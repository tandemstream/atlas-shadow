# Pre-merge grading gate — operator runbook (P2 v1)

Atlas-shadow's pre-merge grading gate scores each packet-tagged GitHub PR
against the packet's own `[qa:qN]` receipts and posts the result back as
a Markdown PR comment + a `atlas-shadow-grading` GitHub Check.

This runbook covers:

1. [Required environment](#required-environment)
2. [GitHub setup](#github-setup)
3. [Public tunnel via cloudflared](#public-tunnel-via-cloudflared)
4. [Verifying the install](#verifying-the-install)
5. [Operating the daemon](#operating-the-daemon)
6. [Troubleshooting](#troubleshooting)

## Required environment

The grading gate needs four env vars (set in your shell, in
`.envrc`, or in the launchd / systemd unit you wrap around the daemon):

| Var | Purpose | Required? |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | HMAC verify on `POST /webhook`. Receiver returns 503 until set. | Yes |
| `GITHUB_ATLAS_SHADOW_TOKEN` | GitHub PAT with the right scopes (see "GitHub setup" below). Used to post commit statuses + PR comments + read PR files. | Yes |
| `ATLAS_DB_URL` (or `ATLAS_ADMIN_DB_URL`, or `ATLAS_SHADOW_DOC_RESOLVER_DB_URL`) | Postgres connection string for the T4a doc resolver's primary lookup. | Strongly recommended (T4a degrades to git-receipt-snapshot when absent) |
| `ANTHROPIC_API_KEY` (or use `ATLAS_SHADOW_GRADER_BACKEND=claude_cli`) | LLM-as-judge backend for `grader.grade`. The CLI fallback uses Claude Code's keychain. | One of the two |

Optional / advanced:

| Var | Default | Purpose |
|---|---|---|
| `ATLAS_SHADOW_DOC_RESOLVER_QUERY_TIMEOUT_MS` | `10000` | Postgres statement_timeout for T4a queries. |
| `ATLAS_SHADOW_GRADER_STUB` | unset | When set, grader skips API calls and returns `partial_match` for everything (test mode). |

## GitHub setup

1. Generate a token. The implementation uses the **Commit Statuses API**
   (`POST /repos/.../statuses/{sha}`), NOT the Check Runs API — Check
   Runs requires GitHub App authentication, which T12 smoketest confirmed
   PAT-authenticated callers can't use (403 ``You must authenticate via
   a GitHub App``). Pick the token shape that matches the API:
   - **Fine-grained PAT (recommended):** repository permissions:
     **Commit statuses: read/write**, **Pull requests: read/write**,
     **Contents: read**.
   - **Classic PAT:** the `repo` scope (covers `repo:status` +
     `public_repo` + everything else needed). For a token narrowed to
     just what's needed, use `repo:status` for the status writes plus
     `public_repo` (public repos) or `repo` (private repos) for the PR
     comment + Contents reads.
2. Export it as `GITHUB_ATLAS_SHADOW_TOKEN` in the daemon's environment.
3. In the GitHub repository's **Settings -> Webhooks**, add a webhook:
   - **Payload URL:** the cloudflared tunnel URL pointing at the daemon
     (see next section).
   - **Content type:** `application/json`.
   - **Secret:** matches `GITHUB_WEBHOOK_SECRET`.
   - **Events:** select individual events; check **Pull requests** AND
     **Pushes** (the existing D5 push path stays live).
4. In **Settings -> Branches -> Branch protection rule**, add
   `atlas-shadow-grading` to the required status checks for `main` (or
   whichever branch packets target). Merge blocks until the check posts.

## Public tunnel via cloudflared

The daemon listens on `127.0.0.1:8765` by default. To receive webhooks
from GitHub's cloud, expose it via cloudflared:

```bash
# One-shot quick tunnel — no Cloudflare account needed. URL changes on
# every restart; only use for testing or local-dev demos.
cloudflared tunnel --url http://localhost:8765

# Long-lived named tunnel — recommended for the production gate. Requires
# a Cloudflare account + `cloudflared tunnel login`.
cloudflared tunnel create atlas-shadow-grading
cloudflared tunnel route dns atlas-shadow-grading grading.example.com
cloudflared tunnel run atlas-shadow-grading
```

Set the GitHub webhook's **Payload URL** to
`https://grading.example.com/webhook` (or whatever URL the quick-tunnel
prints).

`cloudflared` is the simplest path for a single-host install. The
alternative (`ngrok`, reverse-proxy through your office router, etc.)
also works as long as GitHub can reach `POST /webhook` and the daemon
sees the body unchanged (so HMAC verifies).

## Verifying the install

Run `make grading-verify` from the atlas-shadow checkout. It checks the
env vars, that `shadow-runs/` is writable, that `core_repo_path` exists,
and that `psycopg2` is importable. Output looks like:

```
PASS: GITHUB_WEBHOOK_SECRET is set
PASS: GITHUB_ATLAS_SHADOW_TOKEN is set
PASS: doc-resolver DB URL configured via ATLAS_DB_URL
PASS: shadow_runs_dir writable at /path/to/atlas-shadow/shadow-runs
PASS: core_repo_path resolvable at /Users/ray/tandemstream/core--atlas
PASS: psycopg2 importable

All hard requirements passed. 0 warning(s).
```

Exit code 0 means the daemon is ready; 1 means at least one hard
requirement failed.

## Operating the daemon

```bash
# Foreground (Ctrl-C to stop):
make grading-up

# Detached:
make ingest-up-detached      # same process; both push + PR webhooks
tail -f .ingest-daemon.log
make ingest-down

# Status:
make ingest-status           # /status payload from disk; no HTTP needed
```

The daemon serves both push (ingest) and pull_request (grading) events
from a single `POST /webhook` endpoint — they're dispatched by the
`X-GitHub-Event` header in `receiver.py`. No separate process is needed
for grading.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Webhook returns 503 | `GITHUB_WEBHOOK_SECRET` not configured | Export the env var, restart daemon |
| Webhook returns 400 with `invalid HMAC` | Secret mismatch between GitHub and daemon | Re-copy the secret value from GitHub settings into the env var |
| Webhook returns 202 with `event-not-graded` | Event isn't `push` or `pull_request opened/synchronize/reopened` | This is expected for `closed`, `labeled`, etc. |
| Commit status stays in `pending` forever | Grader crashed before reaching `post_final_status` | Check `.ingest-daemon.log` for traceback; `run_pr_grading`'s error path should post a soft-pass `state=success` with description carrying the operational error (D-P2-5), but a daemon process crash before that point leaves the pending status behind. Manually update via `gh api -X POST /repos/{owner}/{repo}/statuses/{sha}` with `state=success` + `context=atlas-shadow-grading` to unstick the PR |
| PR comment shows `(no code_revision_id — base SHA not yet ingested)` | The PR's `base.sha` hasn't been ingested by the push path yet | Push to `main` triggers ingest; wait for the ingest job to land OR `make ingest-replay COMMIT=<base_sha>` |
| Doc receipts grade `unresolved_source_ref` | T4a primary DB lookup didn't find the doc artifact AND git_receipt_snapshot sha256 mismatched | Confirm the cited `<commit>:<path>` exists in the local clone; re-check the receipt's `excerpt_sha256` against the canonical body |
| Doc receipts grade `git_receipt_snapshot` (not `db_commit_scoped`) | The doc artifact isn't in Atlas's index for the cited commit | Expected for older commits or doc-only PRs. The snapshot path is fully valid; the comment column shows the binding so reviewers can see how it resolved |
