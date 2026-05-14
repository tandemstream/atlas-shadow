"""receiver — FastAPI app with ``POST /webhook`` and ``GET /status``.

Endpoints:

  * ``POST /webhook`` — GitHub ``push`` webhook handler.
    - Verifies the ``X-Hub-Signature-256`` HMAC against
      ``GITHUB_WEBHOOK_SECRET``.
    - Decodes payload; if it's a push to ``refs/heads/main`` of
      ``tandemstream/core``, enqueues the head commit SHA.
    - Returns 200 with ``{"queued": bool, "reason": str}``.
    - Returns 400 for bad-HMAC / malformed / missing-header.
    - Returns 202 for valid pushes that aren't to main (we accept but
      ignore — avoids noisy retries from GitHub).
    - Designed to ACK in <10s; the worker drains async.

  * ``GET /status`` — return the daemon's freshness snapshot.
    - ``latest_commit_ingested`` (sha or null)
    - ``latest_commit_ingested_at`` (iso8601 or null)
    - ``queue_depth`` (int)
    - ``last_error`` (string or null — from the most-recent failed row)
    - ``latest_code_revision_id`` (uuid or null)

This module is the FastAPI app factory: callers (entrypoint, tests)
call :func:`create_app(cfg)` to get an ``app`` instance.
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
from typing import Any, Optional

from . import ledger as ledger_mod
from . import queue as queue_mod
from .config import DaemonConfig


# Type-only imports — FastAPI is heavy; we import lazily so tests can
# build the app even if uvicorn isn't installed.


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


def create_app(cfg: DaemonConfig):
    """Build the FastAPI app bound to ``cfg``.

    Lazy-imports FastAPI so that the test module can import this file
    even when FastAPI isn't on the venv.
    """
    # Imports are local so the module is import-safe without FastAPI.
    # See module docstring for why we avoid `from __future__ import
    # annotations` — FastAPI inspects runtime type annotations.
    from fastapi import FastAPI, Request, Response

    app = FastAPI(title="atlas-shadow ingest daemon", version="1.0.0")

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
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
