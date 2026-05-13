"""Tests for atlas_shadow.parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_shadow import parser as parser_mod


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def test_parse_dogfood_v2_jsonl_fixture_has_22_entries():
    receipts = parser_mod.parse_dogfood_v2_jsonl(FIXTURE_DIR / "dogfood-v2-questions.jsonl")
    assert len(receipts) == 22


def test_parse_dogfood_v2_jsonl_first_entry_shape():
    receipts = parser_mod.parse_dogfood_v2_jsonl(FIXTURE_DIR / "dogfood-v2-questions.jsonl")
    r = receipts[0]
    assert r.question_id == "Q01"
    assert "record_instruction" in r.question
    assert r.commit_sha == "87aa9fa"
    assert r.source_path == "Atlas/memory/core/instructions/store.py"
    assert r.source_lines == "214-230"
    assert "def record_instruction(" in r.oracle_excerpt
    assert r.oracle_claim, "claim must be non-empty"
    assert r.class_label == "C1"


def test_parse_dogfood_v2_jsonl_all_have_required_fields():
    receipts = parser_mod.parse_dogfood_v2_jsonl(FIXTURE_DIR / "dogfood-v2-questions.jsonl")
    for r in receipts:
        assert r.question_id.startswith("Q"), f"unexpected id: {r.question_id}"
        assert r.question, f"empty question for {r.question_id}"
        assert r.oracle_excerpt, f"empty excerpt for {r.question_id}"
        assert r.oracle_claim, f"empty claim for {r.question_id}"


def test_parse_dogfood_v2_jsonl_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parser_mod.parse_dogfood_v2_jsonl(tmp_path / "missing.jsonl")


def test_parse_dogfood_v2_jsonl_invalid_json_raises(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("this is not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        parser_mod.parse_dogfood_v2_jsonl(bad)


def test_parse_dogfood_v2_jsonl_blank_lines_skipped(tmp_path):
    p = tmp_path / "mixed.jsonl"
    p.write_text(
        '{"id":"Q1","question":"q?","ingest_answer":{"evidence_excerpt":"e","claim":"c"}}\n'
        "\n"
        '{"id":"Q2","question":"q?","ingest_answer":{"evidence_excerpt":"e","claim":"c"}}\n',
        encoding="utf-8",
    )
    receipts = parser_mod.parse_dogfood_v2_jsonl(p)
    assert [r.question_id for r in receipts] == ["Q1", "Q2"]


def test_parse_fixture_auto_detect_jsonl():
    receipts = parser_mod.parse_fixture(FIXTURE_DIR / "dogfood-v2-questions.jsonl")
    assert len(receipts) == 22


def test_parse_fixture_unsupported_format_raises(tmp_path):
    p = tmp_path / "weird.csv"
    p.write_text("a,b,c\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported fixture format"):
        parser_mod.parse_fixture(p)


def test_parse_qna_log_markdown_basic(tmp_path):
    p = tmp_path / "02-qna-log.md"
    p.write_text(
        """# Q&A log

## q1 [qa:q1]

```yaml
question: what is foo?
source_path: foo/bar.py
source_lines: 1-3
commit_sha: deadbeef
evidence_excerpt: |
  def foo():
      return 42
claim: foo returns 42.
```

## q2 [qa:q2]

```yaml
question: what is bar?
source_path: foo/baz.py
source_lines: 10-12
commit_sha: deadbeef
evidence_excerpt: |
  def bar():
      return 'bar'
claim: bar returns 'bar'.
```
""",
        encoding="utf-8",
    )
    receipts = parser_mod.parse_qna_log_markdown(p)
    assert len(receipts) == 2
    assert receipts[0].question_id == "q1"
    assert receipts[0].oracle_claim == "foo returns 42."
    assert "def foo()" in receipts[0].oracle_excerpt
    assert receipts[1].question_id == "q2"
    assert receipts[1].source_path == "foo/baz.py"
