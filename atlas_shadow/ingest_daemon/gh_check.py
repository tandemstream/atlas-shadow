"""gh_check — GitHub Checks API integration (T7).

The pre-merge gate transitions the ``atlas-shadow-grading`` check_run
through ``in_progress`` -> ``completed`` (with ``conclusion`` in
``success`` / ``failure`` / ``neutral``).

This module is a thin wrapper around the REST API:

  * ``create_check_in_progress(...)`` — POST /repos/{owner}/{repo}/check-runs
    (status=in_progress). Returns the ``check_run_id`` the daemon stores
    until grading completes.
  * ``update_check_complete(...)`` — PATCH /repos/{owner}/{repo}/check-runs/
    {check_run_id} (status=completed + conclusion + output).

Authentication: GitHub PAT or fine-grained token in the
``GITHUB_ATLAS_SHADOW_TOKEN`` env var. Required scopes: ``checks:write``
on the target repo. The daemon refuses to call these helpers when the
token is missing (caller-side check).

All requests go through stdlib ``urllib.request``; no new transitive deps.
``_http`` injection seam lets tests stub the response without monkey-
patching urllib.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional


GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "atlas-shadow-pre-merge-grader/1.0"
DEFAULT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class HttpResponse:
    """Minimal HTTP response shape consumed by gh_check / pr_comment."""

    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


def github_token_from_env(env_var: str = "GITHUB_ATLAS_SHADOW_TOKEN") -> Optional[str]:
    """Resolve the GitHub PAT from the env var. Returns None when absent.

    Caller treats absence as a configuration error (the grading gate
    can't post checks/comments without a token).
    """
    return os.environ.get(env_var) or None


def http_request(
    *,
    method: str,
    url: str,
    body: Optional[bytes] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> HttpResponse:
    """Default HTTP transport. Tests inject a replacement via ``_http=...``.

    Wraps ``urllib.request.urlopen`` so 4xx/5xx responses come back as
    :class:`HttpResponse` (with the response body), not raised exceptions.
    Connection errors (DNS, refused, timeout) still raise ``OSError``.
    """
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(
                status=resp.status,
                body=resp.read(),
                headers=dict(resp.headers),
            )
    except urllib.error.HTTPError as exc:
        body_bytes = b""
        try:
            body_bytes = exc.read() or b""
        except Exception:
            body_bytes = b""
        return HttpResponse(
            status=exc.code,
            body=body_bytes,
            headers=dict(exc.headers or {}),
        )


def _auth_headers(github_token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def create_check_in_progress(
    *,
    repo_full_name: str,
    head_sha: str,
    name: str = "atlas-shadow-grading",
    details_url: Optional[str] = None,
    github_token: str,
    _http: Callable = http_request,
) -> dict[str, Any]:
    """POST a new ``check_run`` in ``in_progress`` state.

    Returns the parsed JSON body (which carries the ``id`` the daemon
    stores until ``update_check_complete``). Raises :class:`RuntimeError`
    on non-2xx responses with the GitHub error body for context.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/check-runs"
    payload: dict[str, Any] = {
        "name": name,
        "head_sha": head_sha,
        "status": "in_progress",
    }
    if details_url:
        payload["details_url"] = details_url
    resp = _http(
        method="POST",
        url=url,
        body=json.dumps(payload).encode("utf-8"),
        headers=_auth_headers(github_token),
    )
    if not (200 <= resp.status < 300):
        raise RuntimeError(
            f"create_check_in_progress failed: status={resp.status} "
            f"body={resp.body[:500]!r}"
        )
    return resp.json() or {}


def update_check_complete(
    *,
    repo_full_name: str,
    check_run_id: int,
    conclusion: str,
    output_title: str,
    output_summary: str,
    output_text: Optional[str] = None,
    github_token: str,
    _http: Callable = http_request,
) -> dict[str, Any]:
    """PATCH an existing check_run to ``status=completed`` with a conclusion.

    Args:
      conclusion: one of ``success`` / ``failure`` / ``neutral`` /
        ``cancelled`` / ``skipped`` / ``timed_out`` / ``action_required``.
        Per D-P2-5, the grading gate uses ``success`` (soft-pass) or
        ``failure`` (hard-fail); other values are passed through for
        forward compatibility.
      output_title / output_summary / output_text: rendered on the GH
        check-run UI. Summary is markdown.
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/check-runs/{check_run_id}"
    output: dict[str, Any] = {
        "title": output_title,
        "summary": output_summary,
    }
    if output_text:
        output["text"] = output_text
    payload = {
        "status": "completed",
        "conclusion": conclusion,
        "output": output,
    }
    resp = _http(
        method="PATCH",
        url=url,
        body=json.dumps(payload).encode("utf-8"),
        headers=_auth_headers(github_token),
    )
    if not (200 <= resp.status < 300):
        raise RuntimeError(
            f"update_check_complete failed: status={resp.status} "
            f"body={resp.body[:500]!r}"
        )
    return resp.json() or {}
