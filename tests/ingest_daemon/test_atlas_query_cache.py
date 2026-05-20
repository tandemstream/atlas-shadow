"""Tests for ``atlas_shadow.ingest_daemon.atlas_query_cache``.

Coverage:
  * Cache key fingerprint stability + every-input sensitivity
  * Get/set round-trip
  * Concurrent double-write safety
  * Env-var disable
  * Cache-version invalidation
  * code_revision_id invalidation (the primary auto-invalidation
    signal when atlas re-indexes)
  * Factory: build_query_cache_if_enabled
  * stats + truncate
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from atlas_shadow.ingest_daemon import atlas_query_cache as cache_mod


# ─── CacheKey ─────────────────────────────────────────────────────────


def _base_key(**overrides) -> cache_mod.CacheKey:
    """Build a CacheKey with sensible defaults; overrides one field."""
    defaults = dict(
        query_text="find synthesize_scan_envelope",
        tool="find_code",
        source_path="core/query/scan_synth.py",
        source_lines="261-361",
        source_commit="408858a",
        code_revision_id="rev-abc",
        org_id="org-1",
        principal_id="user-1",
        domain_pack="code",
    )
    defaults.update(overrides)
    return cache_mod.CacheKey(**defaults)


def test_cache_key_fingerprint_stable_for_same_inputs():
    """Two CacheKeys with identical inputs produce identical
    fingerprints. (Sanity check for the canonicalization.)"""
    a = _base_key()
    b = _base_key()
    assert a.fingerprint() == b.fingerprint()


@pytest.mark.parametrize(
    "field,changed_value",
    [
        ("query_text", "find synthesize_doc_envelope"),
        ("tool", "scan_search"),
        ("source_path", "core/query/answer.py"),
        ("source_lines", "100-200"),
        ("source_commit", "deadbeef"),
        ("code_revision_id", "rev-xyz"),
        ("org_id", "org-2"),
        ("principal_id", "user-2"),
        ("domain_pack", "scheduling_admin"),
    ],
)
def test_cache_key_fingerprint_changes_per_input(field, changed_value):
    """Every input contributes to the fingerprint — changing any one
    yields a different cache key. Critical correctness: missing any
    input from the key would cause silent stale cache hits."""
    base = _base_key()
    changed = _base_key(**{field: changed_value})
    assert base.fingerprint() != changed.fingerprint(), (
        f"field={field!r}: changing this value did NOT change the "
        f"fingerprint — it's missing from the cache key. This is the "
        f"exact class of bug that produces silent wrong-answers."
    )


def test_cache_key_normalizes_query_whitespace():
    """Trailing/internal whitespace variations in query_text shouldn't
    cause cache misses. Different cosmetic forms → same fingerprint."""
    a = _base_key(query_text="find  the   thing")
    b = _base_key(query_text="find the thing")
    c = _base_key(query_text="\n  find the thing  \n")
    assert a.fingerprint() == b.fingerprint() == c.fingerprint()


def test_cache_key_version_bump_invalidates():
    """Bumping CACHE_VERSION yields a different fingerprint — every
    prior entry becomes unreachable. This is the manual escape hatch
    for atlas-side semantic changes that don't bump code_revision_id."""
    a = _base_key(cache_version=1)
    b = _base_key(cache_version=2)
    assert a.fingerprint() != b.fingerprint()


def test_cache_key_none_values_distinguishable():
    """``None`` vs empty string should both be valid but the
    fingerprint must be consistent for ``None`` (we treat both as
    empty for hashing). Important: a receipt with no source_path is
    a legitimate cache key, not a corrupt one."""
    a = _base_key(source_path=None, source_lines=None)
    b = _base_key(source_path=None, source_lines=None)
    assert a.fingerprint() == b.fingerprint()
    # Different from when paths ARE set.
    c = _base_key(source_path="foo.py", source_lines="1-10")
    assert a.fingerprint() != c.fingerprint()


# ─── AtlasQueryCache: get/set ─────────────────────────────────────────


@pytest.fixture
def cache(tmp_path: Path) -> cache_mod.AtlasQueryCache:
    """A fresh per-test cache. NEVER use the real
    ``~/.atlas-shadow/query-cache.sqlite`` from tests."""
    return cache_mod.AtlasQueryCache(tmp_path / "test-cache.sqlite")


def test_cache_miss_returns_none(cache):
    """Querying an unknown key returns None — caller treats that as
    cache miss and calls Atlas."""
    key = _base_key()
    assert cache.get(key) is None


def test_cache_round_trip_returns_stored_payload(cache):
    """Store a response, retrieve it, payload matches exactly.
    The byte-identical guarantee is what makes the layered report
    score equivalence test trustworthy on rerun."""
    key = _base_key()
    payload = json.dumps({"tool_used": "find_code", "answer_text": "hello"})
    fp = cache.set(key, response_json=payload, response_latency_ms=1234)

    hit = cache.get(key)
    assert hit is not None
    assert hit.key_fingerprint == fp
    assert hit.response_json == payload
    assert hit.response_latency_ms == 1234
    assert hit.hits_before == 0  # never accessed before


def test_cache_increments_hits_counter(cache):
    """Each ``get()`` on an existing entry increments the cumulative
    hit counter — useful for ``make cache-stats`` and probe analysis."""
    key = _base_key()
    cache.set(key, response_json="{}", response_latency_ms=0)
    cache.get(key)
    cache.get(key)
    hit = cache.get(key)
    # First call returned hits_before=0; second returned 1; third returns 2.
    assert hit.hits_before == 2


def test_cache_invalidates_on_code_revision_id_change(cache):
    """When atlas re-indexes, ``code_revision_id`` changes. Existing
    cached entries become unreachable → cache miss → fresh atlas
    call. This is the primary auto-invalidation signal that doesn't
    require operator intervention."""
    old = _base_key(code_revision_id="rev-old")
    new = _base_key(code_revision_id="rev-new")
    cache.set(old, response_json='{"old": true}', response_latency_ms=0)
    # The new revision_id doesn't hit the cached entry.
    assert cache.get(new) is None
    # Sanity: the old key still hits.
    assert cache.get(old) is not None


def test_cache_replace_keeps_last_write(cache):
    """``INSERT OR REPLACE`` semantics: writing the same key twice
    keeps the last write. This is what protects concurrent
    double-misses (two threads compute the same atlas call → both
    write → semantically identical, last wins)."""
    key = _base_key()
    cache.set(key, response_json='{"v": 1}', response_latency_ms=100)
    cache.set(key, response_json='{"v": 2}', response_latency_ms=200)

    hit = cache.get(key)
    assert hit.response_json == '{"v": 2}'
    assert hit.response_latency_ms == 200


def test_cache_concurrent_double_write_safe(cache):
    """PR A's receipt parallelism will share one cache instance across
    threads. Two threads writing the same key concurrently must not
    raise — the second write replaces the first cleanly."""
    key = _base_key()
    errors = []

    def writer(payload):
        try:
            cache.set(key, response_json=payload, response_latency_ms=0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(target=writer, args=(f'{{"thread": {i}}}',))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent writes raised: {errors}"
    # One of the 8 payloads must be there (last wins is fine).
    hit = cache.get(key)
    assert hit is not None
    assert hit.response_json.startswith('{"thread"')


# ─── stats + truncate ────────────────────────────────────────────────


def test_cache_stats_reports_entry_count(cache):
    """``stats()`` is consumed by ``make cache-stats``. Reports
    entry count + cumulative hits + first/last cached_at."""
    cache.set(_base_key(query_text="a"), response_json="{}", response_latency_ms=1)
    cache.set(_base_key(query_text="b"), response_json="{}", response_latency_ms=2)
    cache.get(_base_key(query_text="a"))

    stats = cache.stats()
    assert stats["entries"] == 2
    assert stats["cumulative_hits"] == 1
    assert stats["cache_version"] == cache_mod.CACHE_VERSION
    assert stats["oldest_cached_at"] is not None


def test_cache_truncate_removes_all_entries(cache):
    """``make truncate-cache`` removes everything. Returns rows
    deleted so the operator sees what changed."""
    cache.set(_base_key(query_text="a"), response_json="{}", response_latency_ms=0)
    cache.set(_base_key(query_text="b"), response_json="{}", response_latency_ms=0)

    deleted = cache.truncate()
    assert deleted == 2
    assert cache.stats()["entries"] == 0


# ─── Factory + env-var disable ───────────────────────────────────────


def test_build_query_cache_if_enabled_returns_instance(tmp_path: Path, monkeypatch):
    """The default factory call returns a usable cache instance at
    the explicit db_path."""
    monkeypatch.delenv("ATLAS_SHADOW_QUERY_CACHE", raising=False)
    result = cache_mod.build_query_cache_if_enabled(
        db_path=tmp_path / "factory-cache.sqlite",
    )
    assert isinstance(result, cache_mod.AtlasQueryCache)
    assert (tmp_path / "factory-cache.sqlite").is_file()


def test_build_query_cache_if_enabled_respects_env_disable(
    tmp_path: Path, monkeypatch, capsys,
):
    """``ATLAS_SHADOW_QUERY_CACHE=off`` disables. Factory returns
    None. Operator sees a stderr breadcrumb so they know cache was
    skipped (vs. silently absent)."""
    for value in ("off", "disabled", "0", "false", "no"):
        monkeypatch.setenv("ATLAS_SHADOW_QUERY_CACHE", value)
        result = cache_mod.build_query_cache_if_enabled(
            db_path=tmp_path / f"never-{value}.sqlite",
        )
        assert result is None, f"value={value!r} should disable"
        err = capsys.readouterr().err
        assert "disabled via ATLAS_SHADOW_QUERY_CACHE" in err


def test_build_query_cache_if_enabled_respects_cfg_flag(tmp_path: Path, monkeypatch):
    """A cfg with ``query_cache_enabled=False`` also disables.
    Both env-var and cfg flags can independently disable; either wins."""
    monkeypatch.delenv("ATLAS_SHADOW_QUERY_CACHE", raising=False)

    class _Cfg:
        query_cache_enabled = False

    assert cache_mod.build_query_cache_if_enabled(
        _Cfg(),
        db_path=tmp_path / "cfg-off.sqlite",
    ) is None
    # Dict-shaped cfg also supported (some test paths pass dicts).
    assert cache_mod.build_query_cache_if_enabled(
        {"query_cache_enabled": False},
        db_path=tmp_path / "cfg-off-dict.sqlite",
    ) is None


def test_build_query_cache_creates_parent_directory(tmp_path: Path, monkeypatch):
    """Default cache location is ``~/.atlas-shadow/query-cache.sqlite``
    — the parent dir may not exist on a fresh checkout. Construction
    must mkdir it transparently."""
    monkeypatch.delenv("ATLAS_SHADOW_QUERY_CACHE", raising=False)
    nested = tmp_path / "deeply" / "nested" / "cache.sqlite"
    cache_mod.AtlasQueryCache(nested)
    assert nested.is_file()
    assert nested.parent.is_dir()
