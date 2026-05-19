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
        source_lines="10-20",
        command_text="scripts/qa_lookup.sh sed-range src/foo.py 10 20",
    )
    out = grader_service_mod.translate_receipt_to_query(r)
    assert isinstance(out, grader_service_mod.CodeQuery)
    assert out.tool == "find_code"
    assert "source_path: src/foo.py" in out.question
    assert "source_lines: 10-20" in out.question
    assert "command_text: scripts/qa_lookup.sh sed-range src/foo.py 10 20" in out.question


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


def test_grade_one_preserves_atlas_diagnostics_when_grader_fails(daemon_config):
    def broken_grader(**_kwargs):
        raise RuntimeError("grader unavailable")

    receipt = grader_service_mod.PacketReceipt(
        question_id="q1",
        question="Which code path writes chunks?",
        source_path="missing.py",
        source_lines="1-5",
        source_commit=BASE_SHA,
        oracle_excerpt="excerpt",
        oracle_claim="claim",
        command_text="rg chunks",
    )

    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        _runner_run_one=_fake_runner_run_one(),
        _grader_grade=broken_grader,
        # Test focuses on the grader-exception path. PR #17 added a
        # pre-atlas skip for receipts whose source can't be materialized
        # (which is true here — missing.py doesn't exist in the test
        # repo). Disable the skip so we still exercise the original
        # grading-flow + diagnostic-preservation logic.
        _classify_skip=lambda *_args, **_kwargs: None,
    )

    assert row.grade == "no_match"
    assert row.tool == "scan_search"
    assert "grading_error" in row.rationale
    assert row.atlas_answer_len == len("stub atlas response")
    assert row.atlas_returncode == 0
    assert "exception:RuntimeError" in row.warnings
    assert row.source_snapshot_status == "git_source_missing"


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
            # PR #17: bypass pre-atlas skip — orchestrator test
            # focuses on the end-to-end happy path; the sample qna log's
            # paths don't exist in the tmp test repo so the skip would
            # otherwise fire on every receipt.
            _classify_skip=lambda *_args, **_kwargs: None,
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


def test_orchestrator_refuses_to_grade_when_base_sha_not_in_ledger(daemon_config, db_path, state_file, tmp_path):
    """Codex review r5 (I2): when the daemon hasn't ingested base.sha,
    the orchestrator MUST NOT call the runner with
    `code_revision_id=None` (that would resolve atlas's "latest"
    revision and produce a misleading grade). The orchestrator
    refuses to grade unpinned, posts a soft-pass `success` status with
    a clear description, and explains the situation in a PR comment.
    """
    from dataclasses import replace
    cfg = replace(daemon_config, shadow_runs_dir=tmp_path / "sr")
    event = _StubEvent(
        action="opened", repo_full_name="o/r", pr_number=77,
        base_sha="9" * 40,  # NOT in ledger
        base_ref="main", head_sha=HEAD_SHA, head_ref="f",
    )
    http = _stub_http_for_skip_or_error()

    # If the orchestrator EVER calls _grade_one, the test fails: that's
    # the I2 violation we're guarding against.
    def _must_not_grade(**kw):
        pytest.fail("must not grade when code_revision_id is None (I2)")

    outcome = grader_service_mod.run_pr_grading(
        cfg, event, github_token="fake",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: grader_service_mod._fetch_file_at_ref(_http=http, **kw),
        _post_pending=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_pending_status']).post_pending_status(_http=http, **kw),
        _post_final=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_final_status']).post_final_status(_http=http, **kw),
        _post_comment=lambda **kw: __import__('atlas_shadow.ingest_daemon.pr_comment', fromlist=['post_or_update_pr_comment']).post_or_update_pr_comment(_http=http, **kw),
        _grade_one=_must_not_grade,
    )
    assert outcome["status"] == "revision_not_indexed"
    assert outcome["status_state"] == "success_revision_not_indexed"
    assert outcome["code_revision_id"] is None
    # No pin entries should exist for this PR (and no spurious state).
    assert state_file_mod.get_pinned_revision(cfg.state_file, pr_number=77) is None
    # Soft-pass `success` posted with the "not indexed" description.
    status_posts = [c for c in http.calls if c["method"] == "POST" and "/statuses/" in c["url"]]
    assert len(status_posts) == 2  # pending + final
    final_body = json.loads(status_posts[-1]["body"])
    assert final_body["state"] == "success"
    assert "not indexed" in final_body["description"]
    # Explainer comment posted (one comment, with marker so subsequent
    # grading runs can replace it cleanly).
    comment_posts = [c for c in http.calls if c["method"] == "POST" and "/issues/77/comments" in c["url"]]
    assert len(comment_posts) == 1
    posted_body = json.loads(comment_posts[0]["body"])["body"]
    assert "grading skipped" in posted_body
    assert "make ingest-replay" in posted_body


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
    """Codex review r7: a PR with no `02-qna-log.md` touched must still
    get a terminal commit status (state=success, "not a packet PR;
    nothing to grade") so branch protection rules that mark
    `atlas-shadow-grading` as required don't permanently block
    ordinary non-packet PRs. No PR comment is posted (nothing to say).
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
        f"/statuses/{HEAD_SHA}": {
            "methods": ["POST"],
            "body": {"id": 7777, "state": "success"},
        },
    })
    outcome = grader_service_mod.run_pr_grading(
        cfg, event, github_token="fake",
        _fetch_pr_files=lambda **kw: grader_service_mod._fetch_pr_files(_http=http, **kw),
        _fetch_file_at_ref=lambda **kw: pytest.fail("must not fetch contents"),
        _post_pending=lambda **kw: pytest.fail("must not post pending status for non-packet"),
        _post_final=lambda **kw: __import__('atlas_shadow.ingest_daemon.gh_check', fromlist=['post_final_status']).post_final_status(_http=http, **kw),
        _post_comment=lambda **kw: pytest.fail("must not post comment"),
    )
    assert outcome["status"] == "skipped_not_packet"
    assert outcome["status_state"] == "success_not_packet"
    assert outcome["packet_paths"] == []
    # Exactly one final status was posted: state=success, with the
    # "not a packet PR" description.
    status_posts = [c for c in http.calls if c["method"] == "POST" and "/statuses/" in c["url"]]
    assert len(status_posts) == 1
    posted = json.loads(status_posts[0]["body"])
    assert posted["state"] == "success"
    assert "not a packet PR" in posted["description"]


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
            # PR #17: bypass pre-atlas skip — orchestrator test focuses
            # on the partial-failure recovery path, not the new skips.
            _classify_skip=lambda *_args, **_kwargs: None,
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


# ─── PR #14 — clean-denominator grading + lane field + receipt-stale skip ───


def test_infer_lane_doc_resolver(daemon_config):
    """doc_resolver tool always lands in the doc_resolver lane,
    regardless of receipt anchor shape."""
    r = grader_service_mod.PacketReceipt(
        question_id="q", question="q",
        oracle_claim="", oracle_excerpt="",
        source_path="docs/x.md", source_lines="1-5",
    )
    assert grader_service_mod._infer_lane(
        tool_label="doc_resolver", receipt=r
    ) == "doc_resolver"


def test_infer_lane_scan_search(daemon_config):
    r = grader_service_mod.PacketReceipt(
        question_id="q", question="q",
        oracle_claim="", oracle_excerpt="",
    )
    assert grader_service_mod._infer_lane(
        tool_label="scan_search", receipt=r
    ) == "scan_search"


def test_infer_lane_explicit_source_fast_path(daemon_config):
    """find_code w/ both path AND lines maps to fast-path lane."""
    r = grader_service_mod.PacketReceipt(
        question_id="q", question="q",
        oracle_claim="", oracle_excerpt="",
        source_path="core/ai.py", source_lines="847-863",
    )
    assert grader_service_mod._infer_lane(
        tool_label="find_code", receipt=r
    ) == "explicit_source_fast_path"


def test_infer_lane_fuzzy_when_path_only(daemon_config):
    """find_code w/ just a path (no lines) is NOT fast-path eligible —
    fuzzy retrieval lane."""
    r = grader_service_mod.PacketReceipt(
        question_id="q", question="q",
        oracle_claim="", oracle_excerpt="",
        source_path="core/ai.py", source_lines=None,
    )
    assert grader_service_mod._infer_lane(
        tool_label="find_code", receipt=r
    ) == "fuzzy_find_code"


def test_infer_lane_fuzzy_when_no_anchor(daemon_config):
    """find_code w/ no anchors at all is fuzzy."""
    r = grader_service_mod.PacketReceipt(
        question_id="q", question="q",
        oracle_claim="", oracle_excerpt="",
    )
    assert grader_service_mod._infer_lane(
        tool_label="find_code", receipt=r
    ) == "fuzzy_find_code"


def test_infer_lane_fuzzy_when_empty_string_anchors():
    """Whitespace-only / empty-string source_path or source_lines
    shouldn't count as anchored — fall through to fuzzy."""
    r = grader_service_mod.PacketReceipt(
        question_id="q", question="q",
        oracle_claim="", oracle_excerpt="",
        source_path="   ", source_lines="  ",
    )
    assert grader_service_mod._infer_lane(
        tool_label="find_code", receipt=r
    ) == "fuzzy_find_code"


def test_derive_score_status_default_counted():
    """Default outcome — pass-grades and non-stale fails both count.

    PR #16 review: ``atlas_not_found + git_source_missing`` now flips
    to ``skipped_receipt_stale`` (same as no_match) because atlas
    can't be measured against a receipt whose anchor doesn't exist
    at the receipt commit. See
    ``test_derive_score_status_atlas_not_found_skip_paths`` below
    for that behavior; this test stays focused on the legit-counted
    cases (pass-grades + matching snapshots).
    """
    assert grader_service_mod._derive_score_status(
        grade="full_match", source_snapshot_status="git_source_hash_match"
    ) == ("counted", None)
    assert grader_service_mod._derive_score_status(
        grade="no_match", source_snapshot_status="git_source_hash_match"
    ) == ("counted", None)
    assert grader_service_mod._derive_score_status(
        grade="partial_match", source_snapshot_status="git_source_missing"
    ) == ("counted", None)


def test_derive_score_status_receipt_stale():
    """The only skip today: no_match + snap == git_source_missing."""
    assert grader_service_mod._derive_score_status(
        grade="no_match", source_snapshot_status="git_source_missing"
    ) == ("skipped_receipt_stale", "receipt_stale")


def test_derive_score_status_no_snapshot_treated_as_counted():
    """source_snapshot_status=None (e.g. doc_resolver receipts) means
    no snapshot was resolved — never a stale skip."""
    assert grader_service_mod._derive_score_status(
        grade="no_match", source_snapshot_status=None
    ) == ("counted", None)


# ─── PR #15: run-commit line drift ────────────────────────────────────


def test_derive_score_status_run_commit_drift():
    """The PR #15 case: receipt-commit snapshot matched (receipt valid
    at authoring), but the run-commit snapshot didn't (file moved
    between receipt and grading commits) AND grader said no_match.
    Drops out of the clean denominator."""
    assert grader_service_mod._derive_score_status(
        grade="no_match",
        source_snapshot_status="git_source_hash_match",
        run_snapshot_status="run_commit_hash_mismatch",
    ) == ("skipped_run_commit_line_drift", "run_commit_line_drift")


def test_derive_score_status_atlas_not_found_receipt_stale():
    """Codex PR #16 review: ``atlas_not_found`` is a failure grade
    just like ``no_match``. When paired with a receipt-side stale
    signal, it should be skipped on the same path."""
    assert grader_service_mod._derive_score_status(
        grade="atlas_not_found",
        source_snapshot_status="git_source_missing",
    ) == ("skipped_receipt_stale", "receipt_stale")


def test_derive_score_status_atlas_not_found_run_commit_drift():
    """Codex PR #16 review: ``atlas_not_found`` + receipt-snap-match
    + run-snap-mismatch is the q12-shaped case from the real PR #15
    probe — atlas's fast path returned an empty answer because the
    cited line range now contains code that doesn't match the
    receipt's expected excerpt. Skipped on the run-drift path so the
    clean denominator excludes it."""
    assert grader_service_mod._derive_score_status(
        grade="atlas_not_found",
        source_snapshot_status="git_source_hash_match",
        run_snapshot_status="run_commit_hash_mismatch",
    ) == ("skipped_run_commit_line_drift", "run_commit_line_drift")


def test_derive_score_status_atlas_not_found_run_commit_source_missing():
    """atlas_not_found + path deleted at run commit — same skip
    semantics as no_match + path deleted (both grades reflect
    non-measurement)."""
    assert grader_service_mod._derive_score_status(
        grade="atlas_not_found",
        source_snapshot_status="git_source_hash_match",
        run_snapshot_status="run_commit_source_missing",
    ) == ("skipped_run_commit_line_drift", "run_commit_line_drift")


def test_derive_score_status_atlas_not_found_stays_counted_when_no_drift():
    """atlas_not_found with both snapshots clean = real Atlas miss,
    not drift. Stays counted so the score reflects retrieval
    failure."""
    assert grader_service_mod._derive_score_status(
        grade="atlas_not_found",
        source_snapshot_status="git_source_hash_match",
        run_snapshot_status="run_commit_hash_match",
    ) == ("counted", None)


def test_derive_score_status_run_commit_source_missing_is_drift():
    """Codex PR #15 review note: a receipt valid at source_commit
    whose path/file was DELETED or RENAMED by run_commit is the same
    class of non-measurement as line drift — atlas isn't being graded
    against the receipt as authored. ``_derive_score_status`` must
    classify this consistently with the diagnostic classifier (which
    already buckets ``run_commit_source_missing`` as
    ``run_commit_line_drift``)."""
    assert grader_service_mod._derive_score_status(
        grade="no_match",
        source_snapshot_status="git_source_hash_match",
        run_snapshot_status="run_commit_source_missing",
    ) == ("skipped_run_commit_line_drift", "run_commit_line_drift")


def test_derive_score_status_both_snapshots_match_stays_counted():
    """Receipt-commit AND run-commit both match the excerpt, but
    grader still said no_match. This is the genuine fast-path-bug or
    atlas-precision case — counted as a real miss, not excluded.
    (Acceptance #2 from Codex's PR #15 spec.)"""
    assert grader_service_mod._derive_score_status(
        grade="no_match",
        source_snapshot_status="git_source_hash_match",
        run_snapshot_status="run_commit_hash_match",
    ) == ("counted", None)


def test_derive_score_status_receipt_stale_wins_over_run_drift():
    """If the receipt-commit snapshot is missing (receipt stale at
    authoring), that's the narrower signal — pick receipt_stale over
    run_commit_line_drift. Order of checks matters in
    ``_derive_score_status``."""
    assert grader_service_mod._derive_score_status(
        grade="no_match",
        source_snapshot_status="git_source_missing",
        run_snapshot_status="run_commit_hash_mismatch",
    ) == ("skipped_receipt_stale", "receipt_stale")


def test_derive_score_status_run_drift_requires_no_match():
    """Don't ever flip a pass-grade to skipped — pass-grades stay
    counted regardless of snapshot drift signals."""
    assert grader_service_mod._derive_score_status(
        grade="full_match",
        source_snapshot_status="git_source_hash_match",
        run_snapshot_status="run_commit_hash_mismatch",
    ) == ("counted", None)


def test_derive_score_status_run_drift_requires_receipt_match():
    """Don't flip to run_drift unless we have an explicit receipt-side
    match — otherwise the receipt itself could be the issue and we
    shouldn't attribute the miss to file movement."""
    # Receipt-side is mismatched (not just unmatched) — atlas's
    # interpretation of the rendering is suspect. Stays counted (the
    # row isn't covered by either skip path).
    assert grader_service_mod._derive_score_status(
        grade="no_match",
        source_snapshot_status="git_source_hash_mismatch",
        run_snapshot_status="run_commit_hash_mismatch",
    ) == ("counted", None)


def test_grade_one_populates_run_snapshot_drift_skip(daemon_config, tmp_path):
    """End-to-end: a receipt whose path/lines render correctly at the
    receipt commit but differ at the run commit should land
    score_status=skipped_run_commit_line_drift +
    clean_excluded_reason=run_commit_line_drift. (Acceptance #1 from
    Codex's PR #15 spec.)
    """
    import hashlib
    import subprocess as sp
    from dataclasses import replace
    from atlas_shadow.ingest_daemon import doc_resolver as doc_resolver_mod

    # Build a tiny git repo with two commits — second edits lines 2-3.
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True, timeout=10)
    src = repo / "src" / "example.py"
    src.parent.mkdir(parents=True)
    receipt_body = "line one\nline two\nline three\nline four\n"
    src.write_text(receipt_body, encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    sp.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, timeout=10)
    receipt_commit = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.strip()
    # Edit + commit.
    src.write_text("line one\nedited two\nedited three\nline four\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    sp.run(["git", "commit", "-q", "-m", "edit"], cwd=repo, check=True, timeout=10)
    run_commit = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.strip()

    sliced = "\n".join(receipt_body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    expected_sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()

    receipt = grader_service_mod.PacketReceipt(
        question_id="q12",
        question="what does line 2-3 of example.py do?",
        source_path="src/example.py",
        source_lines="2-3",
        source_commit=receipt_commit,
        excerpt_sha256=expected_sha,
        oracle_excerpt=sliced,
        oracle_claim="claim",
        command_text="scripts/qa_lookup.sh sed-range src/example.py 2 3",
    )

    cfg = replace(daemon_config, core_repo_path=repo)
    row = grader_service_mod._grade_one_receipt(
        cfg=cfg,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        run_commit=run_commit,
        _runner_run_one=_fake_runner_run_one(),
        # Atlas returns lines 2-3 of the run-commit content (the
        # "edited" version). The grader compares against the receipt's
        # original "line two/line three" claim, so it says no_match.
        _grader_grade=_fake_grader(grade="no_match", confidence=0.1, rationale="stub"),
        # PR #20: bypass command_snapshot's pre-atlas skip. This test
        # documents the post-atlas drift-detection path; the
        # command_text here ALSO satisfies the command-snapshot lane,
        # which would otherwise pre-empt the drift codepath.
        _classify_skip=lambda *_args, **_kwargs: None,
    )

    assert row.grade == "no_match"
    assert row.source_snapshot_status == "git_source_hash_match"
    assert row.run_snapshot_status == "run_commit_hash_mismatch"
    assert row.score_status == "skipped_run_commit_line_drift"
    assert row.clean_excluded_reason == "run_commit_line_drift"


def test_grade_one_both_snapshots_match_stays_counted(daemon_config, tmp_path):
    """If both snapshots match and grader still says no_match, the row
    stays counted — atlas is genuinely being measured on a stable
    receipt and missed."""
    import hashlib
    import subprocess as sp
    from dataclasses import replace
    from atlas_shadow.ingest_daemon import doc_resolver as doc_resolver_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True, timeout=10)
    src = repo / "src" / "example.py"
    src.parent.mkdir(parents=True)
    body = "line one\nline two\nline three\nline four\n"
    src.write_text(body, encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    sp.run(["git", "commit", "-q", "-m", "stable"], cwd=repo, check=True, timeout=10)
    commit = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.strip()

    sliced = "\n".join(body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    expected_sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()

    receipt = grader_service_mod.PacketReceipt(
        question_id="q1",
        question="what does line 2-3 do?",
        source_path="src/example.py",
        source_lines="2-3",
        source_commit=commit,
        excerpt_sha256=expected_sha,
        oracle_excerpt=sliced,
        oracle_claim="claim",
        command_text="scripts/qa_lookup.sh sed-range src/example.py 2 3",
    )

    cfg = replace(daemon_config, core_repo_path=repo)
    row = grader_service_mod._grade_one_receipt(
        cfg=cfg,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        run_commit=commit,  # same as receipt_commit — no drift
        _runner_run_one=_fake_runner_run_one(),
        _grader_grade=_fake_grader(grade="no_match", confidence=0.1, rationale="stub"),
        # PR #20: bypass command_snapshot pre-skip — see sibling
        # drift test for rationale.
        _classify_skip=lambda *_args, **_kwargs: None,
    )

    assert row.grade == "no_match"
    assert row.source_snapshot_status == "git_source_hash_match"
    assert row.run_snapshot_status == "run_commit_hash_match"
    # The clean denominator should still count this — it's a real Atlas miss.
    assert row.score_status == "counted"
    assert row.clean_excluded_reason is None


def test_serialize_row_includes_pr15_fields():
    """Per-packet JSON artifact must carry run_snapshot_* fields."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod
    from atlas_shadow.ingest_daemon import grader_service as gs

    row = pr_comment_mod.ReceiptGradingRow(
        question_id="q1", question="q1", grade="no_match",
        confidence=0.1, rationale="drift", tool="find_code",
        source_snapshot_status="git_source_hash_match",
        run_snapshot_status="run_commit_hash_mismatch",
        run_snapshot_hash_match=False,
        run_snapshot_sha256="deadbeef" * 8,
        score_status="skipped_run_commit_line_drift",
        clean_excluded_reason="run_commit_line_drift",
    )
    out = gs._serialize_row(row)
    assert out["run_snapshot_status"] == "run_commit_hash_mismatch"
    assert out["run_snapshot_hash_match"] is False
    assert out["run_snapshot_sha256"] == "deadbeef" * 8
    assert out["score_status"] == "skipped_run_commit_line_drift"


def test_grading_summary_skipped_run_commit_line_drift_count():
    """The new GradingSummary property counts only drift skips, not
    receipt-stale skips."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    rows = [
        pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q1", grade="full_match",
            confidence=0.9, rationale="ok", tool="find_code",
            score_status="counted",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q2", question="q2", grade="no_match",
            confidence=0.1, rationale="stale", tool="find_code",
            score_status="skipped_receipt_stale",
            clean_excluded_reason="receipt_stale",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q3", question="q3", grade="no_match",
            confidence=0.1, rationale="drift", tool="find_code",
            score_status="skipped_run_commit_line_drift",
            clean_excluded_reason="run_commit_line_drift",
        ),
    ]
    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=rows,
    )
    assert s.skipped_receipt_stale_count == 1
    assert s.skipped_run_commit_line_drift_count == 1
    assert s.excluded_count == 2
    assert s.clean_total == 1  # only the passing row counts
    assert s.clean_pass_pct == 100


def test_grade_one_populates_lane_and_score_status_for_stale_receipt(daemon_config):
    """End-to-end through _grade_one_receipt: a code receipt whose
    cited source can't be materialized at source_commit hits the
    PR #17 pre-atlas skip with the new ``skipped_unavailable_source_ref``
    status. (Supersedes PR #14's ``skipped_receipt_stale`` — same
    underlying condition, caught earlier and emitted under the new
    name.) The pre-atlas path uses ``lane=non_retrieval`` because we
    never went through find_code/scan_search/doc_resolver.
    """
    receipt = grader_service_mod.PacketReceipt(
        question_id="q1",
        question="missing.py exists?",
        source_path="missing-at-head.py",  # doesn't exist → git_source_missing
        source_lines="10-20",
        source_commit=BASE_SHA,
        oracle_excerpt="excerpt",
        oracle_claim="claim",
        command_text="scripts/qa_lookup.sh sed-range missing-at-head.py 10 20",
    )

    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        _runner_run_one=_fake_runner_run_one(),
        _grader_grade=_fake_grader(grade="no_match", confidence=0.1, rationale="stub"),
    )

    # PR #17: the row is constructed by ``_build_pre_atlas_skip_row``
    # (we never reached the runner stub). Grade enum stays narrow —
    # the skip uses ``atlas_not_found`` to mean "atlas returned
    # nothing" (true: we didn't ask).
    assert row.grade == "atlas_not_found"
    assert row.source_snapshot_status == "git_source_missing"
    assert row.lane == "non_retrieval"
    assert row.tool == "skipped"
    assert row.score_status == "skipped_unavailable_source_ref"
    assert row.clean_excluded_reason == "unavailable_source_ref"


def test_grade_one_populates_lane_and_counted_for_passing_receipt(daemon_config):
    """A passing find_code receipt should land score_status=counted
    regardless of snapshot status — pass-grades are never flipped.

    With PR #17, a source-missing receipt now pre-atlas-skips
    BEFORE the grader runs (so the "grader returns full_match"
    scenario never reaches the row). To preserve the original
    test intent — verifying pass-grades aren't accidentally
    flipped by snapshot-derived bookkeeping — we pass
    ``_classify_skip=lambda *_, **__: None`` to bypass the
    pre-atlas skip and exercise the post-atlas path.
    """
    receipt = grader_service_mod.PacketReceipt(
        question_id="q1",
        question="ok?",
        source_path="missing-at-head.py",
        source_lines="10-20",
        source_commit=BASE_SHA,
        oracle_excerpt="excerpt",
        oracle_claim="claim",
        command_text="scripts/qa_lookup.sh sed-range missing-at-head.py 10 20",
    )

    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        _runner_run_one=_fake_runner_run_one(),
        _grader_grade=_fake_grader(grade="full_match", confidence=0.95, rationale="ok"),
        _classify_skip=lambda *_args, **_kwargs: None,
    )

    assert row.grade == "full_match"
    assert row.lane == "explicit_source_fast_path"
    assert row.score_status == "counted"
    assert row.clean_excluded_reason is None


def test_grading_summary_clean_pass_pct_basic():
    """Two passing + one stale-skip → raw 67%, clean 100% (skip removed
    from both numerator and denominator)."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    rows = [
        pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q1", grade="full_match",
            confidence=0.9, rationale="ok", tool="find_code",
            lane="explicit_source_fast_path", score_status="counted",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q2", question="q2", grade="partial_match",
            confidence=0.7, rationale="ok", tool="find_code",
            lane="explicit_source_fast_path", score_status="counted",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q3", question="q3", grade="no_match",
            confidence=0.1, rationale="stale", tool="find_code",
            lane="explicit_source_fast_path",
            score_status="skipped_receipt_stale",
            clean_excluded_reason="receipt_stale",
            source_snapshot_status="git_source_missing",
        ),
    ]
    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=rows,
    )
    assert s.total == 3
    assert s.pass_count == 2
    assert s.pass_pct == 67  # raw
    assert s.excluded_count == 1
    assert s.skipped_receipt_stale_count == 1
    assert s.clean_total == 2
    assert s.clean_pass_pct == 100  # clean denominator removes the skip


def test_grading_summary_clean_pass_pct_none_when_all_excluded():
    """clean_pass_pct returns None (not 0) when every row is excluded —
    the score is undefined, not zero."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    rows = [
        pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q1", grade="no_match",
            confidence=0.0, rationale="stale", tool="find_code",
            score_status="skipped_receipt_stale",
            clean_excluded_reason="receipt_stale",
        ),
    ]
    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=rows,
    )
    assert s.total == 1
    assert s.pass_count == 0
    assert s.clean_total == 0
    assert s.clean_pass_pct is None  # explicit "undefined", not 0


def test_grading_summary_back_compat_no_new_fields():
    """Rows that don't set score_status default to ``counted`` — old
    test fixtures and legacy code paths shouldn't see behavior shifts.
    """
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    rows = [
        pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q1", grade="full_match",
            confidence=0.9, rationale="ok", tool="find_code",
        ),  # no score_status / lane / clean_excluded_reason
    ]
    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=rows,
    )
    assert s.excluded_count == 0
    assert s.skipped_receipt_stale_count == 0
    assert s.clean_total == 1
    assert s.clean_pass_pct == 100


# ─── PR #16: raw_result diagnostics threading ─────────────────────────


def test_atlas_raw_result_diagnostics_extracts_full_payload():
    """Happy path: a complete raw_result yields all five fields
    populated."""
    raw = {
        "citations": [
            {"file_path": "core/ai.py", "line_start": 100, "line_end": 120},
            {"file_path": "core/query.py", "line_start": 200, "line_end": 250},
            {"file_path": "core/util.py"},  # no lines
        ],
        "retrieval_plan": {
            "lanes_run": ["symbol_exact", "vector"],
            "boosts": ["symbol_exact"],
        },
        "reranker_trace": {
            "candidates_considered": 56,
            "top_k": [{"chunk": {}}, {"chunk": {}}, {"chunk": {}}],
        },
    }
    out = grader_service_mod._atlas_raw_result_diagnostics(raw)
    assert out["retrieval_plan"]["lanes_run"] == ["symbol_exact", "vector"]
    assert out["citation_locations"] == [
        "core/ai.py:100-120",
        "core/query.py:200-250",
        "core/util.py",  # bare path when lines absent
    ]
    assert out["citation_count"] == 3
    assert out["reranker_candidates_considered"] == 56
    assert out["reranker_top_k_count"] == 3


def test_atlas_raw_result_diagnostics_handles_none():
    """A None raw_result (doc_resolver path; or a runner that didn't
    pass raw_result through) returns all-Nones / empty list."""
    out = grader_service_mod._atlas_raw_result_diagnostics(None)
    assert out["retrieval_plan"] is None
    assert out["citation_locations"] == []
    assert out["citation_count"] is None
    assert out["reranker_candidates_considered"] is None
    assert out["reranker_top_k_count"] is None


def test_atlas_raw_result_diagnostics_handles_malformed_shapes():
    """Robustness: any field that isn't the expected shape collapses
    to safe defaults rather than raising. The grader's exception path
    is the safety net but diagnostics extraction should never itself
    be the cause of a row-construction failure."""
    raw = {
        "citations": "not-a-list",
        "retrieval_plan": "not-a-dict",
        "reranker_trace": ["not-a-dict"],
    }
    out = grader_service_mod._atlas_raw_result_diagnostics(raw)
    assert out["retrieval_plan"] is None
    assert out["citation_locations"] == []
    assert out["citation_count"] is None
    assert out["reranker_candidates_considered"] is None
    assert out["reranker_top_k_count"] is None


def test_atlas_raw_result_diagnostics_truncates_citations_to_20():
    """Artifact JSON stays tight: only the first 20 citations land in
    the compact list, but citation_count records the full total so
    consumers can tell when truncation happened."""
    raw = {
        "citations": [
            {"file_path": f"f{i}.py", "line_start": i, "line_end": i + 1}
            for i in range(50)
        ],
    }
    out = grader_service_mod._atlas_raw_result_diagnostics(raw)
    assert len(out["citation_locations"]) == 20
    assert out["citation_count"] == 50


def test_atlas_raw_result_diagnostics_skips_non_dict_citation_entries():
    """A citation entry that isn't a dict gets dropped, doesn't raise,
    doesn't shift the citation_count (which is the raw length)."""
    raw = {
        "citations": [
            {"file_path": "a.py", "line_start": 1, "line_end": 2},
            "string-instead-of-dict",
            {"file_path": "b.py", "line_start": 3, "line_end": 4},
        ],
    }
    out = grader_service_mod._atlas_raw_result_diagnostics(raw)
    assert out["citation_locations"] == ["a.py:1-2", "b.py:3-4"]
    assert out["citation_count"] == 3  # all three contribute to the count


def test_grade_one_populates_raw_result_fields_on_code_path(daemon_config, tmp_path):
    """End-to-end on the code path: when the runner stub returns a
    raw_result with retrieval_plan + citations + reranker_trace,
    those fields land on the row."""
    from atlas_shadow.runner import AtlasResponse, ShadowResponse

    def runner_with_raw_result(receipt, **kwargs):
        return ShadowResponse(
            question_id=receipt.question_id,
            question=receipt.question,
            fixture_id="pr-packet",
            atlas_response=AtlasResponse(
                tool_used=kwargs.get("tool", "find_code"),
                answer_text="stub",
                raw_result={
                    "citations": [
                        {"file_path": "core/ai.py", "line_start": 50, "line_end": 75},
                    ],
                    "retrieval_plan": {"lanes_run": ["symbol_exact"]},
                    "reranker_trace": {
                        "candidates_considered": 42,
                        "top_k": [{}, {}],
                    },
                },
                evidence_keys=[],
                atlas_latency_ms=10,
                request_id="r",
                commit="",
            ),
            wall_time_ms=20,
            captured_at="2026-05-19T00:00:00Z",
            org_id=kwargs.get("org_id", ""),
            tool=kwargs.get("tool", "find_code"),
        )

    receipt = grader_service_mod.PacketReceipt(
        question_id="q1",
        question="q",
        source_path="core/ai.py",
        source_lines="50-75",
        source_commit=BASE_SHA,
        oracle_excerpt="e",
        oracle_claim="c",
        command_text="scripts/qa_lookup.sh sed-range core/ai.py 50 75",
    )

    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        _runner_run_one=runner_with_raw_result,
        _grader_grade=_fake_grader(grade="full_match", confidence=0.9, rationale="ok"),
        # PR #17: test focuses on raw_result field population. Bypass
        # the pre-atlas skip so we still exercise the runner path even
        # though the test's core_repo_path is non-existent.
        _classify_skip=lambda *_args, **_kwargs: None,
    )
    assert row.atlas_retrieval_plan == {"lanes_run": ["symbol_exact"]}
    assert row.atlas_citation_locations == ["core/ai.py:50-75"]
    assert row.atlas_citation_count == 1
    assert row.atlas_reranker_candidates_considered == 42
    assert row.atlas_reranker_top_k_count == 2


def test_grade_one_raw_result_none_on_doc_resolver_path(daemon_config):
    """doc_resolver path doesn't go through workspace_atlas_query, so
    raw_result fields land as the explicit "no diagnostics available"
    defaults (None / empty list)."""
    receipt = grader_service_mod.PacketReceipt(
        question_id="q1",
        question="q",
        source_path="Atlas/docs/spec.md",  # .md routes to doc_resolver
        source_lines="1-20",
        source_commit=BASE_SHA,
        oracle_excerpt="e",
        oracle_claim="c",
    )

    def stub_resolver(receipt, **_kwargs):
        from atlas_shadow.ingest_daemon.doc_resolver import DocResolverResult
        return DocResolverResult(
            status="ok",
            revision_binding="db_commit_scoped",
            raw_text="stub doc",
            artifact_id=None,
            chunk_id=None,
            heading_path=None,
            warnings=[],
        )

    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        _doc_resolver=stub_resolver,
        _grader_grade=_fake_grader(grade="full_match", confidence=0.9, rationale="ok"),
        # PR #17: bypass pre-atlas skip — test focuses on the
        # doc_resolver-path raw_result defaults.
        _classify_skip=lambda *_args, **_kwargs: None,
    )
    assert row.tool == "doc_resolver"
    assert row.atlas_retrieval_plan is None
    assert row.atlas_citation_locations == []
    assert row.atlas_citation_count is None
    assert row.atlas_reranker_candidates_considered is None
    assert row.atlas_reranker_top_k_count is None


def test_serialize_row_includes_pr16_fields():
    """The per-packet JSON artifact must carry the new raw_result
    fields so the diagnostic classifier can consume them."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod
    from atlas_shadow.ingest_daemon import grader_service as gs

    row = pr_comment_mod.ReceiptGradingRow(
        question_id="q1", question="q1", grade="no_match",
        confidence=0.1, rationale="r", tool="find_code",
        atlas_retrieval_plan={"lanes_run": ["vector"]},
        atlas_citation_locations=["core/ai.py:1-10", "core/q.py:50-60"],
        atlas_citation_count=2,
        atlas_reranker_candidates_considered=10,
        atlas_reranker_top_k_count=5,
    )
    out = gs._serialize_row(row)
    assert out["atlas_retrieval_plan"] == {"lanes_run": ["vector"]}
    assert out["atlas_citation_locations"] == [
        "core/ai.py:1-10", "core/q.py:50-60",
    ]
    assert out["atlas_citation_count"] == 2
    assert out["atlas_reranker_candidates_considered"] == 10
    assert out["atlas_reranker_top_k_count"] == 5


def test_serialize_row_includes_pr14_fields():
    """The per-packet JSON artifact must carry lane / score_status /
    clean_excluded_reason so downstream classifiers can apply the
    clean filter without re-inferring."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod
    from atlas_shadow.ingest_daemon import grader_service as gs

    row = pr_comment_mod.ReceiptGradingRow(
        question_id="q1", question="q1", grade="no_match",
        confidence=0.1, rationale="stale", tool="find_code",
        lane="explicit_source_fast_path",
        score_status="skipped_receipt_stale",
        clean_excluded_reason="receipt_stale",
        source_snapshot_status="git_source_missing",
    )
    out = gs._serialize_row(row)
    assert out["lane"] == "explicit_source_fast_path"
    assert out["score_status"] == "skipped_receipt_stale"
    assert out["clean_excluded_reason"] == "receipt_stale"
    assert out["source_snapshot_status"] == "git_source_missing"


# ─── PR #17: non-retrieval skip categories ────────────────────────────


def _mk_skip_receipt(*, evidence_type=None, source_path=None, source_lines=None,
                     source_commit=None, command_text=""):
    """Compact receipt builder for the skip-classifier tests."""
    return grader_service_mod.PacketReceipt(
        question_id="q",
        question="q",
        oracle_claim="c",
        oracle_excerpt="e",
        evidence_type=evidence_type,
        source_path=source_path,
        source_lines=source_lines,
        source_commit=source_commit,
        command_text=command_text,
    )


def test_classify_pre_atlas_skip_external_tool_docs():
    r = _mk_skip_receipt(evidence_type="external_tool_docs")
    assert grader_service_mod._classify_pre_atlas_skip(r) == (
        "skipped_non_repo_evidence", "non_repo_evidence",
    )


def test_classify_pre_atlas_skip_user_context():
    r = _mk_skip_receipt(evidence_type="user_context")
    assert grader_service_mod._classify_pre_atlas_skip(r) == (
        "skipped_non_repo_evidence", "non_repo_evidence",
    )


def test_classify_pre_atlas_skip_absence_search():
    r = _mk_skip_receipt(evidence_type="absence_search")
    assert grader_service_mod._classify_pre_atlas_skip(r) == (
        "skipped_absence_search", "absence_search",
    )


def test_classify_pre_atlas_skip_docs_work_excluded():
    """A source_path in docs/work/** triggers the corpus-exclusion
    skip regardless of evidence_type or snapshot state."""
    r = _mk_skip_receipt(
        evidence_type="source_excerpt",
        source_path="products/tandem/packages/python/atlas/docs/work/2026-05-01-x/04-postmortem.md",
    )
    assert grader_service_mod._classify_pre_atlas_skip(r) == (
        "skipped_doc_corpus_excluded", "doc_corpus_excluded",
    )


def test_classify_pre_atlas_skip_unavailable_source_ref():
    """When the snapshot resolver returns git_source_missing, the
    receipt's cited source can't be materialized — skip pre-atlas."""
    from atlas_shadow.ingest_daemon.code_snapshot import (
        CodeSnapshotResult, STATUS_SOURCE_MISSING,
    )
    r = _mk_skip_receipt(
        evidence_type="source_excerpt",
        source_path="core/missing.py",
        source_lines="1-10",
        source_commit=BASE_SHA,
    )
    snap = CodeSnapshotResult(status=STATUS_SOURCE_MISSING)
    assert grader_service_mod._classify_pre_atlas_skip(
        r, source_snapshot=snap,
    ) == ("skipped_unavailable_source_ref", "unavailable_source_ref")


def test_classify_pre_atlas_skip_returns_none_for_normal_receipt():
    """A source_excerpt receipt with a materializable snapshot doesn't
    qualify for any skip — falls through to normal atlas routing."""
    from atlas_shadow.ingest_daemon.code_snapshot import (
        CodeSnapshotResult, STATUS_MATCH,
    )
    r = _mk_skip_receipt(
        evidence_type="source_excerpt",
        source_path="core/ai.py",
        source_lines="100-110",
        source_commit=BASE_SHA,
    )
    snap = CodeSnapshotResult(status=STATUS_MATCH, hash_match=True)
    assert grader_service_mod._classify_pre_atlas_skip(
        r, source_snapshot=snap,
    ) is None


def test_classify_pre_atlas_command_source_missing_does_not_override_snapshot_match():
    """A command path can be package-relative while code_snapshot can
    resolve the source_ref through Atlas leaf aliases. In that case a
    command_snapshot source-missing result must not pre-skip the row as
    unavailable; let atlas grading run.
    """
    from types import SimpleNamespace
    from atlas_shadow.ingest_daemon import command_snapshot as command_snapshot_mod
    from atlas_shadow.ingest_daemon.code_snapshot import (
        CodeSnapshotResult, STATUS_MATCH,
    )
    r = _mk_skip_receipt(
        evidence_type="source_excerpt",
        source_path="core/ai.py",
        source_lines="100-110",
        source_commit=BASE_SHA,
        command_text="scripts/qa_lookup.sh sed-range core/ai.py 100 110",
    )
    snap = CodeSnapshotResult(status=STATUS_MATCH, hash_match=True)
    cmd = SimpleNamespace(status=command_snapshot_mod.STATUS_SOURCE_MISSING)
    assert grader_service_mod._classify_pre_atlas_skip(
        r, source_snapshot=snap, command_snapshot=cmd,
    ) is None


def test_classify_pre_atlas_skip_priority_docs_work_beats_evidence_type():
    """docs/work/** check runs first — even if evidence_type would
    qualify for another skip, the path-based exclusion wins (it's the
    most specific upstream policy)."""
    r = _mk_skip_receipt(
        evidence_type="absence_search",
        source_path="docs/work/2026-05-01-x/02-qna-log.md",
    )
    result = grader_service_mod._classify_pre_atlas_skip(r)
    assert result[0] == "skipped_doc_corpus_excluded"


def test_grade_one_short_circuits_external_tool_docs(daemon_config):
    """End-to-end: an external_tool_docs receipt bypasses runner +
    doc_resolver entirely. Lane = non_retrieval, tool = "skipped"."""
    receipt = _mk_skip_receipt(evidence_type="external_tool_docs")

    runner_called = []

    def runner_should_not_fire(*args, **kwargs):
        runner_called.append(True)
        raise AssertionError("runner should not be called for skipped receipt")

    def resolver_should_not_fire(*args, **kwargs):
        runner_called.append(True)
        raise AssertionError("doc_resolver should not be called for skipped receipt")

    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        _runner_run_one=runner_should_not_fire,
        _doc_resolver=resolver_should_not_fire,
    )
    assert runner_called == []
    assert row.grade == "atlas_not_found"
    assert row.tool == "skipped"
    assert row.lane == "non_retrieval"
    assert row.score_status == "skipped_non_repo_evidence"
    assert row.clean_excluded_reason == "non_repo_evidence"
    assert row.evidence_type == "external_tool_docs"


def test_grade_one_short_circuits_absence_search(daemon_config):
    receipt = _mk_skip_receipt(evidence_type="absence_search")
    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
    )
    assert row.score_status == "skipped_absence_search"
    assert row.clean_excluded_reason == "absence_search"


def test_grade_one_short_circuits_docs_work_excluded(daemon_config):
    receipt = _mk_skip_receipt(
        evidence_type="source_excerpt",
        source_path="docs/work/2026-05-01-x/04-postmortem.md",
    )
    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
    )
    assert row.score_status == "skipped_doc_corpus_excluded"
    assert row.clean_excluded_reason == "doc_corpus_excluded"


def test_grading_summary_pr17_skip_counts():
    """All four PR #17 skip counts isolate correctly when rows
    sample each category."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    rows = [
        pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q1", grade="atlas_not_found",
            confidence=1.0, rationale="r", tool="skipped",
            score_status="skipped_non_repo_evidence",
            clean_excluded_reason="non_repo_evidence",
            evidence_type="external_tool_docs",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q2", question="q2", grade="atlas_not_found",
            confidence=1.0, rationale="r", tool="skipped",
            score_status="skipped_absence_search",
            clean_excluded_reason="absence_search",
            evidence_type="absence_search",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q3", question="q3", grade="atlas_not_found",
            confidence=1.0, rationale="r", tool="skipped",
            score_status="skipped_unavailable_source_ref",
            clean_excluded_reason="unavailable_source_ref",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q4", question="q4", grade="atlas_not_found",
            confidence=1.0, rationale="r", tool="skipped",
            score_status="skipped_doc_corpus_excluded",
            clean_excluded_reason="doc_corpus_excluded",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q5", question="q5", grade="full_match",
            confidence=0.9, rationale="ok", tool="find_code",
            score_status="counted",
        ),
    ]
    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=rows,
    )
    assert s.skipped_non_repo_evidence_count == 1
    assert s.skipped_absence_search_count == 1
    assert s.skipped_unavailable_source_ref_count == 1
    assert s.skipped_doc_corpus_excluded_count == 1
    assert s.excluded_count == 4
    assert s.clean_total == 1
    assert s.clean_pass_pct == 100


def test_serialize_row_includes_evidence_type():
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod
    from atlas_shadow.ingest_daemon import grader_service as gs

    row = pr_comment_mod.ReceiptGradingRow(
        question_id="q", question="q", grade="atlas_not_found",
        confidence=1.0, rationale="r", tool="skipped",
        evidence_type="absence_search",
        score_status="skipped_absence_search",
    )
    out = gs._serialize_row(row)
    assert out["evidence_type"] == "absence_search"
    assert out["score_status"] == "skipped_absence_search"


# ─── by_evidence_type breakdown (per-evidence-type rollup) ────────────


def test_grading_summary_by_evidence_type_partitions_rows():
    """Every row lands in exactly one bucket; counts + clean math
    match the per-bucket denominators."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    rows = [
        # source_excerpt: 2 receipts, 1 correct, 0 excluded → clean_pct=50.0
        pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q1", grade="full_match",
            confidence=0.9, rationale="ok", tool="find_code",
            evidence_type="source_excerpt",
            score_status="counted",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q2", question="q2", grade="no_match",
            confidence=1.0, rationale="missed", tool="find_code",
            evidence_type="source_excerpt",
            score_status="counted",
        ),
        # external_tool_docs: 1 receipt, all excluded → clean_total=0, clean_pct=None
        pr_comment_mod.ReceiptGradingRow(
            question_id="q3", question="q3", grade="atlas_not_found",
            confidence=1.0, rationale="r", tool="skipped",
            evidence_type="external_tool_docs",
            score_status="skipped_non_repo_evidence",
        ),
        # absence_search: 1 receipt excluded
        pr_comment_mod.ReceiptGradingRow(
            question_id="q4", question="q4", grade="atlas_not_found",
            confidence=1.0, rationale="r", tool="skipped",
            evidence_type="absence_search",
            score_status="skipped_absence_search",
        ),
        # None evidence_type collapses to source_excerpt bucket
        # (matches grader_service's routing default).
        pr_comment_mod.ReceiptGradingRow(
            question_id="q5", question="q5", grade="partial_match",
            confidence=0.7, rationale="meh", tool="find_code",
            evidence_type=None,
            score_status="counted",
        ),
    ]
    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=rows,
    )
    bd = s.by_evidence_type

    assert bd["source_excerpt"]["receipts"] == 3
    assert bd["source_excerpt"]["correct"] == 2  # q1 + q5
    assert bd["source_excerpt"]["excluded"] == 0
    assert bd["source_excerpt"]["clean_total"] == 3
    # 2/3 rounded to 1dp == 66.7
    assert bd["source_excerpt"]["clean_pct"] == 66.7

    assert bd["external_tool_docs"]["receipts"] == 1
    assert bd["external_tool_docs"]["excluded"] == 1
    assert bd["external_tool_docs"]["clean_total"] == 0
    assert bd["external_tool_docs"]["clean_pct"] is None

    assert bd["absence_search"]["receipts"] == 1
    assert bd["absence_search"]["clean_total"] == 0
    assert bd["absence_search"]["clean_pct"] is None

    # user_context bucket gets zero-fill so consumers can index it
    # consistently across runs.
    assert bd["user_context"]["receipts"] == 0
    assert bd["user_context"]["clean_total"] == 0
    assert bd["user_context"]["clean_pct"] is None

    # Receipts across buckets sum to row count (no double counting).
    total = sum(b["receipts"] for b in bd.values())
    assert total == len(rows)


def test_grading_summary_by_evidence_type_unknown_value_routes_to_other():
    """Future evidence_type values land in 'other' rather than
    silently corrupting one of the canonical buckets."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    rows = [
        pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q1", grade="full_match",
            confidence=0.9, rationale="ok", tool="find_code",
            evidence_type="some_future_type",
            score_status="counted",
        ),
    ]
    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=rows,
    )
    bd = s.by_evidence_type
    assert bd["other"]["receipts"] == 1
    assert bd["other"]["correct"] == 1
    assert bd["other"]["clean_pct"] == 100.0
    # Canonical buckets stayed empty — no leakage.
    assert bd["source_excerpt"]["receipts"] == 0


def test_grading_summary_by_evidence_type_empty_summary():
    """No rows → every bucket zero-filled, no exceptions."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=[],
    )
    bd = s.by_evidence_type
    for bucket in (
        "source_excerpt", "external_tool_docs",
        "user_context", "absence_search", "other",
    ):
        assert bd[bucket]["receipts"] == 0
        assert bd[bucket]["clean_total"] == 0
        assert bd[bucket]["clean_pct"] is None


# ─── PR #18 review fix: doc receipts defer source-unavailable to doc_resolver ───


def test_classify_pre_atlas_skip_doc_receipt_with_git_source_missing_not_pre_skipped():
    """PR #18 review (Codex): a DocQuery receipt whose source_path
    doesn't render in raw git should NOT be pre-skipped. Doc receipts
    defer to doc_resolver so DB-based + alias-aware resolution can
    run first."""
    from atlas_shadow.ingest_daemon.code_snapshot import (
        CodeSnapshotResult, STATUS_SOURCE_MISSING,
    )
    from atlas_shadow.ingest_daemon.grader_service import DocQuery

    r = _mk_skip_receipt(
        evidence_type="source_excerpt",
        source_path="Atlas/docs/specs/instruction-memory-v1.md",  # alias-able
        source_lines="1-20",
        source_commit=BASE_SHA,
    )
    snap = CodeSnapshotResult(status=STATUS_SOURCE_MISSING)
    translation = DocQuery(receipt=r)
    assert grader_service_mod._classify_pre_atlas_skip(
        r, source_snapshot=snap, translation=translation,
    ) is None  # NOT pre-skipped — doc_resolver gets a chance


def test_classify_pre_atlas_skip_code_receipt_with_git_source_missing_pre_skipped():
    """Code receipts still get pre-skipped on git_source_missing —
    the review fix only changed doc-receipt behavior."""
    from atlas_shadow.ingest_daemon.code_snapshot import (
        CodeSnapshotResult, STATUS_SOURCE_MISSING,
    )
    from atlas_shadow.ingest_daemon.grader_service import CodeQuery

    r = _mk_skip_receipt(
        evidence_type="source_excerpt",
        source_path="core/missing.py",
        source_lines="1-10",
        source_commit=BASE_SHA,
        command_text="scripts/qa_lookup.sh sed-range core/missing.py 1 10",
    )
    snap = CodeSnapshotResult(status=STATUS_SOURCE_MISSING)
    translation = CodeQuery(tool="find_code", question="q", receipt=r)
    assert grader_service_mod._classify_pre_atlas_skip(
        r, source_snapshot=snap, translation=translation,
    ) == ("skipped_unavailable_source_ref", "unavailable_source_ref")


def test_derive_score_status_doc_unresolved_source_ref_post_resolver():
    """Doc receipts that doc_resolver returns the unresolved binding
    for land in skipped_unavailable_source_ref post-grading. Captures
    the case where the alias-aware resolver tried and still couldn't
    materialize the source.

    PR #19 fix: doc_resolver emits ``BINDING_NONE = "none"`` (string)
    on the row's ``revision_binding`` field — not the longer
    ``"unresolved_source_ref"`` status name. _derive_score_status now
    accepts both for forward compat.
    """
    # Real-world value emitted by doc_resolver:
    assert grader_service_mod._derive_score_status(
        grade="atlas_not_found",
        source_snapshot_status="git_source_missing",
        revision_binding="none",  # actual BINDING_NONE value
    ) == ("skipped_unavailable_source_ref", "unavailable_source_ref")
    # Forward-compat: also accept the longer status form
    assert grader_service_mod._derive_score_status(
        grade="atlas_not_found",
        source_snapshot_status="git_source_missing",
        revision_binding="unresolved_source_ref",
    ) == ("skipped_unavailable_source_ref", "unavailable_source_ref")


def test_derive_score_status_doc_binding_none_with_clean_snapshot_still_skips():
    """PR #19 regression: q10-shape — doc_resolver returned ``"none"``
    binding (couldn't resolve) but source_snapshot_status is
    ``"no_line_range"`` (not git_source_missing). The legacy fallback
    branch would have left this counted; PR #19 catches it as
    skipped_unavailable_source_ref because the resolver explicitly
    said no.
    """
    assert grader_service_mod._derive_score_status(
        grade="atlas_not_found",
        source_snapshot_status="no_line_range",
        revision_binding="none",
    ) == ("skipped_unavailable_source_ref", "unavailable_source_ref")


def test_derive_score_status_doc_db_resolved_stays_counted_despite_git_missing():
    """The case Codex's doc-alias PR enables: receipt path doesn't
    render in raw git (alias path), but doc_resolver resolves via DB
    using the canonical path. We MUST NOT pre-skip these as
    unavailable — they're real atlas measurements."""
    assert grader_service_mod._derive_score_status(
        grade="no_match",  # atlas missed the doc
        source_snapshot_status="git_source_missing",  # raw git says missing
        revision_binding="db_commit_scoped",  # resolver found it via DB
    ) == ("counted", None)


def test_derive_score_status_doc_git_fallback_stays_counted():
    """``git_receipt_snapshot`` binding means doc_resolver fell back
    to a git-side lookup that DID resolve. Still a real measurement,
    not a skip."""
    assert grader_service_mod._derive_score_status(
        grade="no_match",
        source_snapshot_status="git_source_hash_match",
        revision_binding="git_receipt_snapshot",
    ) == ("counted", None)


# ─── PR #20: command-snapshot lane integration ───────────────────────


def test_grade_one_command_snapshot_short_circuits_atlas(daemon_config, tmp_path):
    """End-to-end: a receipt with a whitelisted ``sed-range`` command
    AND a matching excerpt_sha256 gets resolved by command_snapshot
    BEFORE atlas dispatch. Asserts the runner is NOT called.
    """
    import hashlib
    import subprocess as sp
    from dataclasses import replace
    from atlas_shadow.ingest_daemon import doc_resolver as doc_resolver_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True, timeout=10)
    src = repo / "src" / "example.py"
    src.parent.mkdir(parents=True)
    body = "line one\nline two\nline three\nline four\n"
    src.write_text(body, encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    sp.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, timeout=10)
    commit = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.strip()

    sliced = "\n".join(body.splitlines()[1:3])
    canon = doc_resolver_mod._excerpt_canonical(sliced)
    expected_sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()

    receipt = grader_service_mod.PacketReceipt(
        question_id="q1",
        question="cmd-snapshot test",
        source_path="src/example.py",
        source_lines="2-3",
        source_commit=commit,
        excerpt_sha256=expected_sha,
        oracle_excerpt=sliced,
        oracle_claim="claim",
        command_text="scripts/qa_lookup.sh sed-range src/example.py 2 3",
    )

    def runner_should_not_fire(*args, **kwargs):
        raise AssertionError("atlas runner should not be called for command-snapshot row")

    def resolver_should_not_fire(*args, **kwargs):
        raise AssertionError("doc_resolver should not be called for command-snapshot row")

    cfg = replace(daemon_config, core_repo_path=repo)
    row = grader_service_mod._grade_one_receipt(
        cfg=cfg,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        run_commit=commit,
        _runner_run_one=runner_should_not_fire,
        _doc_resolver=resolver_should_not_fire,
    )
    assert row.score_status == "skipped_command_snapshot"
    assert row.clean_excluded_reason == "command_snapshot"
    assert row.lane == "non_retrieval"
    assert row.tool == "skipped"
    assert row.command_snapshot_status == "command_snapshot_match"
    assert row.command_snapshot_hash_match is True


def test_grade_one_command_snapshot_absence_search_verified(daemon_config, tmp_path):
    """Acceptance target: q17-shape — absence_search + grep command
    where the pattern is absent in the searched paths → row lands as
    skipped_command_snapshot with the no_match_expected_absent inner
    status."""
    import subprocess as sp
    from dataclasses import replace

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True, timeout=10)
    src_dir = repo / "core"
    src_dir.mkdir(parents=True)
    (src_dir / "x.py").write_text("def x():\n    pass\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    sp.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, timeout=10)
    commit = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.strip()

    receipt = grader_service_mod.PacketReceipt(
        question_id="q17",
        question="no heading_path anywhere",
        oracle_claim="no occurrences of heading_path in core or tests",
        oracle_excerpt="",
        evidence_type="absence_search",
        source_commit=commit,
        command_text='scripts/qa_lookup.sh grep "heading_path" core tests',
    )

    cfg = replace(daemon_config, core_repo_path=repo)
    row = grader_service_mod._grade_one_receipt(
        cfg=cfg,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
    )
    assert row.score_status == "skipped_command_snapshot"
    assert row.command_snapshot_status == "command_snapshot_no_match_expected_absent"
    assert row.lane == "non_retrieval"


def test_grade_one_synthesized_command_for_directory_receipt(daemon_config, tmp_path):
    """Acceptance target: q12 shape — trailing-slash directory path
    with empty command_text gets synthesized ``ls`` and lands as
    skipped_command_snapshot.

    Note: q10's Makefile (file path, no trailing slash) is
    deliberately out of scope for v1 — file paths without explicit
    command_text continue to route through atlas to preserve existing
    measurements. q10 → command_snapshot will require an explicit
    ``command_text`` (e.g. ``wc -l Makefile``) on the receipt.
    """
    import subprocess as sp
    from dataclasses import replace

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True, timeout=10)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "build.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    sp.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, timeout=10)
    commit = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.strip()

    receipt = grader_service_mod.PacketReceipt(
        question_id="q12",
        question="scripts/ directory shape",
        oracle_claim="scripts/ directory exists",
        oracle_excerpt="",
        source_path="scripts/",  # trailing slash → synthesizes ls
        source_commit=commit,
        command_text="",
    )
    cfg = replace(daemon_config, core_repo_path=repo)
    row = grader_service_mod._grade_one_receipt(
        cfg=cfg,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
    )
    assert row.score_status == "skipped_command_snapshot"
    assert row.command_snapshot_status == "command_snapshot_match"
    assert row.lane == "non_retrieval"


def test_grade_one_command_snapshot_mismatch_to_unavailable(daemon_config, tmp_path):
    """A receipt whose sed-range hash doesn't match the actual repo
    content lands as skipped_unavailable_source_ref (contradicted —
    atlas wasn't being tested fairly)."""
    import subprocess as sp
    from dataclasses import replace

    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True, timeout=10)
    sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True, timeout=10)
    (repo / "src.py").write_text("hello\n", encoding="utf-8")
    sp.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    sp.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, timeout=10)
    commit = sp.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, check=True, timeout=10,
    ).stdout.strip()

    receipt = grader_service_mod.PacketReceipt(
        question_id="q",
        question="q",
        oracle_claim="c",
        oracle_excerpt="",
        source_commit=commit,
        excerpt_sha256="0" * 64,  # intentionally wrong
        command_text="scripts/qa_lookup.sh sed-range src.py 1 1",
    )
    cfg = replace(daemon_config, core_repo_path=repo)
    row = grader_service_mod._grade_one_receipt(
        cfg=cfg,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
    )
    assert row.score_status == "skipped_unavailable_source_ref"
    assert row.clean_excluded_reason == "unavailable_source_ref"
    assert row.command_snapshot_status == "command_snapshot_mismatch"


def test_grade_one_unsupported_command_falls_through(daemon_config):
    """A receipt with shell-metacharacter command_text (unsupported)
    AND no source anchors (so ``synthesize_command`` also returns
    None) falls through to existing routing — command_snapshot
    doesn't short-circuit and atlas grades the receipt normally.
    """
    receipt = grader_service_mod.PacketReceipt(
        question_id="q",
        question="q",
        oracle_claim="c",
        oracle_excerpt="",
        source_path=None,
        source_lines=None,
        source_commit=None,
        command_text="ls foo && find bar",  # compound shell — unsupported
    )

    row = grader_service_mod._grade_one_receipt(
        cfg=daemon_config,
        receipt=receipt,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        repo_full_name="tandemstream/core",
        _runner_run_one=_fake_runner_run_one(),
        _grader_grade=_fake_grader(grade="full_match", confidence=0.9, rationale="r"),
    )
    # The UNSUPPORTED sentinel surfaces on the row to confirm
    # command_snapshot didn't short-circuit.
    assert row.command_snapshot_status == "command_snapshot_unsupported"
    # Atlas was called → the grader returned full_match → row counted.
    assert row.grade == "full_match"
    assert row.score_status == "counted"


def test_grading_summary_skipped_command_snapshot_count():
    """GradingSummary's new PR #20 counter rolls up only command-
    snapshot skips."""
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod

    rows = [
        pr_comment_mod.ReceiptGradingRow(
            question_id="q1", question="q1", grade="atlas_not_found",
            confidence=1.0, rationale="r", tool="skipped",
            score_status="skipped_command_snapshot",
            clean_excluded_reason="command_snapshot",
            command_snapshot_status="command_snapshot_match",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q2", question="q2", grade="atlas_not_found",
            confidence=1.0, rationale="r", tool="skipped",
            score_status="skipped_command_snapshot",
            clean_excluded_reason="command_snapshot",
            command_snapshot_status="command_snapshot_no_match_expected_absent",
        ),
        pr_comment_mod.ReceiptGradingRow(
            question_id="q3", question="q3", grade="full_match",
            confidence=0.9, rationale="ok", tool="find_code",
            score_status="counted",
        ),
    ]
    s = pr_comment_mod.GradingSummary(
        packet_id="pkt", code_revision_id=None,
        base_sha=BASE_SHA, threshold_pct=50, rows=rows,
    )
    assert s.skipped_command_snapshot_count == 2
    assert s.excluded_count == 2
    assert s.clean_total == 1


def test_serialize_row_includes_command_snapshot_fields():
    from atlas_shadow.ingest_daemon import pr_comment as pr_comment_mod
    from atlas_shadow.ingest_daemon import grader_service as gs

    row = pr_comment_mod.ReceiptGradingRow(
        question_id="q", question="q", grade="atlas_not_found",
        confidence=1.0, rationale="r", tool="skipped",
        command_snapshot_status="command_snapshot_match",
        command_snapshot_hash_match=True,
        command_snapshot_sha256="deadbeef" * 8,
        command_snapshot_head="line two\nline three",
        command_snapshot_exit_code=0,
    )
    out = gs._serialize_row(row)
    assert out["command_snapshot_status"] == "command_snapshot_match"
    assert out["command_snapshot_hash_match"] is True
    assert out["command_snapshot_sha256"] == "deadbeef" * 8
    assert out["command_snapshot_head"] == "line two\nline three"
    assert out["command_snapshot_exit_code"] == 0
