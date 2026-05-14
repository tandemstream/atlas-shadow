"""T-D1: receiver tests — HMAC, JSON, queue effects."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

# Skip the whole module when FastAPI isn't installed (e.g., in a stripped venv).
fastapi = pytest.importorskip("fastapi")
try:
    from fastapi.testclient import TestClient  # type: ignore
except ImportError:  # pragma: no cover
    pytest.skip("fastapi.testclient unavailable", allow_module_level=True)

from atlas_shadow.ingest_daemon import queue as queue_mod
from atlas_shadow.ingest_daemon import receiver as receiver_mod


SECRET = "test-secret-do-not-use-in-prod"
PUSH_SHA = "0123456789abcdef0123456789abcdef01234567"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push_payload(sha: str = PUSH_SHA, ref: str = "refs/heads/main") -> dict:
    return {
        "ref": ref,
        "before": "0" * 40,
        "after": sha,
        "repository": {"full_name": "tandemstream/core"},
    }


def test_valid_hmac_main_push_enqueues_and_returns_200(daemon_config, db_path):
    app = receiver_mod.create_app(daemon_config)
    client = TestClient(app)
    body = json.dumps(_push_payload()).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["queued"] is True
    assert data["reason"] == "new"
    assert queue_mod.queue_depth(db_path) == 1


def test_bad_hmac_returns_400_and_no_enqueue(daemon_config, db_path):
    app = receiver_mod.create_app(daemon_config)
    client = TestClient(app)
    body = json.dumps(_push_payload()).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert resp.status_code == 400
    assert queue_mod.queue_depth(db_path) == 0


def test_missing_signature_header_returns_400(daemon_config, db_path):
    app = receiver_mod.create_app(daemon_config)
    client = TestClient(app)
    body = json.dumps(_push_payload()).encode("utf-8")
    resp = client.post("/webhook", content=body)
    assert resp.status_code == 400
    assert queue_mod.queue_depth(db_path) == 0


def test_malformed_json_with_valid_hmac_returns_400(daemon_config, db_path):
    app = receiver_mod.create_app(daemon_config)
    client = TestClient(app)
    body = b"{not-json"
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status_code == 400
    assert queue_mod.queue_depth(db_path) == 0


def test_non_main_push_returns_202_no_enqueue(daemon_config, db_path):
    app = receiver_mod.create_app(daemon_config)
    client = TestClient(app)
    body = json.dumps(_push_payload(ref="refs/heads/feature/foo")).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status_code == 202
    assert resp.json()["queued"] is False
    assert queue_mod.queue_depth(db_path) == 0


def test_branch_delete_push_zero_sha_returns_202(daemon_config, db_path):
    app = receiver_mod.create_app(daemon_config)
    client = TestClient(app)
    payload = _push_payload(sha="0" * 40)
    body = json.dumps(payload).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status_code == 202
    assert queue_mod.queue_depth(db_path) == 0


def test_status_endpoint_returns_initial_empty_state(daemon_config, db_path):
    app = receiver_mod.create_app(daemon_config)
    client = TestClient(app)
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["latest_commit_ingested"] is None
    assert data["queue_depth"] == 0
    assert data["last_error"] is None
    assert data["daemon"]["org_id"] == daemon_config.continuous_shadow_org_id


def test_missing_webhook_secret_returns_503(daemon_config, db_path):
    """If GITHUB_WEBHOOK_SECRET isn't configured, the receiver refuses
    rather than acting as an open relay."""
    from dataclasses import replace

    cfg = replace(daemon_config, webhook_secret=None)
    app = receiver_mod.create_app(cfg)
    client = TestClient(app)
    body = json.dumps(_push_payload()).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status_code == 503


def test_compute_status_reflects_succeeded_ledger_row(daemon_config, db_path):
    from atlas_shadow.ingest_daemon import ledger as ledger_mod

    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=PUSH_SHA,
        status="succeeded",
        started_at="2026-05-14T00:00:00+00:00",
        attempt_number=1,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        latency_ms=5000,
    )
    payload = receiver_mod.compute_status(daemon_config)
    assert payload["latest_commit_ingested"] == PUSH_SHA
    assert payload["latest_code_revision_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["queue_depth"] == 0
