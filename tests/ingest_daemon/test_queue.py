"""T-D2: queue tests (FIFO, dedup, retry, max_attempts, recovery)."""

from __future__ import annotations

from atlas_shadow.ingest_daemon import queue as queue_mod


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def test_enqueue_new_returns_queued_true(db_path):
    res = queue_mod.enqueue(db_path, SHA_A)
    assert res["queued"] is True
    assert res["reason"] == "new"
    assert isinstance(res["queue_id"], int)
    assert queue_mod.queue_depth(db_path) == 1


def test_enqueue_dedups_succeeded(db_path):
    # First enqueue and drain to succeeded.
    res1 = queue_mod.enqueue(db_path, SHA_A)
    claim = queue_mod.claim_next(db_path)
    queue_mod.mark_terminal(db_path, claim["queue_id"], status="succeeded")
    # Re-enqueue should dedup.
    res2 = queue_mod.enqueue(db_path, SHA_A)
    assert res2["queued"] is False
    assert res2["reason"] == "dedup-succeeded"
    assert res2["queue_id"] == res1["queue_id"]
    assert queue_mod.queue_depth(db_path) == 0


def test_enqueue_already_queued_is_noop(db_path):
    queue_mod.enqueue(db_path, SHA_A)
    res = queue_mod.enqueue(db_path, SHA_A)
    assert res["queued"] is False
    assert res["reason"] == "already-queued"
    assert queue_mod.queue_depth(db_path) == 1


def test_enqueue_failed_below_cap_requeues(db_path):
    queue_mod.enqueue(db_path, SHA_A)
    claim = queue_mod.claim_next(db_path)
    queue_mod.mark_terminal(db_path, claim["queue_id"], status="failed", error="boom")
    res = queue_mod.enqueue(db_path, SHA_A, max_attempts=3)
    assert res["queued"] is True
    assert res["reason"] == "requeued-after-failure"
    assert queue_mod.queue_depth(db_path) == 1


def test_enqueue_failed_at_cap_refuses(db_path):
    """3 failed attempts; row stays in `failed` with attempt_count=3;
    enqueuing again with max_attempts=3 yields max-attempts-exceeded."""
    queue_mod.enqueue(db_path, SHA_A)
    # Attempt 1: claim → fail → requeue (attempt_count=1, failed → queued).
    claim = queue_mod.claim_next(db_path)
    queue_mod.mark_terminal(db_path, claim["queue_id"], status="failed", error="boom")
    queue_mod.enqueue(db_path, SHA_A, max_attempts=10)
    # Attempt 2: claim → fail → requeue (attempt_count=2, failed → queued).
    claim = queue_mod.claim_next(db_path)
    queue_mod.mark_terminal(db_path, claim["queue_id"], status="failed", error="boom")
    queue_mod.enqueue(db_path, SHA_A, max_attempts=10)
    # Attempt 3: claim → fail; DO NOT requeue (we want failed status w/ count=3).
    claim = queue_mod.claim_next(db_path)
    queue_mod.mark_terminal(db_path, claim["queue_id"], status="failed", error="boom")
    # Row state now: status=failed, attempt_count=3. enqueue at cap = no-op.
    res = queue_mod.enqueue(db_path, SHA_A, max_attempts=3)
    assert res["queued"] is False
    assert res["reason"] == "max-attempts-exceeded"


def test_claim_next_fifo_order(db_path):
    queue_mod.enqueue(db_path, SHA_A)
    queue_mod.enqueue(db_path, SHA_B)
    queue_mod.enqueue(db_path, SHA_C)
    first = queue_mod.claim_next(db_path)
    second = queue_mod.claim_next(db_path)
    third = queue_mod.claim_next(db_path)
    assert first["commit_sha"] == SHA_A
    assert second["commit_sha"] == SHA_B
    assert third["commit_sha"] == SHA_C


def test_claim_next_returns_none_on_empty(db_path):
    assert queue_mod.claim_next(db_path) is None


def test_claim_increments_attempt_count(db_path):
    queue_mod.enqueue(db_path, SHA_A)
    claim1 = queue_mod.claim_next(db_path)
    assert claim1["attempt_number"] == 1
    queue_mod.mark_terminal(db_path, claim1["queue_id"], status="failed", error="x")
    queue_mod.enqueue(db_path, SHA_A)  # requeue
    claim2 = queue_mod.claim_next(db_path)
    assert claim2["attempt_number"] == 2


def test_recover_running_on_startup(db_path):
    queue_mod.enqueue(db_path, SHA_A)
    queue_mod.enqueue(db_path, SHA_B)
    queue_mod.claim_next(db_path)  # now SHA_A is running
    n = queue_mod.recover_running_on_startup(db_path)
    assert n == 1
    # Both rows queued again.
    assert queue_mod.queue_depth(db_path) == 2


def test_enqueue_rejects_empty_sha(db_path):
    import pytest

    with pytest.raises(ValueError):
        queue_mod.enqueue(db_path, "")


def test_mark_terminal_rejects_bad_status(db_path):
    import pytest

    queue_mod.enqueue(db_path, SHA_A)
    claim = queue_mod.claim_next(db_path)
    with pytest.raises(ValueError):
        queue_mod.mark_terminal(db_path, claim["queue_id"], status="running")
