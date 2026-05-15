"""T11 — PR grading gate test suite.

Covers AG1 through AG7 (T4a / AG8 are in ``test_doc_resolver.py``).

Test isolation:
  * Every test uses ``daemon_config`` from ``conftest.py`` — fresh
    SQLite DB + tmp state file per test. No shared mutable state.
  * GitHub Checks / Issues API calls run through an injectable
    ``_http=...`` stub that records requests and returns canned responses.
  * The runner (Atlas-query subprocess) is stubbed via ``_runner_run_one``
    so tests don't need a real `workspace run atlas-query` install.
  * The grader is stubbed via ``ATLAS_SHADOW_GRADER_STUB=1`` env (existing
    short-circuit in ``grader.grade``) or via explicit ``_grader_grade``
    injection.

AG7 (offline regression) is exercised manually via
``make shadow-run FIXTURE=dogfood-v2 LIMIT=1``. This file's
``test_offline_imports_unchanged`` proves the import surface stays
intact — a deeper regression check than the make target catches.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
try:
    from fastapi.testclient import TestClient  # type: ignore
except ImportError:  # pragma: no cover
    pytest.skip("fastapi.testclient unavailable", allow_module_level=True)

from atlas_shadow.ingest_daemon import grader_service as grader_service_mod
from atlas_shadow.ingest_daemon import ledger as ledger_mod
from atlas_shadow.ingest_daemon import receiver as receiver_mod
from atlas_shadow.ingest_daemon import state_file as state_file_mod


SECRET = "test-secret-do-not-use-in-prod"
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
HEAD_SHA = "abcdef0123456789abcdef0123456789abcdef01"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_payload(
    *,
    action: str = "opened",
    pr_number: int = 42,
    base_sha: str = BASE_SHA,
    head_sha: str = HEAD_SHA,
    repo: str = "tandemstream/core",
) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "title": "Test PR",
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
            "base": {"sha": base_sha, "ref": "main"},
            "head": {"sha": head_sha, "ref": "feature/test"},
        },
        "repository": {"full_name": repo},
    }


# ===========================================================================
# AG1 — HMAC verify (T1)
# ===========================================================================


def test_hmac_rejects_bad_sig(daemon_config, db_path):
    """Bad HMAC on a pull_request webhook -> 400 + no handler call."""
    handler = MagicMock()
    app = receiver_mod.create_app(daemon_config, pr_event_handler=handler)
    client = TestClient(app)
    body = json.dumps(_pr_payload()).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert resp.status_code == 400
    assert handler.call_count == 0


def test_hmac_accepts_valid_sig(daemon_config, db_path):
    """Valid HMAC + opened action -> 200 + handler scheduled."""
    handler = MagicMock()
    app = receiver_mod.create_app(daemon_config, pr_event_handler=handler)
    client = TestClient(app)
    body = json.dumps(_pr_payload(action="opened")).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["handled"] is True
    assert data["pr_number"] == 42
    # BackgroundTasks runs the handler synchronously when TestClient
    # finishes a request, so by now the handler has been called.
    assert handler.call_count == 1
    cfg_passed, event_passed = handler.call_args[0]
    assert event_passed.action == "opened"
    assert event_passed.base_sha == BASE_SHA.lower()
    assert event_passed.pr_number == 42


def test_pr_event_non_graded_action_returns_202(daemon_config, db_path):
    """closed/labeled/etc. accepted with 202 but handler not invoked."""
    handler = MagicMock()
    app = receiver_mod.create_app(daemon_config, pr_event_handler=handler)
    client = TestClient(app)
    body = json.dumps(_pr_payload(action="closed")).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 202
    assert resp.json()["handled"] is False
    assert handler.call_count == 0


def test_pr_event_malformed_payload_returns_400(daemon_config, db_path):
    """Payload missing required fields -> 400."""
    handler = MagicMock()
    app = receiver_mod.create_app(daemon_config, pr_event_handler=handler)
    client = TestClient(app)
    # Missing pull_request.base.sha
    bad = {
        "action": "opened",
        "pull_request": {"number": 1, "title": "x", "base": {}, "head": {"sha": HEAD_SHA, "ref": "f"}},
        "repository": {"full_name": "tandemstream/core"},
    }
    body = json.dumps(bad).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 400
    assert handler.call_count == 0


def test_other_event_type_returns_202(daemon_config, db_path):
    """X-GitHub-Event=check_run / issue_comment / etc. -> 202 not graded."""
    handler = MagicMock()
    app = receiver_mod.create_app(daemon_config, pr_event_handler=handler)
    client = TestClient(app)
    body = json.dumps({"action": "created"}).encode("utf-8")
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "check_run",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 202
    assert handler.call_count == 0


# ===========================================================================
# AG2 — Packet-tag detection (T2)
# ===========================================================================


def test_detect_packet_qna_log_found():
    files = [
        "README.md",
        "products/tandem/packages/python/atlas/docs/work/2026-05-14-foo-v1/02-qna-log.md",
        "products/tandem/packages/python/atlas/docs/work/2026-05-14-foo-v1/02-plan.md",
        "src/main.py",
    ]
    hits = grader_service_mod.detect_packet_qna_log(files)
    assert hits == [
        "products/tandem/packages/python/atlas/docs/work/2026-05-14-foo-v1/02-qna-log.md"
    ]


def test_detect_packet_qna_log_absent():
    files = [
        "README.md",
        "src/main.py",
        "docs/architecture.md",
        "docs/work/notes.txt",  # has /docs/work but not /02-qna-log.md
    ]
    assert grader_service_mod.detect_packet_qna_log(files) == []


def test_detect_packet_qna_log_multiple_packets():
    """A PR may legitimately touch multiple packets' qna logs."""
    files = [
        "docs/work/2026-05-14-a-v1/02-qna-log.md",
        "docs/work/2026-05-14-b-v2/02-qna-log.md",
        "src/x.py",
    ]
    hits = grader_service_mod.detect_packet_qna_log(files)
    assert set(hits) == {
        "docs/work/2026-05-14-a-v1/02-qna-log.md",
        "docs/work/2026-05-14-b-v2/02-qna-log.md",
    }


# ===========================================================================
# AG3 — Receipt parser (T3)
# ===========================================================================

_SAMPLE_QNA_LOG = """# Q&A log — test packet

**grading_threshold_pct:** 60

---

## §1 Test phase

### q1: simple receipt

- **Claim supported:** "The receipt parser handles the canonical bullet-list format."
- **Status:** `ok`
- **Evidence type:** `source_excerpt`
- **Source ref:**
  - repo: `tandemstream/core`
  - commit: `abcdef0123456789abcdef0123456789abcdef01`
  - tree_state: `clean`
  - path: `src/foo.py`
  - lines: `10-20`
  - excerpt_sha256: `0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef`
- **Command:**
  - text: `scripts/qa_lookup.sh sed-range src/foo.py 10 20`
  - exit_code: 0
- **Excerpt:**
  ```
  def foo():
      return 42
  ```

### q2: absence receipt

- **Claim supported:** "No legacy `bar` reference exists."
- **Status:** `ok_absence`
- **Evidence type:** `absence_search`
- **Source ref:**
  - repo: `tandemstream/core`
  - commit: `abcdef0123456789abcdef0123456789abcdef01`
  - tree_state: `clean`
  - absence_scope: `src/`
- **Command:**
  - text: `scripts/qa_lookup.sh grep "bar" src`
  - exit_code: 1
- **Excerpt:**
  ```
  [zero matches]
  ```
"""


def test_parse_packet_receipts(tmp_path):
    p = tmp_path / "02-qna-log.md"
    p.write_text(_SAMPLE_QNA_LOG, encoding="utf-8")
    receipts, threshold = grader_service_mod.parse_packet_receipts(p)
    assert threshold == 60
    assert len(receipts) == 2
    r1 = receipts[0]
    assert r1.question_id == "q1"
    assert r1.status == "ok"
    assert r1.source_path == "src/foo.py"
    assert r1.source_lines == "10-20"
    assert r1.excerpt_sha256.startswith("0123456789")
    assert "def foo()" in r1.oracle_excerpt
    assert r1.command_text and "sed-range" in r1.command_text
    r2 = receipts[1]
    assert r2.question_id == "q2"
    assert r2.status == "ok_absence"
    assert "[zero matches]" in r2.oracle_excerpt
    assert r2.command_text and "grep" in r2.command_text


def test_parse_packet_receipts_no_threshold_returns_none(tmp_path):
    p = tmp_path / "02-qna-log.md"
    p.write_text(
        _SAMPLE_QNA_LOG.replace("**grading_threshold_pct:** 60\n\n", ""),
        encoding="utf-8",
    )
    _, threshold = grader_service_mod.parse_packet_receipts(p)
    assert threshold is None


# ===========================================================================
# AG4 — Translator (T4)
# ===========================================================================


def _mk_receipt(**kw) -> grader_service_mod.PacketReceipt:
    defaults = dict(
        question_id="q1",
        question="Test question",
        oracle_claim="claim",
        oracle_excerpt="excerpt",
    )
    defaults.update(kw)
    return grader_service_mod.PacketReceipt(**defaults)


def test_translate_sed_range_to_find_code():
    r = _mk_receipt(
        source_path="src/foo.py",
        command_text="scripts/qa_lookup.sh sed-range src/foo.py 10 20",
    )
    out = grader_service_mod.translate_receipt_to_query(r)
    assert isinstance(out, grader_service_mod.CodeQuery)
    assert out.tool == "find_code"


def test_translate_grep_to_scan_search():
    r = _mk_receipt(
        source_path=None,
        command_text='scripts/qa_lookup.sh grep "needle" src',
    )
    out = grader_service_mod.translate_receipt_to_query(r)
    assert isinstance(out, grader_service_mod.CodeQuery)
    assert out.tool == "scan_search"


def test_translate_query_hint_override():
    r = _mk_receipt(
        source_path="src/foo.py",
        command_text="scripts/qa_lookup.sh sed-range src/foo.py 10 20",
        query_hint="scan_search",
    )
    out = grader_service_mod.translate_receipt_to_query(r)
    assert isinstance(out, grader_service_mod.CodeQuery)
    assert out.tool == "scan_search"


def test_translate_query_hint_invalid_falls_back_to_heuristic():
    r = _mk_receipt(
        source_path="src/foo.py",
        command_text="scripts/qa_lookup.sh sed-range src/foo.py 10 20",
        query_hint="not_a_real_tool",
    )
    out = grader_service_mod.translate_receipt_to_query(r)
    assert isinstance(out, grader_service_mod.CodeQuery)
    assert out.tool == "find_code"


@pytest.mark.parametrize("ext", [".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".log"])
def test_translate_doc_extension_routes_to_t4a(ext):
    r = _mk_receipt(
        source_path=f"docs/architecture{ext}",
        command_text="scripts/qa_lookup.sh sed-range docs/x 1 5",
    )
    out = grader_service_mod.translate_receipt_to_query(r)
    assert isinstance(out, grader_service_mod.DocQuery)


def test_translate_doc_path_overrides_query_hint():
    """Doc-path routing wins even when query_hint says find_code (D-P2-10)."""
    r = _mk_receipt(
        source_path="docs/foo.md",
        command_text="sed-range docs/foo.md 1 5",
        query_hint="find_code",
    )
    out = grader_service_mod.translate_receipt_to_query(r)
    assert isinstance(out, grader_service_mod.DocQuery)


# ===========================================================================
# AG5 — Grading + pinning (T5+T6)
# ===========================================================================


@dataclass
class _StubEvent:
    action: str
    repo_full_name: str
    pr_number: int
    base_sha: str
    base_ref: str
    head_sha: str
    head_ref: str
    title: str = "Test PR"
    html_url: str = ""


@dataclass
class _StubHttp:
    """Records every call; returns canned responses by URL pattern."""

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, *, method, url, body=None, headers=None, timeout=30):
        from atlas_shadow.ingest_daemon.gh_check import HttpResponse
        self.calls.append({"method": method, "url": url, "body": body})
        for pattern, resp in self.responses.items():
            if pattern in url and (
                resp.get("methods") is None or method in resp["methods"]
            ):
                return HttpResponse(
                    status=resp.get("status", 200),
                    body=json.dumps(resp.get("body") or {}).encode("utf-8"),
                    headers={},
                )
        raise AssertionError(f"unexpected HTTP call: {method} {url}")


def _fake_grader(grade="full_match", confidence=0.9, rationale="stub"):
    from atlas_shadow.grader import GraderResponse

    def grade_fn(**kwargs):
        return GraderResponse(
            grade=grade, confidence=confidence, rationale=rationale, latency_ms=0
        )
    return grade_fn


def _fake_runner_run_one():
    """Returns a ShadowResponse-like object with answer_text='stub atlas response'."""
    from atlas_shadow.runner import AtlasResponse, ShadowResponse

    def run_one(receipt, **kwargs):
        return ShadowResponse(
            question_id=receipt.question_id,
            question=receipt.question,
            fixture_id="pr-packet",
            atlas_response=AtlasResponse(
                tool_used=kwargs.get("tool", "find_code"),
                answer_text="stub atlas response",
                raw_result={},
                evidence_keys=[],
                atlas_latency_ms=10,
                request_id="test-req",
                commit="",
            ),
            wall_time_ms=20,
            captured_at="2026-05-15T00:00:00+00:00",
            org_id=kwargs.get("org_id", ""),
            tool=kwargs.get("tool", "find_code"),
        )
    return run_one


def test_run_pr_grading_stub(daemon_config, db_path, state_file, tmp_path, monkeypatch):
    """End-to-end orchestrator run with all I/O stubbed.

    Sets up: an ingested base SHA in the ledger (so T6 finds a
    code_revision_id), a stubbed PR-files response, a stubbed contents-
    api response with a real receipt body, a stubbed runner (returns
    "stub atlas response"), a stubbed grader (returns full_match), and
    stubbed GH check + comment endpoints.

    Asserts: pin acquired during run + released after; artifact written;
    check_run updated to success.
    """
    # 1. Seed the ledger so find_by_commit_sha returns a code_revision_id.
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=BASE_SHA,
        status="succeeded",
        started_at="2026-05-15T00:00:00+00:00",
        attempt_number=1,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        latency_ms=1000,
    )

    # 2. Point shadow_runs_dir at tmp_path.
    from dataclasses import replace
    cfg = replace(
        daemon_config,
        shadow_runs_dir=tmp_path / "shadow-runs",
    )

    # 3. Build the stub event.
    event = _StubEvent(
        action="opened",
        repo_full_name="tandemstream/core",
        pr_number=99,
        base_sha=BASE_SHA,
        base_ref="main",
        head_sha=HEAD_SHA,
        head_ref="feature/x",
    )

    # 4. Set up stub HTTP responses.
    import base64
    qna_log_b64 = base64.b64encode(_SAMPLE_QNA_LOG.encode("utf-8")).decode()
    http = _StubHttp(responses={
        "/pulls/99/files": {
            "methods": ["GET"],
            "body": [
                {"filename": "products/tandem/packages/python/atlas/docs/work/2026-05-14-test-v1/02-qna-log.md", "status": "added"},
                {"filename": "src/x.py", "status": "modified"},
            ],
        },
        "/contents/products/tandem/packages/python/atlas/docs/work/2026-05-14-test-v1/02-qna-log.md": {
            "methods": ["GET"],
            "body": {"encoding": "base64", "content": qna_log_b64},
        },
        "/check-runs": {
            "methods": ["POST"],
            "body": {"id": 555},
        },
        "/check-runs/555": {
            "methods": ["PATCH"],
            "body": {"id": 555, "conclusion": "success"},
        },
        "/issues/99/comments": {
            "methods": ["GET", "POST"],
            "body": [],
        },
    })

    # 5. Wire grader + runner stubs via the orchestrator's injection seams.
    def stubbed_grade_one(*, cfg, receipt, code_revision_id, repo_full_name, **_kw):
        return grader_service_mod._grade_one_receipt(
            cfg=cfg,
            receipt=receipt,
            code_revision_id=code_revision_id,
            repo_full_name=repo_full_name,
            _runner_run_one=_fake_runner_run_one(),
            _grader_grade=_fake_grader(grade="full_match", confidence=0.9),
        )

    # 6. Run the orchestrator.
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod
    outcome = grader_service_mod.run_pr_grading(
        cfg,
        event,
        github_token="fake-token",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: grader_service_mod._fetch_file_at_ref(_http=http, **kw),
        _create_check=lambda **kw: gh_check_mod.create_check_in_progress(_http=http, **kw),
        _update_check=lambda **kw: gh_check_mod.update_check_complete(_http=http, **kw),
        _post_comment=lambda **kw: pr_comment_mod.post_or_update_pr_comment(_http=http, **kw),
        _grade_one=stubbed_grade_one,
    )

    # 7. Assertions.
    assert outcome["status"] == "ok"
    assert outcome["check_run_id"] == 555
    assert outcome["code_revision_id"] == "11111111-1111-1111-1111-111111111111"
    assert outcome["conclusion"] == "success"
    assert len(outcome["summaries"]) == 1
    s = outcome["summaries"][0]
    assert s["passed"] is True
    assert s["pass_count"] == 2
    assert s["total"] == 2

    # Pin released
    assert state_file_mod.get_pinned_revision(cfg.state_file, pr_number=99) is None

    # Artifact written
    artifact_path = Path(s["artifact_path"])
    assert artifact_path.exists()
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["pr_number"] == 99
    assert payload["passed"] is True
    assert len(payload["rows"]) == 2

    # check_run was created + updated; comment was posted
    methods_and_urls = [(c["method"], c["url"]) for c in http.calls]
    assert any(m == "POST" and "/check-runs" in u for m, u in methods_and_urls)
    assert any(m == "PATCH" and "/check-runs/555" in u for m, u in methods_and_urls)
    assert any(m == "POST" and "/issues/99/comments" in u for m, u in methods_and_urls)


def test_pin_added_before_run_released_after(daemon_config, db_path, tmp_path):
    """T6 lifecycle: acquire_pin sets it; release_pin removes it.

    Direct test of the helpers (the end-to-end test above covers the
    orchestrator-level lifecycle).
    """
    cfg = daemon_config
    pr_number = 7
    state_file_mod.acquire_pin(
        cfg.state_file,
        pr_number=pr_number,
        code_revision_id="22222222-2222-2222-2222-222222222222",
    )
    assert state_file_mod.get_pinned_revision(cfg.state_file, pr_number=pr_number) == (
        "22222222-2222-2222-2222-222222222222"
    )
    # Daemon ingest write must NOT wipe the pin.
    state_file_mod.write_state(
        state_file_path=cfg.state_file,
        latest_commit_ingested=BASE_SHA,
        latest_code_revision_id="33333333-3333-3333-3333-333333333333",
    )
    assert state_file_mod.get_pinned_revision(cfg.state_file, pr_number=pr_number) == (
        "22222222-2222-2222-2222-222222222222"
    )
    # Release.
    state_file_mod.release_pin(cfg.state_file, pr_number=pr_number)
    assert state_file_mod.get_pinned_revision(cfg.state_file, pr_number=pr_number) is None


def test_find_by_commit_sha_returns_succeeded_only(daemon_config, db_path):
    """T6: ledger.find_by_commit_sha filters to status=succeeded."""
    # Insert one failed + one succeeded.
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=BASE_SHA,
        status="failed",
        started_at="2026-05-15T00:00:00+00:00",
        attempt_number=1,
        error_message="something went wrong",
    )
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=BASE_SHA,
        status="succeeded",
        started_at="2026-05-15T00:01:00+00:00",
        attempt_number=2,
        code_revision_id="44444444-4444-4444-4444-444444444444",
    )
    row = ledger_mod.find_by_commit_sha(db_path, BASE_SHA)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["code_revision_id"] == "44444444-4444-4444-4444-444444444444"

    # No succeeded -> None.
    row2 = ledger_mod.find_by_commit_sha(db_path, "f" * 40)
    assert row2 is None


# ===========================================================================
# AG6 — GH check transitions (T7) — mocked GH API
# ===========================================================================


def test_gh_check_create_and_update():
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    http = _StubHttp(responses={
        "/check-runs": {"methods": ["POST"], "body": {"id": 999, "status": "in_progress"}},
        "/check-runs/999": {"methods": ["PATCH"], "body": {"id": 999, "conclusion": "failure"}},
    })

    created = gh_check_mod.create_check_in_progress(
        repo_full_name="o/r",
        head_sha=HEAD_SHA,
        github_token="t",
        _http=http,
    )
    assert created["id"] == 999

    updated = gh_check_mod.update_check_complete(
        repo_full_name="o/r",
        check_run_id=999,
        conclusion="failure",
        output_title="atlas-shadow grading: fail",
        output_summary="2/5 receipts passed (40%)",
        github_token="t",
        _http=http,
    )
    assert updated["conclusion"] == "failure"

    # Verify the POST and PATCH bodies carry the right payload shape.
    posted = json.loads(http.calls[0]["body"])
    assert posted["status"] == "in_progress"
    assert posted["head_sha"] == HEAD_SHA
    patched = json.loads(http.calls[1]["body"])
    assert patched["status"] == "completed"
    assert patched["conclusion"] == "failure"
    assert "title" in patched["output"]


def test_gh_check_non_2xx_raises():
    """A 4xx response from the Checks API should raise RuntimeError."""
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    http = _StubHttp(responses={
        "/check-runs": {"methods": ["POST"], "status": 422, "body": {"message": "bad"}},
    })
    with pytest.raises(RuntimeError):
        gh_check_mod.create_check_in_progress(
            repo_full_name="o/r",
            head_sha=HEAD_SHA,
            github_token="t",
            _http=http,
        )


def test_pr_comment_renders_revision_binding_column_when_doc_receipts_present():
    """T8: revision_binding column appears only when at least one row is doc."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    code_only = pr_comment_mod.GradingSummary(
        packet_id="2026-05-14-x-v1",
        code_revision_id="aa",
        base_sha=BASE_SHA,
        threshold_pct=50,
        rows=[
            pr_comment_mod.ReceiptGradingRow(
                question_id="q1",
                question="x",
                grade="full_match",
                confidence=0.9,
                rationale="ok",
                tool="find_code",
            )
        ],
    )
    md_code = pr_comment_mod.build_comment_markdown(code_only)
    assert "revision_binding" not in md_code

    mixed = pr_comment_mod.GradingSummary(
        packet_id="2026-05-14-y-v1",
        code_revision_id="aa",
        base_sha=BASE_SHA,
        threshold_pct=50,
        rows=[
            pr_comment_mod.ReceiptGradingRow(
                question_id="q1",
                question="code-anchored",
                grade="full_match",
                confidence=0.9,
                rationale="ok",
                tool="find_code",
            ),
            pr_comment_mod.ReceiptGradingRow(
                question_id="q2",
                question="doc-anchored",
                grade="partial_match",
                confidence=0.7,
                rationale="ok",
                tool="doc_resolver",
                revision_binding="db_commit_scoped",
            ),
        ],
    )
    md_mixed = pr_comment_mod.build_comment_markdown(mixed)
    assert "revision_binding" in md_mixed
    assert "db_commit_scoped" in md_mixed


# ===========================================================================
# AG7 — Offline regression (I4)
# ===========================================================================


def test_offline_imports_unchanged():
    """The existing offline grader modules must still import + expose
    the public surface ``atlas_shadow.cli`` consumes.

    This is a regression guard: P2 may not break the offline flow.
    The deep regression check is ``make shadow-run FIXTURE=dogfood-v2
    LIMIT=1`` (run manually); this test is the unit-test stand-in.
    """
    from atlas_shadow import grader, parser, runner, cli  # noqa: F401

    # Symbols the offline grader pipeline reads:
    assert callable(grader.grade)
    assert callable(parser.parse_qna_log_markdown)
    assert callable(parser.parse_dogfood_v2_jsonl)
    assert callable(parser.parse_fixture)
    assert callable(runner.run_one)
    assert callable(runner.run_batch)
    assert callable(runner.resolve_code_revision_id)
