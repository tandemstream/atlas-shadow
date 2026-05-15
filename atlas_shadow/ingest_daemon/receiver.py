"""receiver — FastAPI app with ``POST /webhook`` and ``GET /status``.

Endpoints:

  * ``POST /webhook`` — GitHub webhook handler. Dispatches by the
    ``X-GitHub-Event`` header:

    - **``push``** — original D5 path. If the push is to ``refs/heads/main``
      of ``tandemstream/core``, enqueues the head commit SHA for the worker
      to ingest. Returns 200 ``{"queued": bool, "reason": str}`` for the
      queue outcome, 202 for non-main / zero-sha pushes.

    - **``pull_request``** — P2 packet 2026-05-14-atlas-shadow-pre-merge-
      grading-gate-v1 (T1). For ``opened`` / ``synchronize`` / ``reopened``
      actions, extracts the PR metadata (number, base.sha, head.sha, repo)
      and hands off to the configured ``pr_event_handler`` callback (T5's
      ``grader_service.run_pr_grading``). Async by design (RQ-5): the
      handler runs in a FastAPI ``BackgroundTask`` so the webhook returns
      quickly; merge-block uses GitHub Checks transitions (T7), not the
      webhook ACK.

    - **other event types** — accepted with 202 (``{"handled": false}``);
      GitHub doesn't retry 2xx, which keeps the queue clear of unrelated
      events.

    Common to both branches:
    - HMAC verify via ``X-Hub-Signature-256`` against
      ``GITHUB_WEBHOOK_SECRET`` (I1 invariant). Bad/missing signature → 400.
    - Missing webhook secret in cfg → 503 (avoid open relay).
    - Malformed JSON → 400.

  * ``GET /status`` — return the daemon's freshness snapshot.
    - ``latest_commit_ingested`` (sha or null)
    - ``latest_commit_ingested_at`` (iso8601 or null)
    - ``queue_depth`` (int)
    - ``last_error`` (string or null — from the most-recent failed row)
    - ``latest_code_revision_id`` (uuid or null)

This module is the FastAPI app factory: callers (entrypoint, tests)
call :func:`create_app(cfg)` to get an ``app`` instance. T5's grader
service is wired in by passing ``pr_event_handler=grader_service.handle_pr_event``
at app-construction time.
"""

# NOTE: deliberately NOT using `from __future__ import annotations` —
# FastAPI's dependency injection inspects runtime type annotations to
# distinguish `Request` (no DI marker) from query params; with deferred
# annotations the type is just a string and FastAPI treats `request`
# as a query parameter, returning 422 to every POST.

import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import ledger as ledger_mod
from . import queue as queue_mod
from .config import DaemonConfig


# Type-only imports — FastAPI is heavy; we import lazily so tests can
# build the app even if uvicorn isn't installed.


# PR-event actions we grade. Other actions (closed, edited, labeled, ...)
# are accepted but not graded — GitHub doesn't retry 2xx, so we keep the
# webhook quiet rather than spawning grading work for irrelevant churn.
_GRADED_PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


@dataclass(frozen=True)
class PrEvent:
    """Normalized GitHub ``pull_request`` event payload.

    The PR-event handler (T5's ``grader_service``) consumes this in
    preference to the raw GitHub payload so the receiver remains the
    single place where payload shape changes need adapting.
    """

    action: str
    repo_full_name: str
    pr_number: int
    base_sha: str
    base_ref: str
    head_sha: str
    head_ref: str
    title: str
    html_url: str


def _verify_hmac(*, secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    """GitHub's ``X-Hub-Signature-256`` is ``sha256=<hex>``."""
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    sent = signature_header.removeprefix("sha256=").strip()
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, mac)


def _extract_commit_for_main_push(payload: dict[str, Any]) -> Optional[str]:
    """If the webhook payload is a push to main, return the head SHA."""
    ref = payload.get("ref")
    if ref != "refs/heads/main":
        return None
    after = payload.get("after")
    if not after or after == "0000000000000000000000000000000000000000":
        # Branch delete or zero SHA — skip.
        return None
    return str(after).lower()


def _extract_pr_event(payload: dict[str, Any]) -> Optional[PrEvent]:
    """Parse a ``pull_request`` payload into a :class:`PrEvent`.

    Returns ``None`` when required fields are missing — the receiver
    treats that as malformed and returns 400 to the caller (so misshapen
    payloads are surfaced rather than silently dropped).
    """
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    if not isinstance(action, str) or not isinstance(pr, dict) or not isinstance(repo, dict):
        return None
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    number = pr.get("number")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    base_ref = base.get("ref") if isinstance(base, dict) else None
    head_ref = head.get("ref") if isinstance(head, dict) else None
    repo_full_name = repo.get("full_name")
    if not (isinstance(number, int)
            and isinstance(base_sha, str) and base_sha
            and isinstance(head_sha, str) and head_sha
            and isinstance(base_ref, str)
            and isinstance(head_ref, str)
            and isinstance(repo_full_name, str)):
        return None
    return PrEvent(
        action=action,
        repo_full_name=repo_full_name,
        pr_number=int(number),
        base_sha=base_sha.lower(),
        base_ref=base_ref,
        head_sha=head_sha.lower(),
        head_ref=head_ref,
        title=str(pr.get("title") or ""),
        html_url=str(pr.get("html_url") or ""),
    )


def _default_pr_event_handler(cfg: DaemonConfig, event: PrEvent) -> None:
    """Logging-only handler used until T5's ``grader_service`` is wired in.

    The real handler (``grader_service.handle_pr_event``) is passed via
    ``create_app(..., pr_event_handler=...)`` at startup. Tests that
    don't exercise T5 can use this default — it just records that the
    event was received.
    """
    print(
        f"[ingest-daemon] PR event received (no handler configured): "
        f"action={event.action} repo={event.repo_full_name} "
        f"pr=#{event.pr_number} base={event.base_sha[:7]} head={event.head_sha[:7]}",
        file=sys.stderr,
    )


def create_app(
    cfg: DaemonConfig,
    *,
    pr_event_handler: Optional[Callable[[DaemonConfig, PrEvent], None]] = None,
):
    """Build the FastAPI app bound to ``cfg``.

    Args:
      cfg: daemon config (carries ``webhook_secret``, ``db_path``, etc.).
      pr_event_handler: optional callback invoked for graded PR actions
        (``opened`` / ``synchronize`` / ``reopened``). Defaults to a
        stderr-logging stub; T5's ``grader_service.handle_pr_event`` is
        wired in here at entrypoint setup time. Test code can pass a mock
        to assert the handler is called with the right :class:`PrEvent`.

    Lazy-imports FastAPI so that the test module can import this file
    even when FastAPI isn't on the venv.
    """
    # Imports are local so the module is import-safe without FastAPI.
    # See module docstring for why we avoid `from __future__ import
    # annotations` — FastAPI inspects runtime type annotations.
    from fastapi import BackgroundTasks, FastAPI, Request, Response

    handler = pr_event_handler or _default_pr_event_handler

    app = FastAPI(title="atlas-shadow ingest daemon", version="1.0.0")

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        if cfg.webhook_secret:
            sig = request.headers.get("X-Hub-Signature-256")
            if not _verify_hmac(secret=cfg.webhook_secret, body=body, signature_header=sig):
                return Response(
                    content=json.dumps({"error": "invalid HMAC"}),
                    status_code=400,
                    media_type="application/json",
                )
        else:
            # If no secret configured, refuse to accept (avoid open relay).
            return Response(
                content=json.dumps({"error": "GITHUB_WEBHOOK_SECRET not configured"}),
                status_code=503,
                media_type="application/json",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response(
                content=json.dumps({"error": "invalid JSON body"}),
                status_code=400,
                media_type="application/json",
            )
        if not isinstance(payload, dict):
            return Response(
                content=json.dumps({"error": "payload must be a JSON object"}),
                status_code=400,
                media_type="application/json",
            )

        event_type = request.headers.get("X-GitHub-Event", "").lower()

        if event_type == "pull_request":
            pr_event = _extract_pr_event(payload)
            if pr_event is None:
                return Response(
                    content=json.dumps({"error": "malformed pull_request payload"}),
                    status_code=400,
                    media_type="application/json",
                )
            if pr_event.action not in _GRADED_PR_ACTIONS:
                return Response(
                    content=json.dumps(
                        {
                            "handled": False,
                            "reason": f"pull_request action not graded: {pr_event.action}",
                            "pr_number": pr_event.pr_number,
                        }
                    ),
                    status_code=202,
                    media_type="application/json",
                )
            # RQ-5: async grading. The handler runs in a background task
            # so the webhook ACK is fast; GitHub Checks (T7) carries the
            # eventual status transition. Errors raised by the handler
            # surface in daemon logs but do not fail the webhook.
            background.add_task(handler, cfg, pr_event)
            return Response(
                content=json.dumps(
                    {
                        "handled": True,
                        "reason": f"pr_grading_scheduled:{pr_event.action}",
                        "pr_number": pr_event.pr_number,
                        "base_sha": pr_event.base_sha,
                    }
                ),
                status_code=200,
                media_type="application/json",
            )

        if event_type == "push" or not event_type:
            # Original D5 path. (No header is treated as a push for
            # backward compatibility with the existing webhook fixtures.)
            sha = _extract_commit_for_main_push(payload)
            if sha is None:
                return Response(
                    content=json.dumps(
                        {"queued": False, "reason": "not-a-main-push-or-zero-sha"}
                    ),
                    status_code=202,
                    media_type="application/json",
                )
            result = queue_mod.enqueue(
                cfg.db_path,
                sha,
                source="webhook",
                max_attempts=cfg.max_attempts_per_commit,
            )
            return Response(
                content=json.dumps(result),
                status_code=200,
                media_type="application/json",
            )

        # Any other event type (issue_comment, check_run, etc.): accept
        # but don't act. 2xx prevents GitHub from retrying.
        return Response(
            content=json.dumps(
                {"handled": False, "reason": f"event-not-graded:{event_type}"}
            ),
            status_code=202,
            media_type="application/json",
        )

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return compute_status(cfg)

    return app


def compute_status(cfg: DaemonConfig) -> dict[str, Any]:
    """Build the ``/status`` payload from ledger + queue tables.

    Pulled out so tests can call it without standing up FastAPI.
    """
    succ = ledger_mod.latest_succeeded(cfg.db_path)
    latest_any = ledger_mod.latest_attempt(cfg.db_path)
    depth = queue_mod.queue_depth(cfg.db_path)
    last_error: Optional[str] = None
    if latest_any and latest_any.get("status") == "failed":
        last_error = latest_any.get("error_message")
    return {
        "latest_commit_ingested": succ.get("commit_sha") if succ else None,
        "latest_commit_ingested_at": succ.get("finished_at") if succ else None,
        "latest_code_revision_id": succ.get("code_revision_id") if succ else None,
        "queue_depth": depth,
        "last_error": last_error,
        "daemon": {
            "org_id": cfg.continuous_shadow_org_id,
            "state_file": str(cfg.state_file),
            "db_path": str(cfg.db_path),
        },
    }
