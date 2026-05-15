# T12 — End-to-end operator smoketest (manual, pre-merge)

**Status:** DEFERRED — manual, not automated. Per the plan
(`02-plan.md` §3 In Scope T12), T12 is a manual operator step that
needs a live atlas-shadow daemon + a real GitHub webhook + a Cloudflare
tunnel. It is the ONE acceptance check that the unit-test suite (AG1-AG8)
cannot cover.

**Treat T12 as a pre-merge gate.** The plan calls T12 "in scope", and
the offline acceptance gates (AG1–AG7) all pass — but no PR should
merge before an operator has run the steps below at least once against
a synthetic PR. Document the result in `03-review-log.md` Timeline as
a `t12_smoketest_passed` / `t12_smoketest_failed` entry before merging
either repo's PR.

If the smoketest fails, the daemon does not need to revert — the failure
mode is "grading didn't fire end-to-end", which is a configuration /
plumbing issue, not a code regression. Fix the env / webhook / tunnel
config and re-run; the impl branch's code itself is independently
covered by the offline AGs.

---

## Pre-run setup

1. **`make grading-verify`** in `tandemstream/atlas-shadow`. Must exit 0.
   Run this **after** exporting the four required env vars
   (`GITHUB_WEBHOOK_SECRET`, `GITHUB_ATLAS_SHADOW_TOKEN`, an
   `ATLAS_*_DB_URL`, and `ANTHROPIC_API_KEY` OR
   `ATLAS_SHADOW_GRADER_BACKEND=claude_cli`).
2. Daemon running (`make grading-up` foreground, or
   `make ingest-up-detached`).
3. Tunnel exposed via `cloudflared tunnel --url http://localhost:8765`
   (or named tunnel — see `docs/pre-merge-grading-gate.md`).
4. GitHub webhook configured on `tandemstream/core` (or your test repo)
   pointing at the tunnel URL + `pull_request` + `push` events + correct
   secret.
5. Branch protection rule on `main`: required status check
   `atlas-shadow-grading` set.

## Smoketest sequence

| # | Action | Expected outcome | Pass? |
|---|---|---|---|
| 1 | Push a commit to `main` that updates a packet's `02-qna-log.md`. | `ingest-status` shows the SHA as `latest_commit_ingested`. | ☐ |
| 2 | Create a feature branch + open a draft PR that touches `02-qna-log.md` and the source files those receipts cite. | Daemon log shows `[ingest-daemon] PR event received (no handler configured)` is REPLACED with actual grading invocation. | ☐ |
| 3 | Within 10 seconds of opening the PR, the GH UI shows `atlas-shadow-grading` check in `in_progress`. | ✓ | ☐ |
| 4 | Within `len(receipts) * 180s`, the check transitions to `success` or `failure`. | ✓ | ☐ |
| 5 | A PR comment carrying the marker `<!-- atlas-shadow-grading -->` is posted. | Per-receipt table; columns include `qid`, `question`, `tool`, `grade`, `conf`, `rationale`. | ☐ |
| 6 | If the packet has doc-anchored receipts (`source_path` ending in `.md`/`.yaml`/etc.), the comment table includes a `revision_binding` column showing `db_commit_scoped` / `git_receipt_snapshot` / `unresolved_source_ref`. | ✓ | ☐ |
| 7 | A JSON artifact lands at `atlas-shadow/shadow-runs/pr-<N>-<ts>.json` matching the comment's grades. | ✓ | ☐ |
| 8 | Push a second commit to the PR branch (triggers `synchronize`). | New check_run created; the SAME PR comment is UPDATED (one comment total, not two). | ☐ |
| 9 | Close (without merging) the PR; open it again (`reopened` action). | Grading re-fires; the same PR comment is updated again. | ☐ |
| 10 | Open a PR that touches `src/foo.py` but NO `02-qna-log.md`. | No check_run is created; no PR comment is posted. Daemon log notes `skipped_not_packet`. | ☐ |
| 11 | `make shadow-run FIXTURE=dogfood-v2 LIMIT=1` (AG7 regression check). | Exit 0. | ☐ |
| 12 | Inspect `.daemon-state.json`: during the in-flight PR (step 3-4 window), the file contains a `pinned_revisions` entry keyed on the PR number. After grading completes (step 5), the pin is GONE. | ✓ | ☐ |

## Diagnostics (if a step fails)

- **Step 3 stuck on `in_progress` or never fires:** check `GITHUB_WEBHOOK_SECRET` matches the webhook config; check tunnel URL is reachable; check the daemon log for HMAC errors.
- **Step 4 / 5 / 7 don't all happen:** the grader hit an exception. The check_run should be `neutral` (operational-error path); check `.ingest-daemon.log` for the traceback. The PR-comment payload may or may not have posted depending on where the failure was.
- **Step 6 doc-receipts column absent when expected:** Atlas DB URL probably isn't configured; T4a degraded to git_receipt_snapshot for every doc receipt (which is correct, but the column should still appear). Run `make grading-verify` again.
- **Step 8 duplicate comments (not an update):** the marker isn't being matched. Look at the actual comment body — there should be an HTML comment `<!-- atlas-shadow-grading -->` at the top.
- **Step 10 grades a non-packet PR:** `detect_packet_qna_log` is mis-matching paths. Check the PR's file list; the regex requires `<...>/docs/work/<packet>/02-qna-log.md`.
- **Step 12 pin not released:** the grader crashed before the `finally:` block. Check the daemon log; the orchestrator's outer try/finally should always release. If pin survives a daemon restart, manual cleanup: edit `.daemon-state.json` and remove the entry.

## Recording the result

After the run, append a Timeline entry to
`docs/work/2026-05-14-atlas-shadow-pre-merge-grading-gate-v1/03-review-log.md`
(in the core packet's worktree):

```
### YYYY-MM-DD — T12 operator smoketest

**Stage:** t12_smoketest
**Verdict:** passed | failed
**Operator:** <name>
**Atlas-shadow HEAD:** <SHA>
**Notes:** <anything anomalous; reference step numbers from this checklist>
```

Then proceed with the PR merge (atlas-shadow first, then core planning packet — or vice versa, the two PRs don't depend on each other).
