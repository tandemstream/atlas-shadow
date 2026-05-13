"""grader — LLM-as-judge that compares Atlas's answer to the oracle excerpt.

The grader emits one of 4 grades:

- ``full_match``      — Atlas's answer captures the oracle claim faithfully.
- ``partial_match``   — Atlas's answer overlaps the claim but is incomplete
                        or has minor inaccuracies.
- ``no_match``        — Atlas returned something, but it doesn't answer the
                        question or contradicts the claim.
- ``atlas_not_found`` — Atlas returned no citations / empty answer text /
                        explicit "source unavailable" marker.

Implementation uses the Anthropic SDK. Model is config-driven. The prompt
explicitly forbids the grader from using outside knowledge — it grades only
on whether Atlas's `answer_text` is consistent with the oracle excerpt + claim.

A stub mode is supported for tests: pass ``api_key=None`` and inject a
``_client`` factory or set ``ATLAS_SHADOW_GRADER_STUB=1`` in the env.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

GRADE_VALUES = ("full_match", "partial_match", "no_match", "atlas_not_found")


@dataclass(frozen=True)
class GraderResponse:
    grade: str
    confidence: float
    rationale: str
    latency_ms: int
    raw: str = ""


SYSTEM_PROMPT = """You are an automated grader for a code-question benchmark.

You will be given:
1. A question about a specific repository state.
2. An oracle excerpt (verbatim source text that answers the question) and a
   one-sentence oracle claim about what the excerpt says.
3. Atlas's answer to the question (a natural-language `answer_text`,
   possibly empty, possibly an explicit "source unavailable" marker).

Your job: judge whether Atlas's answer is consistent with the oracle. Emit
ONE of the following grades exactly:

- full_match: Atlas's answer captures the oracle claim accurately and
  completely. Minor wording differences are fine; the substantive facts
  must match.
- partial_match: Atlas's answer overlaps with the oracle claim but is
  incomplete OR has minor inaccuracies that don't fully contradict it.
- no_match: Atlas returned content, but it doesn't answer the question OR
  contradicts the oracle claim.
- atlas_not_found: Atlas's answer_text is empty, only "(no code citations
  returned)", "(no chunks returned)", or contains a "(source unavailable:
  commit ... not in this repo)" marker for the cited evidence.

Do NOT use outside knowledge. Grade strictly on whether Atlas's
`answer_text` is consistent with the provided oracle excerpt + claim.

Output STRICTLY a JSON object with this schema (and nothing else):

{
  "grade": "full_match" | "partial_match" | "no_match" | "atlas_not_found",
  "confidence": <float between 0.0 and 1.0>,
  "rationale": "<1-2 sentences explaining the grade>"
}
"""


def _build_user_prompt(
    *,
    question: str,
    oracle_excerpt: str,
    oracle_claim: str,
    atlas_answer_text: str,
) -> str:
    return (
        f"## Question\n{question}\n\n"
        f"## Oracle excerpt\n```\n{oracle_excerpt}\n```\n\n"
        f"## Oracle claim\n{oracle_claim}\n\n"
        f"## Atlas answer_text\n```\n{atlas_answer_text}\n```\n\n"
        "Grade Atlas's answer per the rules above. Output JSON only."
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a model response.

    Models sometimes wrap JSON in code fences; be tolerant.
    """
    text = text.strip()
    # Strip ``` fences
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    # Find the first balanced { ... } block
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found in grader response")
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                return json.loads(blob)
    raise ValueError("unbalanced JSON in grader response")


def _normalize_grade(value: str) -> str:
    v = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in GRADE_VALUES:
        return v
    raise ValueError(f"grader returned invalid grade: {value!r}")


def _heuristic_short_circuit(atlas_answer_text: str) -> Optional[GraderResponse]:
    """Cheap pre-grader check for the obvious atlas_not_found case.

    Saves a grader API call when Atlas returned nothing. Also catches the
    revision-faithful "(source unavailable: commit ... not in this repo)"
    marker introduced in PR-r4 of Phase 2.A.
    """
    txt = (atlas_answer_text or "").strip()
    if not txt:
        return GraderResponse(
            grade="atlas_not_found",
            confidence=1.0,
            rationale="Atlas returned an empty answer_text.",
            latency_ms=0,
        )
    if txt in {"(no code citations returned)", "(no chunks returned)"}:
        return GraderResponse(
            grade="atlas_not_found",
            confidence=1.0,
            rationale=f"Atlas returned the sentinel string: {txt}",
            latency_ms=0,
        )
    if "source unavailable: commit" in txt and "not in this repo" in txt:
        return GraderResponse(
            grade="atlas_not_found",
            confidence=1.0,
            rationale=(
                "Atlas returned a citation but the cited commit is not in "
                "the local repo (revision-faithful read failed)."
            ),
            latency_ms=0,
        )
    return None


def _anthropic_client(api_key: Optional[str]):
    """Lazy-import the Anthropic SDK so unit tests that stub the grader
    don't require the dependency at collection time."""
    try:
        import anthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "anthropic SDK not installed; run `make setup`."
        ) from exc
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def _claude_cli_grade(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout: int = 120,
) -> str:
    """Shell out to the `claude` CLI to grade.

    Used when ``ATLAS_SHADOW_GRADER_BACKEND=claude_cli`` (or when no
    ``ANTHROPIC_API_KEY`` is set and the CLI is available). Composes a
    single prompt by concatenating system + user, drops the
    ``ANTHROPIC_API_KEY`` sentinel from the subprocess env (per the Atlas
    pattern in ``core/ai.py`` — the CLI uses Claude Code's keychain, not
    env), and parses stdout for the grader's JSON block.

    Returns the raw stdout text; the caller is responsible for JSON
    extraction.
    """
    import subprocess as _sp

    prompt = f"<system>\n{system_prompt}\n</system>\n\n<user>\n{user_prompt}\n</user>"
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    # --print: non-interactive stdout. We deliberately do NOT pass --bare
    # because that disables keychain auth; the grader relies on Claude
    # Code's keychain credentials in the no-API-key case.
    cmd = ["claude", "--print", "--model", model, prompt]
    proc = _sp.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (rc={proc.returncode}): stderr={proc.stderr[:500]}"
        )
    return proc.stdout


def grade(
    *,
    question: str,
    oracle_excerpt: str,
    oracle_claim: str,
    atlas_answer_text: str,
    model: str,
    api_key: Optional[str] = None,
    max_tokens: int = 512,
    _client=None,
) -> GraderResponse:
    """Grade a single response.

    ``_client`` is injectable for tests. When the heuristic short-circuit
    fires (Atlas returned nothing), no API call is made.
    """
    short = _heuristic_short_circuit(atlas_answer_text)
    if short is not None:
        return short

    if os.environ.get("ATLAS_SHADOW_GRADER_STUB"):
        return GraderResponse(
            grade="partial_match",
            confidence=0.5,
            rationale="stubbed grader (ATLAS_SHADOW_GRADER_STUB=1)",
            latency_ms=0,
            raw="",
        )

    user_prompt = _build_user_prompt(
        question=question,
        oracle_excerpt=oracle_excerpt,
        oracle_claim=oracle_claim,
        atlas_answer_text=atlas_answer_text,
    )

    backend = os.environ.get("ATLAS_SHADOW_GRADER_BACKEND", "").strip().lower()
    use_cli = backend == "claude_cli" or (
        not backend and not api_key and not os.environ.get("ANTHROPIC_API_KEY")
        and _client is None
    )

    if use_cli:
        start = time.perf_counter()
        try:
            raw_text = _claude_cli_grade(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                model=model,
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return GraderResponse(
                grade="no_match",
                confidence=0.0,
                rationale=f"claude CLI failed: {exc}",
                latency_ms=elapsed_ms,
                raw="",
            )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
    else:
        client = _client if _client is not None else _anthropic_client(api_key)
        start = time.perf_counter()
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        # Anthropic SDK returns content as a list of blocks; the first
        # text block carries the JSON. Be permissive about shape.
        text_parts: list[str] = []
        for block in getattr(msg, "content", []) or []:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            else:
                t = getattr(block, "text", None)
                if t:
                    text_parts.append(t)
        raw_text = "\n".join(text_parts).strip()

    try:
        parsed = _extract_json(raw_text)
        grade_value = _normalize_grade(parsed["grade"])
        confidence = float(parsed.get("confidence", 0.0))
        rationale = str(parsed.get("rationale", ""))
    except Exception as exc:
        return GraderResponse(
            grade="no_match",
            confidence=0.0,
            rationale=f"grader parse error: {exc}; raw={raw_text[:200]}",
            latency_ms=elapsed_ms,
            raw=raw_text,
        )

    return GraderResponse(
        grade=grade_value,
        confidence=max(0.0, min(1.0, confidence)),
        rationale=rationale,
        latency_ms=elapsed_ms,
        raw=raw_text,
    )
