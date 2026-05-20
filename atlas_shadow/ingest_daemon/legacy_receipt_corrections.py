"""Legacy receipt correction sidecar support.

This module is intentionally narrow. It exists for frozen historical packet
corpora where the authored ``02-qna-log.md`` files no longer live on core
``main`` and cannot be repaired in place. Corrections are opt-in from offline
batch grading only; live PR grading never loads this sidecar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml


DEFAULT_CORRECTIONS_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "pilots"
    / "legacy-receipt-corrections.yaml"
)


@dataclass(frozen=True)
class LegacyReceiptCorrection:
    packet_id: str
    question_id: str
    action: str
    clean_excluded_reason: str
    note: str
    source_commit: Optional[str] = None


class LegacyReceiptCorrections:
    """Lookup wrapper for packet/qid/source-commit scoped corrections."""

    def __init__(self, corrections: Iterable[LegacyReceiptCorrection] = ()):
        self._corrections = tuple(corrections)

    def match(
        self,
        *,
        packet_id: Optional[str],
        question_id: str,
        source_commit: Optional[str],
    ) -> Optional[LegacyReceiptCorrection]:
        if not packet_id:
            return None
        for correction in self._corrections:
            if correction.packet_id != packet_id:
                continue
            if correction.question_id != question_id:
                continue
            if not _commit_matches(
                expected=correction.source_commit,
                actual=source_commit,
            ):
                continue
            return correction
        return None

    def __bool__(self) -> bool:
        return bool(self._corrections)


def load_legacy_receipt_corrections(
    path: Path = DEFAULT_CORRECTIONS_PATH,
) -> LegacyReceiptCorrections:
    """Load the default legacy-correction sidecar if present."""

    path = Path(path)
    if not path.exists():
        return LegacyReceiptCorrections()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("corrections", []) if isinstance(raw, dict) else []
    corrections: list[LegacyReceiptCorrection] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        if action != "skip_legacy_receipt_defect":
            continue
        corrections.append(
            LegacyReceiptCorrection(
                packet_id=str(item["packet_id"]).strip(),
                question_id=str(item["question_id"]).strip(),
                action=action,
                clean_excluded_reason=str(
                    item.get("clean_excluded_reason")
                    or "legacy_receipt_defect"
                ).strip(),
                note=str(item.get("note") or "").strip(),
                source_commit=_optional_str(item.get("source_commit")),
            )
        )
    return LegacyReceiptCorrections(corrections)


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _commit_matches(*, expected: Optional[str], actual: Optional[str]) -> bool:
    """Allow short-SHA sidecar entries while guarding against broad matches."""

    if not expected:
        return True
    if not actual:
        return False
    lhs = expected.lower()
    rhs = actual.lower()
    return lhs == rhs or rhs.startswith(lhs) or lhs.startswith(rhs)
