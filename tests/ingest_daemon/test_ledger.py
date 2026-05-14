"""T-D2 (cont'd): ledger tests (insert, latest_succeeded, by_commit)."""

from __future__ import annotations

from atlas_shadow.ingest_daemon import ledger as ledger_mod


SHA_A = "a" * 40
SHA_B = "b" * 40


def test_insert_terminal_succeeded(db_path):
    ledger_id = ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=SHA_A,
        status="succeeded",
        started_at="2026-05-14T00:00:00+00:00",
        attempt_number=1,
        code_revision_id="11111111-1111-1111-1111-111111111111",
        scip_path="/tmp/x.scip",
        source_root="/tmp/x",
        scip_size_bytes=1234,
        chunker_stats_total=42,
        counts={"file_count": 7, "symbol_count": 99},
        latency_ms=8000,
    )
    assert isinstance(ledger_id, int) and ledger_id > 0
    latest = ledger_mod.latest_succeeded(db_path)
    assert latest is not None
    assert latest["commit_sha"] == SHA_A
    assert latest["code_revision_id"] == "11111111-1111-1111-1111-111111111111"
    assert latest["counts"]["file_count"] == 7
    assert latest["latency_ms"] == 8000


def test_latest_succeeded_skips_failed(db_path):
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=SHA_A,
        status="succeeded",
        started_at="2026-05-14T00:00:00+00:00",
        attempt_number=1,
        code_revision_id="rev-A",
    )
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=SHA_B,
        status="failed",
        started_at="2026-05-14T00:01:00+00:00",
        attempt_number=1,
        error_message="boom",
    )
    latest = ledger_mod.latest_succeeded(db_path)
    assert latest["commit_sha"] == SHA_A
    assert latest["code_revision_id"] == "rev-A"


def test_latest_attempt_returns_most_recent_regardless_of_status(db_path):
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=SHA_A,
        status="succeeded",
        started_at="2026-05-14T00:00:00+00:00",
        attempt_number=1,
        code_revision_id="rev-A",
    )
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=SHA_B,
        status="failed",
        started_at="2026-05-14T00:01:00+00:00",
        attempt_number=1,
        error_message="boom",
    )
    latest = ledger_mod.latest_attempt(db_path)
    assert latest["commit_sha"] == SHA_B
    assert latest["status"] == "failed"
    assert latest["error_message"] == "boom"


def test_get_by_commit_sha_returns_newest_first(db_path):
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=SHA_A,
        status="failed",
        started_at="2026-05-14T00:00:00+00:00",
        attempt_number=1,
        error_message="first",
    )
    ledger_mod.insert_terminal_attempt(
        db_path,
        commit_sha=SHA_A,
        status="succeeded",
        started_at="2026-05-14T00:01:00+00:00",
        attempt_number=2,
        code_revision_id="rev-A",
    )
    rows = ledger_mod.get_by_commit_sha(db_path, SHA_A)
    assert len(rows) == 2
    assert rows[0]["status"] == "succeeded"
    assert rows[1]["status"] == "failed"


def test_insert_rejects_invalid_status(db_path):
    import pytest

    with pytest.raises(ValueError):
        ledger_mod.insert_terminal_attempt(
            db_path,
            commit_sha=SHA_A,
            status="running",
            started_at="x",
            attempt_number=1,
        )


def test_latest_succeeded_returns_none_when_empty(db_path):
    assert ledger_mod.latest_succeeded(db_path) is None
