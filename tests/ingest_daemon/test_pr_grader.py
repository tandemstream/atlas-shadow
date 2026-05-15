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


# ---------- AG3 hotspot deepening (receipt parser) ----------


def test_parse_real_p2_packet_qna_log():
    """Parse the actual P2 packet's 02-qna-log.md and confirm field
    extraction is right across all 12 receipts.

    This is the canary that catches drift between the parser and the
    real packet format authored against `check_qa_receipts.py`.
    """
    p = Path(
        "/Users/ray/tandemstream/core--atlas-shadow-pre-merge-grading-gate-v1/"
        "products/tandem/packages/python/atlas/docs/work/"
        "2026-05-14-atlas-shadow-pre-merge-grading-gate-v1/02-qna-log.md"
    )
    if not p.exists():
        pytest.skip(f"real packet not present at {p}")
    receipts, threshold = grader_service_mod.parse_packet_receipts(p)
    # Packet doesn't carry a threshold header today.
    assert threshold is None
    # 12 receipts (q1..q12) per the packet's amendment-2 receipt set.
    assert len(receipts) == 12
    qids = [r.question_id for r in receipts]
    assert qids == [f"q{n}" for n in range(1, 13)]

    # Spot-check fields that downstream consumers rely on.
    by_id = {r.question_id: r for r in receipts}

    # q1: code-anchored sed-range receipt.
    q1 = by_id["q1"]
    assert q1.status == "ok"
    assert q1.source_path and q1.source_path.endswith("code_tools.py")
    assert q1.source_commit and len(q1.source_commit) == 40
    assert q1.source_lines == "370-402"
    assert q1.excerpt_sha256 and len(q1.excerpt_sha256) == 64
    assert q1.command_text and "sed-range" in q1.command_text
    assert q1.command_exit_code == 0
    assert "def find_code" in q1.oracle_excerpt

    # q6: absence receipt (status=ok_absence; no excerpt_sha256 required).
    q6 = by_id["q6"]
    assert q6.status == "ok_absence"
    # ok_absence receipts don't need a sha256 — confirm we don't fabricate one.
    # (The actual P2 packet emits the receipt without excerpt_sha256 for absence.)
    assert q6.command_text and "grep" in q6.command_text


def test_parse_skips_malformed_block_keeps_valid_ones(tmp_path):
    """A block with no `Claim supported:` field is dropped; other valid
    blocks in the same file still parse. Defensive permissiveness:
    `02-qna-log.md` files shouldn't ever have malformed blocks in
    practice (the linter catches that upstream), but the parser must
    not abort the entire run when it does.
    """
    body = """# Test packet

## §1 Phase

### q1: valid receipt

- **Claim supported:** "ok"
- **Status:** `ok`
- **Excerpt:**
  ```
  hello
  ```

### q2: malformed (no Claim supported)

- **Status:** `ok`

### q3: also valid

- **Claim supported:** "still here"
- **Status:** `ok`
- **Excerpt:**
  ```
  world
  ```
"""
    p = tmp_path / "02-qna-log.md"
    p.write_text(body, encoding="utf-8")
    receipts, _ = grader_service_mod.parse_packet_receipts(p)
    qids = [r.question_id for r in receipts]
    assert qids == ["q1", "q3"]


@pytest.mark.parametrize(
    "line,expected",
    [
        ("grading_threshold_pct: 75", 75),
        ("**grading_threshold_pct:** 75", 75),
        ("**grading_threshold_pct**: 75", 75),
        ("Grading threshold pct: 75", 75),
        ("grading-threshold-pct: 0", 0),
        ("grading_threshold_pct: 100", 100),
        ("grading_threshold_pct: 101", None),  # out of range -> ignored
        ("grading_threshold_pct: -5", None),   # negative -> ignored (regex won't match -)
        ("grading_threshold_pct: not-a-number", None),
        ("# grading_threshold_pct: 50", None),  # comment-style; not at line start
    ],
)
def test_parse_threshold_variants(line, expected, tmp_path):
    body = f"# header\n\n{line}\n\n## §1\n\n### q1:\n\n- **Claim supported:** \"x\"\n"
    p = tmp_path / "02-qna-log.md"
    p.write_text(body, encoding="utf-8")
    _, threshold = grader_service_mod.parse_packet_receipts(p)
    assert threshold == expected


def test_parse_threshold_after_section_heading_ignored(tmp_path):
    """Threshold header inside §1 (after the first `## ` heading) must
    NOT match. The convention is preamble-only so reviewers can find it.
    """
    body = """# header

## §1 Section

grading_threshold_pct: 99

### q1: foo

- **Claim supported:** "x"
"""
    p = tmp_path / "02-qna-log.md"
    p.write_text(body, encoding="utf-8")
    _, threshold = grader_service_mod.parse_packet_receipts(p)
    assert threshold is None


def test_parse_empty_file_returns_empty(tmp_path):
    p = tmp_path / "02-qna-log.md"
    p.write_text("", encoding="utf-8")
    receipts, threshold = grader_service_mod.parse_packet_receipts(p)
    assert receipts == []
    assert threshold is None


def test_parse_whitespace_only_file(tmp_path):
    p = tmp_path / "02-qna-log.md"
    p.write_text("\n\n   \n\n", encoding="utf-8")
    receipts, threshold = grader_service_mod.parse_packet_receipts(p)
    assert receipts == []
    assert threshold is None


def test_parse_query_hint_extracted(tmp_path):
    body = """# packet

### q1: needs hint

- **Claim supported:** "x"
- **Status:** `ok`
- **Query hint:** `scan_search`
- **Excerpt:**
  ```
  whatever
  ```
"""
    p = tmp_path / "02-qna-log.md"
    p.write_text(body, encoding="utf-8")
    receipts, _ = grader_service_mod.parse_packet_receipts(p)
    assert len(receipts) == 1
    assert receipts[0].query_hint == "scan_search"


def test_parse_missing_optional_fields_uses_none(tmp_path):
    """A receipt with ONLY `Claim supported` parses; all other fields
    default to None / empty string. Defensive permissiveness.
    """
    body = """# packet

### q1: minimal

- **Claim supported:** "minimal claim"
"""
    p = tmp_path / "02-qna-log.md"
    p.write_text(body, encoding="utf-8")
    receipts, _ = grader_service_mod.parse_packet_receipts(p)
    assert len(receipts) == 1
    r = receipts[0]
    assert r.oracle_claim == "minimal claim"
    assert r.status is None
    assert r.source_path is None
    assert r.source_lines is None
    assert r.source_commit is None
    assert r.excerpt_sha256 is None
    assert r.command_text is None
    assert r.command_exit_code is None
    assert r.oracle_excerpt == ""


def test_parse_h2_and_h3_both_recognized(tmp_path):
    """The parser accepts both `## qN` (older convention) and `### qN`
    (current convention) as receipt boundaries.
    """
    body = """# packet

## q1: h2 form

- **Claim supported:** "h2 receipt"

### q2: h3 form

- **Claim supported:** "h3 receipt"
"""
    p = tmp_path / "02-qna-log.md"
    p.write_text(body, encoding="utf-8")
    receipts, _ = grader_service_mod.parse_packet_receipts(p)
    assert {r.question_id for r in receipts} == {"q1", "q2"}


def test_parse_excerpt_extracts_only_first_fence(tmp_path):
    """A receipt body may contain multiple fenced blocks (e.g., the
    Excerpt section + a separate quoted block elsewhere). The parser
    should take only the body of the first fence after `- **Excerpt:**`.
    """
    body = """# packet

### q1: multi-fence

- **Claim supported:** "x"
- **Excerpt:**
  ```
  FIRST FENCE BODY
  ```

Some narrative.

```
SECOND FENCE BODY (NOT THE EXCERPT)
```
"""
    p = tmp_path / "02-qna-log.md"
    p.write_text(body, encoding="utf-8")
    receipts, _ = grader_service_mod.parse_packet_receipts(p)
    assert len(receipts) == 1
    assert "FIRST FENCE BODY" in receipts[0].oracle_excerpt
    assert "SECOND FENCE BODY" not in receipts[0].oracle_excerpt


def test_parse_nonexistent_file_raises(tmp_path):
    p = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError):
        grader_service_mod.parse_packet_receipts(p)


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
        f"/statuses/{HEAD_SHA}": {
            "methods": ["POST"],
            "body": {"id": 555, "state": "success"},
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
        _post_pending=lambda **kw: gh_check_mod.post_pending_status(_http=http, **kw),
        _post_final=lambda **kw: gh_check_mod.post_final_status(_http=http, **kw),
        _post_comment=lambda **kw: pr_comment_mod.post_or_update_pr_comment(_http=http, **kw),
        _grade_one=stubbed_grade_one,
    )

    # 7. Assertions.
    assert outcome["status"] == "ok"
    assert outcome["status_state"] == "success"
    assert outcome["code_revision_id"] == "11111111-1111-1111-1111-111111111111"
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

    # Two statuses posted (pending + final), comment was posted.
    methods_and_urls = [(c["method"], c["url"]) for c in http.calls]
    status_posts = [
        (m, u) for m, u in methods_and_urls
        if m == "POST" and f"/statuses/{HEAD_SHA}" in u
    ]
    assert len(status_posts) == 2  # pending + final
    # Verify the pending was posted before the final.
    pending_body = json.loads(http.calls[
        [c["url"] for c in http.calls].index(
            next(c["url"] for c in http.calls if f"/statuses/{HEAD_SHA}" in c["url"])
        )
    ]["body"])
    assert pending_body["state"] == "pending"
    final_body = json.loads([c for c in http.calls if f"/statuses/{HEAD_SHA}" in c["url"]][-1]["body"])
    assert final_body["state"] == "success"
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


def test_gh_status_pending_and_final():
    """Commit Statuses API: POST pending + POST final on the same SHA.

    Migrated from the Check Runs API in 2026-05-15 when T12 smoketest
    surfaced that Check Runs requires GitHub App auth (PATs are rejected
    with 403). The Commit Statuses API works with PAT scopes.
    """
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    http = _StubHttp(responses={
        f"/statuses/{HEAD_SHA}": {
            "methods": ["POST"],
            "body": {"id": 999, "state": "pending"},
        },
    })

    pending = gh_check_mod.post_pending_status(
        repo_full_name="o/r",
        head_sha=HEAD_SHA,
        github_token="t",
        _http=http,
    )
    assert pending["state"] == "pending"

    final = gh_check_mod.post_final_status(
        repo_full_name="o/r",
        head_sha=HEAD_SHA,
        state="failure",
        description="fail 2/5 (40%)",
        github_token="t",
        _http=http,
    )
    # Each call returns whatever the stub gave us; the important assertion
    # is on the request payload (state + context + description).
    posted_pending = json.loads(http.calls[0]["body"])
    assert posted_pending["state"] == "pending"
    assert posted_pending["context"] == "atlas-shadow-grading"
    posted_final = json.loads(http.calls[1]["body"])
    assert posted_final["state"] == "failure"
    assert posted_final["description"] == "fail 2/5 (40%)"
    assert posted_final["context"] == "atlas-shadow-grading"


def test_gh_status_non_2xx_raises():
    """A 4xx response from the Statuses API should raise RuntimeError."""
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    http = _StubHttp(responses={
        f"/statuses/{HEAD_SHA}": {
            "methods": ["POST"], "status": 422, "body": {"message": "bad"},
        },
    })
    with pytest.raises(RuntimeError):
        gh_check_mod.post_pending_status(
            repo_full_name="o/r",
            head_sha=HEAD_SHA,
            github_token="t",
            _http=http,
        )


def test_gh_status_rejects_invalid_state():
    """``state`` must be one of the four valid values; others raise
    ValueError before any HTTP call (defensive contract check)."""
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    http = _StubHttp(responses={})  # would raise if called
    with pytest.raises(ValueError, match="state"):
        gh_check_mod.post_status(
            repo_full_name="o/r",
            head_sha=HEAD_SHA,
            state="not-a-real-state",
            description="x",
            github_token="t",
            _http=http,
        )


def test_gh_status_truncates_long_description():
    """GitHub caps description at 140 chars; the wrapper truncates with
    an ellipsis so the rendering is deterministic."""
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    http = _StubHttp(responses={
        f"/statuses/{HEAD_SHA}": {"methods": ["POST"], "body": {}},
    })
    long_desc = "x" * 200
    gh_check_mod.post_status(
        repo_full_name="o/r",
        head_sha=HEAD_SHA,
        state="success",
        description=long_desc,
        github_token="t",
        _http=http,
    )
    posted = json.loads(http.calls[0]["body"])
    assert len(posted["description"]) <= 140
    assert posted["description"].endswith("...")


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


# ===========================================================================
# Hotspot 3 deepening — pin lifecycle edge cases
# ===========================================================================


def _stub_http_for_skip_or_error(error_at: str = None):
    """Build a stub that satisfies enough of the orchestrator flow to
    reach the desired error point.
    """
    import base64
    qna_b64 = base64.b64encode(_SAMPLE_QNA_LOG.encode("utf-8")).decode()

    responses = {
        "/pulls/77/files": {
            "methods": ["GET"],
            "body": [
                {"filename": "products/tandem/packages/python/atlas/docs/work/2026-05-14-x-v1/02-qna-log.md", "status": "added"},
            ],
        },
        "/contents/products/tandem/packages/python/atlas/docs/work/2026-05-14-x-v1/02-qna-log.md": {
            "methods": ["GET"],
            "body": {"encoding": "base64", "content": qna_b64},
        },
        f"/statuses/{HEAD_SHA}": {
            "methods": ["POST"],
            "body": {"id": 1234, "state": "pending"},
        },
        "/issues/77/comments": {
            "methods": ["GET", "POST"],
            "body": [],
        },
    }
    return _StubHttp(responses=responses)


def test_pin_released_on_orchestrator_exception(daemon_config, db_path, state_file, tmp_path):
    """If grading raises mid-run (e.g., GH API 500), the pin MUST still
    be released in the finally:. Stale pins block GC.
    """
    # Seed ledger so the pin gets acquired.
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=BASE_SHA,
        status="succeeded",
        started_at="2026-05-15T00:00:00+00:00",
        attempt_number=1,
        code_revision_id="55555555-5555-5555-5555-555555555555",
        latency_ms=1000,
    )
    from dataclasses import replace
    cfg = replace(daemon_config, shadow_runs_dir=tmp_path / "sr")
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=77,
        base_sha=BASE_SHA, base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )
    http = _stub_http_for_skip_or_error()

    call_log = {"count": 0}

    def explosive_final(**kw):
        # First call (pending) succeeds; second call (final) raises so the
        # orchestrator's error path fires.
        call_log["count"] += 1
        if call_log["count"] == 1:
            raise RuntimeError("simulated GH outage during post_final_status")
        # Subsequent calls (the error-path's soft-pass attempt) also raise
        # so we exercise the nested-try in the error path.
        raise RuntimeError("simulated GH outage on error-path soft-pass")

    outcome = grader_service_mod.run_pr_grading(
        cfg, event, github_token="fake",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: grader_service_mod._fetch_file_at_ref(_http=http, **kw),
        _post_pending=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_pending_status']).post_pending_status(_http=http, **kw),
        _post_final=explosive_final,
        _post_comment=lambda **kw: __import__('atlas_shadow.ingest_daemon.pr_comment', fromlist=['post_or_update_pr_comment']).post_or_update_pr_comment(_http=http, **kw),
        _grade_one=lambda **kw: grader_service_mod._grade_one_receipt(
            **kw,
            _runner_run_one=_fake_runner_run_one(),
            _grader_grade=_fake_grader(),
        ),
    )
    assert outcome["status"] == "error"
    # The pin MUST be released even though grading exploded.
    assert state_file_mod.get_pinned_revision(cfg.state_file, pr_number=77) is None


def test_pin_not_acquired_when_base_sha_not_in_ledger(daemon_config, db_path, state_file, tmp_path):
    """If `find_by_commit_sha` returns None, no pin is acquired; release
    on no-pin is a safe no-op (no exception, no spurious state file).
    """
    from dataclasses import replace
    cfg = replace(daemon_config, shadow_runs_dir=tmp_path / "sr")
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=77,
        base_sha="9" * 40,  # NOT in ledger
        base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )
    http = _stub_http_for_skip_or_error()

    outcome = grader_service_mod.run_pr_grading(
        cfg, event, github_token="fake",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: grader_service_mod._fetch_file_at_ref(_http=http, **kw),
        _post_pending=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_pending_status']).post_pending_status(_http=http, **kw),
        _post_final=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_final_status']).post_final_status(_http=http, **kw),
        _post_comment=lambda **kw: __import__('atlas_shadow.ingest_daemon.pr_comment', fromlist=['post_or_update_pr_comment']).post_or_update_pr_comment(_http=http, **kw),
        _grade_one=lambda **kw: grader_service_mod._grade_one_receipt(
            **kw, _runner_run_one=_fake_runner_run_one(), _grader_grade=_fake_grader(),
        ),
    )
    assert outcome["status"] == "ok"
    assert outcome["code_revision_id"] is None
    # No pin entries should exist for this PR (and no spurious state).
    assert state_file_mod.get_pinned_revision(cfg.state_file, pr_number=77) is None


def test_pin_multiple_prs_independent(daemon_config, state_file):
    """Two concurrently-held pins don't stomp each other; releasing one
    leaves the other intact.
    """
    state_file_mod.acquire_pin(
        daemon_config.state_file, pr_number=10,
        code_revision_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    state_file_mod.acquire_pin(
        daemon_config.state_file, pr_number=11,
        code_revision_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    state_file_mod.release_pin(daemon_config.state_file, pr_number=10)
    assert state_file_mod.get_pinned_revision(daemon_config.state_file, pr_number=10) is None
    assert state_file_mod.get_pinned_revision(daemon_config.state_file, pr_number=11) == (
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )


def test_release_pin_when_state_file_missing(daemon_config, state_file):
    """release_pin must not crash when the state file doesn't exist yet
    (cold daemon start; nothing to release)."""
    # Ensure state file is absent.
    assert not daemon_config.state_file.exists()
    # Must not raise.
    state_file_mod.release_pin(daemon_config.state_file, pr_number=42)


def test_release_pin_when_state_file_corrupt(daemon_config, state_file):
    """release_pin gracefully handles a corrupt state file: read_state
    returns None, so the function falls back to an empty pin map and
    writes a clean file (no crash).
    """
    daemon_config.state_file.parent.mkdir(parents=True, exist_ok=True)
    daemon_config.state_file.write_text("{not valid json", encoding="utf-8")
    # Must not raise.
    state_file_mod.release_pin(daemon_config.state_file, pr_number=42)


def test_acquire_pin_preserves_other_pins_under_repeated_writes(daemon_config, state_file):
    """Acquiring three pins in a row, each call preserves the prior pins."""
    for n in (1, 2, 3):
        state_file_mod.acquire_pin(
            daemon_config.state_file,
            pr_number=n,
            code_revision_id=f"{n:08d}-{n:04d}-{n:04d}-{n:04d}-{n:012d}",
        )
    for n in (1, 2, 3):
        assert state_file_mod.get_pinned_revision(daemon_config.state_file, pr_number=n) == (
            f"{n:08d}-{n:04d}-{n:04d}-{n:04d}-{n:012d}"
        )


# ===========================================================================
# Hotspot 4 deepening — GitHub Checks / PR-comment behavior
# ===========================================================================


def test_run_pr_grading_skips_non_packet_pr(daemon_config, db_path, tmp_path):
    """A PR that touches no 02-qna-log.md should NOT create a check_run
    and NOT post a comment. The orchestrator returns
    `skipped_not_packet`.
    """
    from dataclasses import replace
    cfg = replace(daemon_config, shadow_runs_dir=tmp_path / "sr")
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=77,
        base_sha=BASE_SHA, base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )
    http = _StubHttp(responses={
        "/pulls/77/files": {
            "methods": ["GET"],
            "body": [
                {"filename": "src/foo.py", "status": "modified"},
                {"filename": "README.md", "status": "modified"},
            ],
        },
    })
    outcome = grader_service_mod.run_pr_grading(
        cfg, event, github_token="fake",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: pytest.fail("must not fetch contents"),
        _post_pending=lambda **kw: pytest.fail("must not post pending status"),
        _post_final=lambda **kw: pytest.fail("must not post final status"),
        _post_comment=lambda **kw: pytest.fail("must not post comment"),
    )
    assert outcome["status"] == "skipped_not_packet"
    assert outcome["packet_paths"] == []
    # No HTTP calls beyond the pulls/files list.
    assert all("/pulls/" in c["url"] for c in http.calls)


def test_run_pr_grading_continues_on_partial_grader_failure(daemon_config, db_path, tmp_path):
    """If `_grade_one` raises on one receipt, the orchestrator records a
    `no_match` row for it and KEEPS GRADING the rest. A single bad
    receipt MUST NOT abort the whole PR.
    """
    ledger_mod.insert_terminal_attempt(
        db_path, commit_sha=BASE_SHA, status="succeeded",
        started_at="2026-05-15T00:00:00+00:00", attempt_number=1,
        code_revision_id="77777777-7777-7777-7777-777777777777",
        latency_ms=1,
    )
    from dataclasses import replace
    cfg = replace(daemon_config, shadow_runs_dir=tmp_path / "sr")
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=77,
        base_sha=BASE_SHA, base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )
    http = _stub_http_for_skip_or_error()

    call_count = {"n": 0}

    def flaky_grade_one(**kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated grader crash on q1")
        return grader_service_mod._grade_one_receipt(
            **kw,
            _runner_run_one=_fake_runner_run_one(),
            _grader_grade=_fake_grader(grade="full_match"),
        )

    outcome = grader_service_mod.run_pr_grading(
        cfg, event, github_token="fake",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: grader_service_mod._fetch_file_at_ref(_http=http, **kw),
        _post_pending=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_pending_status']).post_pending_status(_http=http, **kw),
        _post_final=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_final_status']).post_final_status(_http=http, **kw),
        _post_comment=lambda **kw: __import__('atlas_shadow.ingest_daemon.pr_comment', fromlist=['post_or_update_pr_comment']).post_or_update_pr_comment(_http=http, **kw),
        _grade_one=flaky_grade_one,
    )
    # Outcome is ok (no fatal exception caught at orchestrator level).
    assert outcome["status"] == "ok"
    s = outcome["summaries"][0]
    # Two receipts in _SAMPLE_QNA_LOG; first failed, second graded.
    assert s["total"] == 2
    # Read the artifact: the first row should be no_match with error rationale.
    payload = json.loads(Path(s["artifact_path"]).read_text(encoding="utf-8"))
    rows_by_qid = {r["question_id"]: r for r in payload["rows"]}
    assert rows_by_qid["q1"]["grade"] == "no_match"
    assert "grading_error" in rows_by_qid["q1"]["rationale"]
    assert rows_by_qid["q2"]["grade"] == "full_match"


def test_run_pr_grading_operational_error_soft_passes(daemon_config, db_path, state_file, tmp_path):
    """When the orchestrator hits an OPERATIONAL error AFTER posting the
    pending status (e.g., GH Contents API outage), the status is updated
    to ``success`` with a descriptive error message so PRs don't
    soft-block indefinitely (D-P2-5).

    Commit Status API has no ``neutral`` equivalent — the closest match
    for "didn't grade but don't block" is ``success`` with a description
    that surfaces the error to operators.
    """
    ledger_mod.insert_terminal_attempt(
        db_path, commit_sha=BASE_SHA, status="succeeded",
        started_at="2026-05-15T00:00:00+00:00", attempt_number=1,
        code_revision_id="77777777-7777-7777-7777-777777777777",
        latency_ms=1,
    )
    from dataclasses import replace
    cfg = replace(daemon_config, shadow_runs_dir=tmp_path / "sr")
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=77,
        base_sha=BASE_SHA, base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )
    # HTTP: PR files lists a packet; pending status POST succeeds;
    # Contents API returns 500.
    http = _StubHttp(responses={
        "/pulls/77/files": {
            "methods": ["GET"],
            "body": [
                {"filename": "products/tandem/packages/python/atlas/docs/work/2026-05-14-x-v1/02-qna-log.md", "status": "added"},
            ],
        },
        f"/statuses/{HEAD_SHA}": {
            "methods": ["POST"],
            "body": {"id": 8888, "state": "pending"},
        },
        "/contents/": {
            "methods": ["GET"],
            "status": 500,
            "body": {"message": "internal error"},
        },
    })

    outcome = grader_service_mod.run_pr_grading(
        cfg, event, github_token="fake",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: grader_service_mod._fetch_file_at_ref(_http=http, **kw),
        _post_pending=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_pending_status']).post_pending_status(_http=http, **kw),
        _post_final=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_final_status']).post_final_status(_http=http, **kw),
        _post_comment=lambda **kw: __import__('atlas_shadow.ingest_daemon.pr_comment', fromlist=['post_or_update_pr_comment']).post_or_update_pr_comment(_http=http, **kw),
    )
    assert outcome["status"] == "error"
    # Two statuses posted on /statuses/<sha>: pending then a soft-pass success.
    status_posts = [
        c for c in http.calls
        if c["method"] == "POST" and f"/statuses/{HEAD_SHA}" in c["url"]
    ]
    assert len(status_posts) == 2
    final_body = json.loads(status_posts[1]["body"])
    assert final_body["state"] == "success"
    assert "operational error" in final_body["description"].lower()
    # And the pin was released.
    assert state_file_mod.get_pinned_revision(cfg.state_file, pr_number=77) is None


def test_handle_pr_event_no_token_returns_silently(daemon_config, monkeypatch, capsys):
    """`handle_pr_event` with no GITHUB_ATLAS_SHADOW_TOKEN must NOT raise
    or call any GH API. Logs to stderr so operators notice.
    """
    monkeypatch.delenv("GITHUB_ATLAS_SHADOW_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=99,
        base_sha=BASE_SHA, base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )
    grader_service_mod.handle_pr_event(daemon_config, event)
    err = capsys.readouterr().err
    assert "GITHUB_ATLAS_SHADOW_TOKEN" in err


def test_pr_comment_idempotent_marker_based_update():
    """`post_or_update_pr_comment` PATCHes the existing comment carrying
    the atlas-shadow marker; on first call (no marker) it POSTs new.
    """
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    # First call: no existing comment -> POST.
    http_new = _StubHttp(responses={
        "/issues/42/comments": {"methods": ["GET", "POST"], "body": []},
    })
    pr_comment_mod.post_or_update_pr_comment(
        repo_full_name="o/r",
        pr_number=42,
        body=pr_comment_mod.COMMENT_MARKER + "\n new body",
        github_token="t",
        _http=http_new,
    )
    post_calls = [c for c in http_new.calls if c["method"] == "POST"]
    assert len(post_calls) == 1

    # Second call: existing comment carries the marker -> PATCH (not POST).
    http_update = _StubHttp(responses={
        "/issues/42/comments": {
            "methods": ["GET"],
            "body": [
                {"id": 9876, "body": "irrelevant other comment"},
                {"id": 1234, "body": pr_comment_mod.COMMENT_MARKER + "\n old body"},
            ],
        },
        "/issues/comments/1234": {"methods": ["PATCH"], "body": {"id": 1234}},
    })
    pr_comment_mod.post_or_update_pr_comment(
        repo_full_name="o/r",
        pr_number=42,
        body=pr_comment_mod.COMMENT_MARKER + "\n new body",
        github_token="t",
        _http=http_update,
    )
    patch_calls = [c for c in http_update.calls if c["method"] == "PATCH"]
    assert len(patch_calls) == 1
    assert "/issues/comments/1234" in patch_calls[0]["url"]


def test_run_pr_grading_idempotent_rerun_updates_comment(daemon_config, db_path, tmp_path):
    """Re-running grading on the same PR creates a NEW check_run each
    time (GH Checks don't support PATCH-create-or-update) but PATCHes
    the existing PR comment via the marker.

    Tests two consecutive run_pr_grading calls against a stateful HTTP
    stub. The stub starts with no comments; after run 1, the comments
    list contains the atlas-shadow marker comment; run 2 finds-and-
    updates it.
    """
    ledger_mod.insert_terminal_attempt(
        db_path, commit_sha=BASE_SHA, status="succeeded",
        started_at="2026-05-15T00:00:00+00:00", attempt_number=1,
        code_revision_id="77777777-7777-7777-7777-777777777777",
        latency_ms=1,
    )
    from dataclasses import replace
    cfg = replace(daemon_config, shadow_runs_dir=tmp_path / "sr")
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=77,
        base_sha=BASE_SHA, base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )

    # Stateful comment list.
    comments: list[dict] = []

    import base64
    qna_b64 = base64.b64encode(_SAMPLE_QNA_LOG.encode("utf-8")).decode()

    class StatefulHttp:
        def __init__(self):
            self.calls = []

        def __call__(self, *, method, url, body=None, headers=None, timeout=30):
            from atlas_shadow.ingest_daemon.gh_check import HttpResponse
            self.calls.append({"method": method, "url": url, "body": body})
            if "/pulls/77/files" in url:
                return HttpResponse(
                    status=200,
                    body=json.dumps([
                        {"filename": "products/tandem/packages/python/atlas/docs/work/2026-05-14-x-v1/02-qna-log.md", "status": "added"},
                    ]).encode(),
                    headers={},
                )
            if "/contents/products/tandem/packages/python/atlas/docs/work/2026-05-14-x-v1/02-qna-log.md" in url:
                return HttpResponse(
                    status=200,
                    body=json.dumps({"encoding": "base64", "content": qna_b64}).encode(),
                    headers={},
                )
            if "/statuses/" in url and method == "POST":
                # Each post returns a new status id (id is the GH status
                # row id; orchestrator doesn't track it).
                return HttpResponse(
                    status=201,
                    body=json.dumps({"id": 10000 + len(self.calls), "state": "pending"}).encode(),
                    headers={},
                )
            if "/issues/77/comments" in url and method == "GET":
                return HttpResponse(
                    status=200,
                    body=json.dumps(comments).encode(),
                    headers={},
                )
            if "/issues/77/comments" in url and method == "POST":
                # Append a new comment carrying the marker.
                payload = json.loads(body or b"{}")
                cid = 5000 + len(comments)
                comments.append({"id": cid, "body": payload.get("body", "")})
                return HttpResponse(
                    status=201,
                    body=json.dumps({"id": cid}).encode(),
                    headers={},
                )
            if "/issues/comments/" in url and method == "PATCH":
                # Update the body in-place.
                cid = int(url.rsplit("/", 1)[1])
                for c in comments:
                    if c["id"] == cid:
                        c["body"] = json.loads(body or b"{}").get("body", c["body"])
                return HttpResponse(status=200, body=b"{}", headers={})
            raise AssertionError(f"unexpected: {method} {url}")

    http = StatefulHttp()
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod
    common_kw = dict(
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: grader_service_mod._fetch_file_at_ref(_http=http, **kw),
        _post_pending=lambda **kw: gh_check_mod.post_pending_status(_http=http, **kw),
        _post_final=lambda **kw: gh_check_mod.post_final_status(_http=http, **kw),
        _post_comment=lambda **kw: pr_comment_mod.post_or_update_pr_comment(_http=http, **kw),
        _grade_one=lambda **kw: grader_service_mod._grade_one_receipt(
            **kw, _runner_run_one=_fake_runner_run_one(), _grader_grade=_fake_grader(),
        ),
    )

    # Run 1
    outcome1 = grader_service_mod.run_pr_grading(cfg, event, github_token="t1", **common_kw)
    assert outcome1["status"] == "ok"
    # Exactly one comment created.
    assert len(comments) == 1
    initial_comment_id = comments[0]["id"]

    # Run 2 (re-fired webhook simulates 'synchronize' on the same PR).
    outcome2 = grader_service_mod.run_pr_grading(cfg, event, github_token="t1", **common_kw)
    assert outcome2["status"] == "ok"
    # Still exactly one comment — not duplicated. ID is the same comment;
    # body has been PATCHed (we don't assert on body content beyond the
    # marker still being present).
    assert len(comments) == 1
    assert comments[0]["id"] == initial_comment_id
    assert pr_comment_mod.COMMENT_MARKER in comments[0]["body"]

    # Two POSTs to /statuses/ per run (pending + final) -> 4 total across both runs.
    status_posts = [
        c for c in http.calls
        if c["method"] == "POST" and "/statuses/" in c["url"]
    ]
    assert len(status_posts) == 4


def test_run_pr_grading_multi_packet_pr_posts_aggregated_comment(daemon_config, db_path, tmp_path):
    """Codex review on impl PR (2026-05-15): a PR touching MORE than one
    `02-qna-log.md` must produce ONE PR comment with all packets'
    sections, not one comment per packet (which would PATCH-overwrite
    via the marker, leaving only the last packet's rows). And per-packet
    artifacts must not collide on filename.
    """
    ledger_mod.insert_terminal_attempt(
        db_path, commit_sha=BASE_SHA, status="succeeded",
        started_at="2026-05-15T00:00:00+00:00", attempt_number=1,
        code_revision_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        latency_ms=1,
    )
    from dataclasses import replace
    cfg = replace(daemon_config, shadow_runs_dir=tmp_path / "sr")
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=42,
        base_sha=BASE_SHA, base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )

    # Two packets touched on this PR.
    import base64
    pkt_a_path = "products/tandem/packages/python/atlas/docs/work/2026-05-14-packet-a-v1/02-qna-log.md"
    pkt_b_path = "products/tandem/packages/python/atlas/docs/work/2026-05-14-packet-b-v1/02-qna-log.md"
    qna_b64 = base64.b64encode(_SAMPLE_QNA_LOG.encode("utf-8")).decode()

    # Stateful HTTP: track comment posts + status posts.
    comments: list[dict] = []
    status_posts: list[dict] = []

    class TwoPacketHttp:
        def __init__(self):
            self.calls = []

        def __call__(self, *, method, url, body=None, headers=None, timeout=30):
            from atlas_shadow.ingest_daemon.gh_check import HttpResponse
            self.calls.append({"method": method, "url": url, "body": body})
            if "/pulls/42/files" in url and method == "GET":
                return HttpResponse(
                    status=200,
                    body=json.dumps([
                        {"filename": pkt_a_path, "status": "added"},
                        {"filename": pkt_b_path, "status": "added"},
                    ]).encode(),
                    headers={},
                )
            if "/contents/" in url and method == "GET":
                return HttpResponse(
                    status=200,
                    body=json.dumps({"encoding": "base64", "content": qna_b64}).encode(),
                    headers={},
                )
            if "/statuses/" in url and method == "POST":
                status_posts.append(json.loads(body or b"{}"))
                return HttpResponse(status=201, body=b'{"id":1}', headers={})
            if "/issues/42/comments" in url and method == "GET":
                return HttpResponse(
                    status=200, body=json.dumps(comments).encode(), headers={}
                )
            if "/issues/42/comments" in url and method == "POST":
                payload = json.loads(body or b"{}")
                cid = 1000 + len(comments)
                comments.append({"id": cid, "body": payload.get("body", "")})
                return HttpResponse(status=201, body=json.dumps({"id": cid}).encode(), headers={})
            if "/issues/comments/" in url and method == "PATCH":
                cid = int(url.rsplit("/", 1)[1])
                for c in comments:
                    if c["id"] == cid:
                        c["body"] = json.loads(body or b"{}").get("body", c["body"])
                return HttpResponse(status=200, body=b"{}", headers={})
            raise AssertionError(f"unexpected: {method} {url}")

    http = TwoPacketHttp()
    from atlas_shadow.ingest_daemon import gh_check as gh_check_mod
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod
    outcome = grader_service_mod.run_pr_grading(
        cfg, event, github_token="t",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: grader_service_mod._fetch_file_at_ref(_http=http, **kw),
        _post_pending=lambda **kw: gh_check_mod.post_pending_status(_http=http, **kw),
        _post_final=lambda **kw: gh_check_mod.post_final_status(_http=http, **kw),
        _post_comment=lambda **kw: pr_comment_mod.post_or_update_pr_comment(_http=http, **kw),
        _grade_one=lambda **kw: grader_service_mod._grade_one_receipt(
            **kw, _runner_run_one=_fake_runner_run_one(), _grader_grade=_fake_grader(),
        ),
    )
    assert outcome["status"] == "ok"
    assert len(outcome["summaries"]) == 2
    assert outcome["packet_paths"] == [pkt_a_path, pkt_b_path]

    # Exactly ONE comment posted — not duplicated per packet.
    assert len(comments) == 1
    body = comments[0]["body"]
    # Comment body contains BOTH packets' sections.
    assert "2026-05-14-packet-a-v1" in body
    assert "2026-05-14-packet-b-v1" in body
    # And the multi-packet header.
    assert "2 packets" in body or "Overall:" in body

    # Two artifact files written (one per packet) — distinct filenames.
    artifact_dir = cfg.shadow_runs_dir
    artifacts = sorted(artifact_dir.glob("pr-42-*.json"))
    assert len(artifacts) == 2
    assert artifacts[0].name != artifacts[1].name
    # Filenames include packet id (codex fix for the collision bug).
    assert any("packet-a-v1" in a.name for a in artifacts)
    assert any("packet-b-v1" in a.name for a in artifacts)

    # Status: pending posted once + final posted once (NOT per packet).
    pending_states = [s for s in status_posts if s["state"] == "pending"]
    final_states = [s for s in status_posts if s["state"] in ("success", "failure")]
    assert len(pending_states) == 1
    assert len(final_states) == 1


def test_build_comment_markdown_for_summaries_multi_section():
    """Direct test of the multi-packet renderer: two summaries -> one
    body with two `## atlas-shadow grading` sections + an overall header.
    """
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    def _row(qid, grade):
        return pr_comment_mod.ReceiptGradingRow(
            question_id=qid, question="q", grade=grade, confidence=0.9,
            rationale="ok", tool="find_code",
        )

    s1 = pr_comment_mod.GradingSummary(
        packet_id="pkt-a-v1", code_revision_id=None, base_sha=BASE_SHA,
        threshold_pct=50,
        rows=[_row("q1", "full_match"), _row("q2", "no_match")],
    )
    s2 = pr_comment_mod.GradingSummary(
        packet_id="pkt-b-v1", code_revision_id=None, base_sha=BASE_SHA,
        threshold_pct=50,
        rows=[_row("q1", "full_match"), _row("q2", "full_match")],
    )
    body = pr_comment_mod.build_comment_markdown_for_summaries([s1, s2])
    # ONE marker (idempotency).
    assert body.count(pr_comment_mod.COMMENT_MARKER) == 1
    # Multi-packet aggregate header.
    assert "2 packets" in body
    # Both packet ids appear.
    assert "pkt-a-v1" in body and "pkt-b-v1" in body
    # FAIL badge present (pkt-a passes 1/2 = 50%, just at threshold; pkt-b 2/2).
    # Overall pass count = 3, total = 4, pct = 75. All summary.passed checks:
    # pkt-a's pass_pct = 50 >= 50 -> True; pkt-b 100 >= 50 -> True.
    # So overall_passed = True -> Overall: PASS.
    assert "Overall:" in body and "PASS" in body


def test_build_comment_markdown_single_packet_backward_compat():
    """The single-packet wrapper renders identically to the multi-packet
    renderer with one summary (NO overall header, just the packet section).
    """
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    summary = pr_comment_mod.GradingSummary(
        packet_id="pkt-x", code_revision_id=None, base_sha=BASE_SHA,
        threshold_pct=50,
        rows=[pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q", grade="full_match",
            confidence=0.9, rationale="ok", tool="find_code",
        )],
    )
    body = pr_comment_mod.build_comment_markdown(summary)
    # No "N packets" aggregate header in single-packet form.
    assert "packets" not in body.lower() or body.lower().count("packets") == 0
    # Marker still present.
    assert pr_comment_mod.COMMENT_MARKER in body
    # The single packet's id appears in the section header.
    assert "pkt-x" in body
