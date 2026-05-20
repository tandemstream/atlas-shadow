"""atlas_query_cache — SQLite-backed cache for raw ``workspace_atlas_query``
subprocess results.

**Why this exists.** Re-running the same grading workflow against the
same commit (dashboard regen, counted-misses regen, comparison
re-renders, post-receipt-edit re-grade) currently re-invokes Atlas for
every receipt every time. Each Atlas call is a multi-second subprocess
that loads the atlas package + opens DB connections + runs an LLM
call. With ~80 atlas-eligible receipts per typical baseline, repeated
runs burn minutes each on work whose result is deterministic given
the same inputs.

**Scope is intentionally narrow.** This caches ONLY the JSON stdout
of ``workspace_atlas_query`` subprocess calls. It does NOT cache:

  * Grader / LLM judgments (those depend on the oracle text which
    changes when the receipt is edited).
  * ``command_snapshot`` / ``doc_resolver`` resolver results (those
    are cheap deterministic git/db operations; caching them would
    add complexity without payoff and risks masking corpus changes
    that those resolvers SHOULD pick up immediately).

**Cache key includes every input that can affect the Atlas response.**
Per Codex's PR B1 review feedback:

  * ``query_text`` (canonical: stripped + internal whitespace
    collapsed)
  * ``tool`` (find_code / scan_search / doc_resolver)
  * ``source_path`` (PR #426 fast-path is anchored here)
  * ``source_lines`` (same — fast-path uses both)
  * ``source_commit`` (the pinned receipt commit)
  * ``code_revision_id`` (auto-invalidates on atlas re-index)
  * ``org_id`` (RLS visibility)
  * ``principal_id`` (RLS visibility)
  * ``domain_pack`` (affects tool behavior)
  * ``cache_version`` (manual invalidation knob — bump when atlas
    response shape changes or you need a clean slate)

**Live PR-grading boundary.** The cache is opt-in at the call site,
not via global config. ``build_query_cache_if_enabled`` is invoked
only from batch mode (``grade_batch.cmd_grade_packet_batch``); the
webhook path calls ``run_pr_grading`` without a cache argument so
production gates never observe a cached result.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ─── Manual invalidation knob ─────────────────────────────────────────


# Bump CACHE_VERSION when:
#   * The ``AtlasResponse`` / ``workspace_atlas_query`` JSON schema
#     changes in a way that makes old cached entries unusable.
#   * You need a clean slate (e.g. validating a cache-correctness
#     concern in an existing populated cache).
# All entries written under a previous version become unreachable
# because the version is part of the key fingerprint.
CACHE_VERSION = 1


# ─── Cache status values ─────────────────────────────────────────────


# Used as the per-row ``atlas_cache_status`` field. Operators read
# this to disambiguate "cache off" from "didn't call Atlas".
STATUS_HIT = "hit"
STATUS_MISS = "miss"
STATUS_DISABLED = "disabled"

# Receipts that never call Atlas (pre-skipped via command_snapshot /
# unavailable_source_ref / etc.) carry ``atlas_cache_status = None``
# on the row — distinct from ``"disabled"`` which means "cache was off
# but Atlas WAS called".

ALL_STATUSES: tuple[str, ...] = (STATUS_HIT, STATUS_MISS, STATUS_DISABLED)


# ─── Cache key ───────────────────────────────────────────────────────


def canonical_query_text(query_text: str) -> str:
    """Normalize query text for cache hits: strip ends + collapse
    internal whitespace. Avoids trivial cache misses on whitespace-only
    variations while staying strict enough that a real query change
    misses.
    """
    return " ".join((query_text or "").split())


@dataclass(frozen=True)
class CacheKey:
    """The 10 inputs that fully determine a workspace_atlas_query result.

    Keep field order stable — :meth:`fingerprint` joins by null bytes,
    so reordering would invalidate every existing cache entry without
    a ``CACHE_VERSION`` bump.
    """

    query_text: str
    tool: str
    source_path: Optional[str]
    source_lines: Optional[str]
    source_commit: str
    code_revision_id: Optional[str]
    org_id: str
    principal_id: Optional[str]
    domain_pack: Optional[str]
    cache_version: int = CACHE_VERSION

    def fingerprint(self) -> str:
        """SHA256 over the canonicalized fields joined with null bytes.

        Null-byte separator is unambiguous against any field value
        (paths, queries, ids — none contain ``\\x00`` in practice).
        Each input is stringified consistently (``None`` → ``""``)
        so cache hits are stable across types.
        """
        parts = [
            canonical_query_text(self.query_text),
            self.tool or "",
            self.source_path or "",
            self.source_lines or "",
            self.source_commit or "",
            self.code_revision_id or "",
            self.org_id or "",
            self.principal_id or "",
            self.domain_pack or "",
            str(self.cache_version),
        ]
        joined = "\x00".join(parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheHit:
    """A successful cache lookup. ``response_json`` is the verbatim
    JSON the subprocess returned on stdout — caller deserializes."""

    key_fingerprint: str
    response_json: str
    response_latency_ms: int
    cached_at: str
    hits_before: int


# ─── The cache ───────────────────────────────────────────────────────


class AtlasQueryCache:
    """SQLite-backed cache for atlas-query subprocess results.

    Concurrency: opened with ``check_same_thread=False`` and ``PRAGMA
    journal_mode=WAL`` so PR A's receipt parallelism (when it lands)
    can share a single cache instance across worker threads safely.

    Double-write under concurrency is benign — two threads computing
    the same cache miss both write the result; sqlite's ``INSERT OR
    REPLACE`` keeps the last write. The two cached values are
    semantically identical because they came from the same inputs.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS atlas_query_cache (
        key TEXT PRIMARY KEY,
        query_text TEXT NOT NULL,
        tool TEXT NOT NULL,
        source_path TEXT,
        source_lines TEXT,
        source_commit TEXT NOT NULL,
        code_revision_id TEXT,
        org_id TEXT NOT NULL,
        principal_id TEXT,
        domain_pack TEXT,
        cache_version INTEGER NOT NULL,
        response_json TEXT NOT NULL,
        response_latency_ms INTEGER NOT NULL,
        cached_at TEXT NOT NULL,
        hits INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_atlas_query_cache_cached_at
        ON atlas_query_cache(cached_at);
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection. WAL mode + check_same_thread=False
        so threaded callers (PR A) can share this cache instance."""
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
            timeout=30.0,  # bounded wait on writer lock
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)

    def get(self, key: CacheKey) -> Optional[CacheHit]:
        """Look up a cached response. Returns ``None`` on miss.

        On hit, increments the row's ``hits`` counter — useful for
        observability ("how many times has this query been answered
        from cache?"). The increment is a separate UPDATE rather than
        baked into the SELECT to keep the hot path simple.
        """
        fp = key.fingerprint()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_json, response_latency_ms, cached_at, hits "
                "FROM atlas_query_cache WHERE key = ?",
                (fp,),
            ).fetchone()
            if row is None:
                return None
            response_json, latency_ms, cached_at, hits_before = row
            conn.execute(
                "UPDATE atlas_query_cache SET hits = hits + 1 WHERE key = ?",
                (fp,),
            )
        return CacheHit(
            key_fingerprint=fp,
            response_json=response_json,
            response_latency_ms=int(latency_ms),
            cached_at=cached_at,
            hits_before=int(hits_before),
        )

    def set(
        self,
        key: CacheKey,
        *,
        response_json: str,
        response_latency_ms: int,
    ) -> str:
        """Store (or replace) a cached response. Returns the
        fingerprint used as the key.

        ``INSERT OR REPLACE`` semantics: a concurrent double-miss
        write is safe; both rows are identical, last write wins.
        """
        fp = key.fingerprint()
        cached_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO atlas_query_cache (
                    key, query_text, tool, source_path, source_lines,
                    source_commit, code_revision_id, org_id, principal_id,
                    domain_pack, cache_version, response_json,
                    response_latency_ms, cached_at, hits
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    fp,
                    canonical_query_text(key.query_text),
                    key.tool,
                    key.source_path,
                    key.source_lines,
                    key.source_commit,
                    key.code_revision_id,
                    key.org_id,
                    key.principal_id,
                    key.domain_pack,
                    int(key.cache_version),
                    response_json,
                    int(response_latency_ms),
                    cached_at,
                ),
            )
        return fp

    def stats(self) -> dict[str, Any]:
        """Aggregate stats for the ``make cache-stats`` Makefile
        target. Reports entry count, total cumulative hits across
        all entries, and a coarse oldest-entry timestamp.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0), MIN(cached_at), "
                "MAX(cached_at) FROM atlas_query_cache"
            ).fetchone()
        n_entries, total_hits, oldest, newest = row
        return {
            "db_path": str(self.db_path),
            "cache_version": CACHE_VERSION,
            "entries": int(n_entries),
            "cumulative_hits": int(total_hits or 0),
            "oldest_cached_at": oldest,
            "newest_cached_at": newest,
        }

    def truncate(self) -> int:
        """Delete every cached entry. Returns the number of rows
        deleted. Used by the ``make truncate-cache`` Makefile target
        for periodic cleanup or after a cache-correctness concern.
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM atlas_query_cache")
            deleted = cur.rowcount or 0
        return int(deleted)


# ─── Factory ─────────────────────────────────────────────────────────


_DEFAULT_CACHE_PATH = Path.home() / ".atlas-shadow" / "query-cache.sqlite"


def _env_disabled() -> bool:
    """Honor ``ATLAS_SHADOW_QUERY_CACHE=off`` (or ``=disabled``,
    ``=0``, ``=false``). Any other value (including unset) leaves
    the cache enabled — opt-out, not opt-in."""
    raw = (os.environ.get("ATLAS_SHADOW_QUERY_CACHE") or "").strip().lower()
    return raw in {"off", "disabled", "0", "false", "no"}


def build_query_cache_if_enabled(
    cfg: Any = None,
    *,
    db_path: Optional[Path] = None,
) -> Optional[AtlasQueryCache]:
    """Construct an :class:`AtlasQueryCache` instance unless disabled.

    Returns ``None`` when:
      * ``ATLAS_SHADOW_QUERY_CACHE`` env var is set to ``off`` /
        ``disabled`` / ``0`` / ``false`` / ``no``.
      * The cfg explicitly sets ``query_cache_enabled: false``.

    Otherwise returns a cache opened at ``db_path`` (or the default
    ``~/.atlas-shadow/query-cache.sqlite``).

    The cfg-disabled path coexists with the env-var override; either
    can disable the cache. The env var wins for ad-hoc operator
    overrides (e.g. ``ATLAS_SHADOW_QUERY_CACHE=off make probe``).
    """
    if _env_disabled():
        print(
            "[atlas_query_cache] disabled via ATLAS_SHADOW_QUERY_CACHE env var",
            file=sys.stderr,
        )
        return None
    if cfg is not None:
        cfg_flag = getattr(cfg, "query_cache_enabled", None)
        if cfg_flag is None and isinstance(cfg, dict):
            cfg_flag = cfg.get("query_cache_enabled")
        if cfg_flag is False:
            return None
    resolved_path = Path(db_path) if db_path is not None else _DEFAULT_CACHE_PATH
    return AtlasQueryCache(resolved_path)
