"""parser — reads question fixtures from disk into typed receipt objects.

Two input shapes are supported in v1:

1. **Q&A log markdown** (`02-qna-log.md` from a `tandemstream/core` packet
   directory). The receipt structure mirrors the Atlas planning convention
   (`[qa:qN]` citations + a YAML-front-matter block per receipt). Not
   exercised by the dogfood-v2 smoketest, but the parser supports it so
   atlas-shadow can run against future packet receipts.
2. **Dogfood-v2 JSONL** (`tests/fixtures/dogfood-v2-questions.jsonl`). Each
   line is the sanitized oracle for one question — `id`, `question`,
   `ingest_answer.{source_path, source_lines, evidence_excerpt, claim}`.

Both shapes produce a list of :class:`Receipt` objects with the fields the
runner + grader need.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Receipt:
    """A single graded question + its oracle ground truth.

    Fields:
      question_id: identifier (e.g. ``Q01``).
      question: natural-language question text.
      oracle_excerpt: the source span (raw text) that answers the question.
      oracle_claim: a one-sentence assertion the grader compares against.
      source_path: optional file the oracle excerpt was extracted from.
      source_lines: optional ``L-R`` line range string.
      commit_sha: optional commit the oracle excerpt is anchored to.
      class_label: optional fixture class (e.g. ``C1`` in dogfood-v2).
    """

    question_id: str
    question: str
    oracle_excerpt: str
    oracle_claim: str
    source_path: Optional[str] = None
    source_lines: Optional[str] = None
    commit_sha: Optional[str] = None
    class_label: Optional[str] = None
    extra: dict = field(default_factory=dict)


def parse_dogfood_v2_jsonl(path: Path) -> list[Receipt]:
    """Parse the dogfood-v2 sanitized fixture JSONL file.

    Each line is a JSON object with at minimum::

        {
          "id": "Q01",
          "question": "...",
          "class": "C1",
          "ingest_answer": {
            "source_path": "...",
            "source_lines": "214-230",
            "sha": "87aa9fa",
            "evidence_excerpt": "def record_instruction(...): ...",
            "claim": "record_instruction is keyword-only ..."
          }
        }
    """
    if not path.exists():
        raise FileNotFoundError(f"fixture not found: {path}")
    receipts: list[Receipt] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} — invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_no} — expected object, got {type(row).__name__}")
        ingest = row.get("ingest_answer") or {}
        if not isinstance(ingest, dict):
            ingest = {}
        receipts.append(
            Receipt(
                question_id=str(row.get("id") or row.get("question_id") or f"Q{line_no:02d}"),
                question=str(row.get("question") or ""),
                oracle_excerpt=str(ingest.get("evidence_excerpt") or ""),
                oracle_claim=str(ingest.get("claim") or ""),
                source_path=ingest.get("source_path"),
                source_lines=ingest.get("source_lines"),
                commit_sha=ingest.get("sha"),
                class_label=row.get("class"),
                extra={k: row[k] for k in row if k not in {"id", "question", "ingest_answer", "class"}},
            )
        )
    return receipts


def parse_qna_log_markdown(path: Path) -> list[Receipt]:
    """Parse `02-qna-log.md`-style markdown receipts.

    Looks for receipt blocks of the form::

        ## q1 [qa:q1]
        ```yaml
        question: ...
        source_path: ...
        source_lines: ...
        commit_sha: ...
        evidence_excerpt: |
          ...
        claim: ...
        ```

    The implementation is deliberately permissive: it skips malformed
    blocks rather than aborting the whole run. v1 callers only exercise
    this against well-formed Phase-1 packets, which all use the canonical
    convention.
    """
    if not path.exists():
        raise FileNotFoundError(f"qna log not found: {path}")

    text = path.read_text(encoding="utf-8")
    receipts: list[Receipt] = []
    # Split on receipt headers. We accept "## qN" or "### qN".
    blocks: list[tuple[str, str]] = []
    current_id: Optional[str] = None
    current_lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## q") or stripped.startswith("### q"):
            if current_id is not None:
                blocks.append((current_id, "\n".join(current_lines)))
            current_id = stripped.split()[1]  # "q1", "q1." etc.
            current_id = current_id.rstrip(".:")
            current_lines = []
        elif current_id is not None:
            current_lines.append(raw_line)
    if current_id is not None:
        blocks.append((current_id, "\n".join(current_lines)))

    for qid, body in blocks:
        yaml_block = _extract_yaml_block(body)
        if not yaml_block:
            continue
        fields = _parse_simple_yaml(yaml_block)
        question = fields.get("question") or ""
        if not question:
            continue
        receipts.append(
            Receipt(
                question_id=qid,
                question=question,
                oracle_excerpt=fields.get("evidence_excerpt", ""),
                oracle_claim=fields.get("claim", ""),
                source_path=fields.get("source_path"),
                source_lines=fields.get("source_lines"),
                commit_sha=fields.get("commit_sha") or fields.get("sha"),
                class_label=fields.get("class"),
            )
        )
    return receipts


def parse_fixture(path: Path) -> list[Receipt]:
    """Auto-detect fixture format by suffix and dispatch."""
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return parse_dogfood_v2_jsonl(path)
    if suffix in {".md", ".markdown"}:
        return parse_qna_log_markdown(path)
    raise ValueError(f"unsupported fixture format for {path}: {suffix}")


# ---------------------------------------------------------------------------
# Internal helpers — intentionally minimal; we avoid a YAML dep here so the
# parser module is import-light. The fixture format is simple key/value with
# at most one ``|``-indented block (``evidence_excerpt``).
# ---------------------------------------------------------------------------


def _extract_yaml_block(body: str) -> Optional[str]:
    """Extract the first triple-backtick yaml block from a markdown body."""
    lines = body.splitlines()
    in_block = False
    out: list[str] = []
    for ln in lines:
        if ln.strip().startswith("```yaml"):
            in_block = True
            continue
        if in_block and ln.strip().startswith("```"):
            break
        if in_block:
            out.append(ln)
    if not out:
        return None
    return "\n".join(out)


def _parse_simple_yaml(text: str) -> dict[str, str]:
    """Very small YAML subset parser.

    Supports `key: value` lines, plus the ``|`` block-scalar indicator on a
    single key. Not safe for arbitrary YAML — only for the receipt
    convention. Avoids the pyyaml dependency in the parser to keep the
    test surface light.
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "|":
            # collect the indented block
            block: list[str] = []
            indent = None
            while i < len(lines):
                cont = lines[i]
                if cont.strip() == "":
                    block.append("")
                    i += 1
                    continue
                cont_indent = len(cont) - len(cont.lstrip())
                if indent is None:
                    indent = cont_indent
                if cont_indent < indent:
                    break
                block.append(cont[indent:] if cont_indent >= indent else cont)
                i += 1
            out[key] = "\n".join(block).rstrip()
        else:
            out[key] = value.strip('"').strip("'")
    return out
