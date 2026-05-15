"""grader_service — PR grading orchestrator.

This module is the entry point for atlas-shadow's pre-merge grading gate
(packet 2026-05-14-atlas-shadow-pre-merge-grading-gate-v1). It coordinates
across receiver (T1), packet-tag detection (T2), receipt parsing (T3),
receipt -> Atlas-query translation (T4), doc-anchored resolution (T4a, in
``doc_resolver.py``), the existing offline ``grader.grade`` rubric, revision
pinning (T6), GitHub Checks API (T7), the PR comment generator (T8), and
the durable artifact writer (T9).

Tasks colocated here:

  * T2 — :func:`detect_packet_qna_log` (PR file-presence check).
  * T3 — :func:`parse_packet_receipts` (wire ``parser.parse_qna_log_markdown``;
    parse ``grading_threshold_pct:`` header override).
  * T4 — :func:`translate_receipt_to_query` (CODE-anchored heuristic +
    ``query_hint:`` override + doc-extension routing to T4a).
  * T5 — :func:`run_pr_grading` / :func:`handle_pr_event` (the orchestrator).
  * T6 — pin lifecycle helpers (:func:`acquire_pin` / :func:`release_pin`).

T4a (doc resolver) lives in a separate module to keep its direct psycopg
dependency contained.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# T2 — Packet-tag detection
# ---------------------------------------------------------------------------

# Match `<anything>/docs/work/<packet>/02-qna-log.md`. The packet slug is the
# directory between `docs/work/` and `/02-qna-log.md`; T3 uses it to find
# sibling planning docs. Anchored so the suffix is the exact filename.
_QNA_LOG_RE = re.compile(
    r"(?:^|/)docs/work/(?P<packet>[^/]+)/02-qna-log\.md$"
)


def detect_packet_qna_log(pr_files: list[str]) -> list[str]:
    """Return PR files that look like packet ``02-qna-log.md`` paths.

    A PR is "packet-tagged" (RQ-1) when it touches at least one
    ``<...>/docs/work/<packet>/02-qna-log.md`` file. The packet directory
    is ``Path(qna_log_path).parent`` — T3 uses that to locate the receipts.

    Args:
      pr_files: list of file paths from the GitHub PR-files API. Caller
        is responsible for filtering out deletions (status='removed').

    Returns:
      list of touched ``02-qna-log.md`` paths (zero, one, or more — most
      PRs touch one packet but multi-packet PRs are not refused).
    """
    return [p for p in pr_files if _QNA_LOG_RE.search(p)]
