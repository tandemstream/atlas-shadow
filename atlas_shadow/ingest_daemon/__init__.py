"""atlas_shadow.ingest_daemon — continuous-ingest daemon for atlas-shadow.

Packet: 2026-05-13-atlas-shadow-continuous-ingest-v1 (D5).

Receives GitHub `push` webhooks for ``tandemstream/core@main``, queues each
commit SHA, and runs a single-thread worker that builds SCIP and shells out
to ``scripts.dogfood_v2_smoketest_ingest_code`` in the Atlas leaf — the
single subprocess does both ``ingest_scip_upload`` and
``chunk_code_revision`` (per the post-freeze amendment to the plan; the
daemon never imports core.code.*).

Modules:
- ``config``      — load ``shadow-config.yaml``'s ``ingest_daemon:`` section.
- ``queue``       — SQLite FIFO + dedup over ``ingest_queue``.
- ``ledger``      — append-only attempt history (``ingest_ledger``).
- ``cache``       — clone-or-fetch ``tandemstream/core`` into a local dir.
- ``scip_builder``— shell out to ``scip-python`` to emit a SCIP blob.
- ``worker``      — drain the queue; shell out to the dogfood ingest CLI.
- ``receiver``    — FastAPI app with ``POST /webhook`` and ``GET /status``.
- ``entrypoint``  — process entry point (``python -m
                    atlas_shadow.ingest_daemon``).

The state-file ``<atlas-shadow>/.daemon-state.json`` is the daemon → runner
IPC: the runner reads it for ``latest_code_revision_id`` and falls back to
``shadow-config.yaml:continuous_shadow_code_revision_id`` when absent.
"""

from __future__ import annotations

__all__ = [
    "config",
    "queue",
    "ledger",
    "cache",
    "scip_builder",
    "worker",
    "receiver",
    "entrypoint",
]
