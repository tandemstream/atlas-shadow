"""Tests for atlas_shadow.grader.

The Anthropic client is stubbed via injection — no API calls in tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from atlas_shadow import grader as grader_mod


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [SimpleNamespace(text=text)]


class _FakeClient:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls: list[dict] = []

        class _Messages:
            def __init__(self, parent):
                self._parent = parent

            def create(self, **kwargs):
                self._parent.calls.append(kwargs)
                return _FakeMessage(self._parent.response_text)

        self.messages = _Messages(self)


def test_heuristic_short_circuits_empty_answer():
    result = grader_mod.grade(
        question="q?",
        oracle_excerpt="e",
        oracle_claim="c",
        atlas_answer_text="",
        model="any",
        _client=_FakeClient(""),
    )
    assert result.grade == "atlas_not_found"
    assert result.confidence == 1.0
    assert result.latency_ms == 0


def test_heuristic_short_circuits_no_citations_sentinel():
    result = grader_mod.grade(
        question="q?",
        oracle_excerpt="e",
        oracle_claim="c",
        atlas_answer_text="(no code citations returned)",
        model="any",
        _client=_FakeClient(""),
    )
    assert result.grade == "atlas_not_found"


def test_heuristic_short_circuits_no_chunks_sentinel():
    result = grader_mod.grade(
        question="q?",
        oracle_excerpt="e",
        oracle_claim="c",
        atlas_answer_text="(no chunks returned)",
        model="any",
        _client=_FakeClient(""),
    )
    assert result.grade == "atlas_not_found"


def test_heuristic_short_circuits_source_unavailable_marker():
    """PR-r4 fix: explicit-failure marker for commits not in repo."""
    text = (
        "foo/bar.py:1-5 @abc123\n"
        "(source unavailable: commit abc123 not in this repo)"
    )
    result = grader_mod.grade(
        question="q?",
        oracle_excerpt="e",
        oracle_claim="c",
        atlas_answer_text=text,
        model="any",
        _client=_FakeClient(""),
    )
    assert result.grade == "atlas_not_found"
    assert "revision-faithful" in result.rationale.lower() or "not in" in result.rationale.lower()


def test_grade_calls_client_and_parses_json_response():
    fake = _FakeClient(
        json.dumps(
            {"grade": "full_match", "confidence": 0.92, "rationale": "Matches exactly."}
        )
    )
    result = grader_mod.grade(
        question="q?",
        oracle_excerpt="def foo(): return 42",
        oracle_claim="foo returns 42",
        atlas_answer_text="def foo(): return 42",
        model="claude-3-5-sonnet-20241022",
        _client=fake,
    )
    assert result.grade == "full_match"
    assert result.confidence == 0.92
    assert result.rationale == "Matches exactly."
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "claude-3-5-sonnet-20241022"


def test_grade_handles_fenced_json_response():
    fake = _FakeClient(
        '```json\n{"grade":"partial_match","confidence":0.6,"rationale":"some overlap"}\n```'
    )
    result = grader_mod.grade(
        question="q?",
        oracle_excerpt="e",
        oracle_claim="c",
        atlas_answer_text="atlas said something",
        model="any",
        _client=fake,
    )
    assert result.grade == "partial_match"
    assert result.confidence == 0.6


def test_grade_normalizes_hyphenated_grade():
    fake = _FakeClient(
        '{"grade":"full-match","confidence":1.0,"rationale":"x"}'
    )
    result = grader_mod.grade(
        question="q?", oracle_excerpt="e", oracle_claim="c",
        atlas_answer_text="x", model="any", _client=fake,
    )
    assert result.grade == "full_match"


def test_grade_parse_failure_becomes_no_match():
    fake = _FakeClient("garbage with no json")
    result = grader_mod.grade(
        question="q?", oracle_excerpt="e", oracle_claim="c",
        atlas_answer_text="x", model="any", _client=fake,
    )
    assert result.grade == "no_match"
    assert result.confidence == 0.0
    assert "parse error" in result.rationale


def test_grade_invalid_grade_value_becomes_no_match():
    fake = _FakeClient('{"grade":"bogus","confidence":0.9,"rationale":"x"}')
    result = grader_mod.grade(
        question="q?", oracle_excerpt="e", oracle_claim="c",
        atlas_answer_text="x", model="any", _client=fake,
    )
    assert result.grade == "no_match"


def test_grade_confidence_clamped_to_unit_interval():
    fake = _FakeClient('{"grade":"full_match","confidence":1.7,"rationale":"x"}')
    result = grader_mod.grade(
        question="q?", oracle_excerpt="e", oracle_claim="c",
        atlas_answer_text="x", model="any", _client=fake,
    )
    assert result.confidence == 1.0


def test_grade_stub_mode_via_env(monkeypatch):
    monkeypatch.setenv("ATLAS_SHADOW_GRADER_STUB", "1")
    result = grader_mod.grade(
        question="q?", oracle_excerpt="e", oracle_claim="c",
        atlas_answer_text="some answer", model="any",
    )
    assert result.grade == "partial_match"
    assert "stubbed" in result.rationale
