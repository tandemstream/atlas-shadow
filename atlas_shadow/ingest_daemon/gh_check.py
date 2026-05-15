"""gh_check — GitHub commit-status integration (T7).

The pre-merge gate posts a ``atlas-shadow-grading`` commit status that
transitions through ``pending`` -> ``success`` / ``failure`` / ``error``
on the PR's head SHA.

**Why commit statuses, not Check Runs?** The Checks API
(``/repos/{owner}/{repo}/check-runs``) is **only available to GitHub
Apps** — it returns 403 ``You must authenticate via a GitHub App`` when
called with a personal access token (caught during T12 smoketest,
2026-05-15). Commit Statuses (``/repos/{owner}/{repo}/statuses/{sha}``)
accept PAT auth, integrate with branch protection ``Settings -> Branches
-> Required status checks`` the same way, and display in the same PR
``Checks`` UI tab. The migration path to a GitHub App (richer per-check
annotations, line-level details) is deferred to v1.1 if needed.

This module is a thin wrapper around the REST API:

  * ``post_pending_status(...)`` — POST /repos/{owner}/{repo}/statuses/{sha}
    with ``state=pending``. Initial entry posted when the PR webhook
    arrives.
  * ``post_final_status(...)`` — POST same endpoint with
    ``state=success | failure | error``. Replaces the prior status on
    the (sha, context) pair (GitHub shows only the latest in the UI).

Authentication: GitHub PAT or fine-grained token in the
``GITHUB_ATLAS_SHADOW_TOKEN`` env var. Required scope: ``repo:status``
(included in the broader ``repo`` scope). The daemon refuses to call
these helpers when the token is missing (caller-side check).

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


STATUS_CONTEXT = "atlas-shadow-grading"
_VALID_STATES = frozenset({"pending", "success", "failure", "error"})


def post_status(
    *,
    repo_full_name: str,
    head_sha: str,
    state: str,
    description: str,
    context: str = STATUS_CONTEXT,
    target_url: Optional[str] = None,
    github_token: str,
    _http: Callable = http_request,
) -> dict[str, Any]:
    """POST a commit status to ``/repos/{owner}/{repo}/statuses/{sha}``.

    Args:
      state: one of ``pending`` / ``success`` / ``failure`` / ``error``.
        Per D-P2-5: receipts-passed-threshold -> ``success``,
        receipts-failed-threshold -> ``failure``, operational error ->
        ``success`` (soft-pass; description carries the error so
        operators see it without blocking merge).
      description: short string (max 140 chars per GH; we truncate).
        Shown next to the status in the PR's Checks tab.
      context: status context name. Branch-protection rules match on
        this string; default ``atlas-shadow-grading``.
      target_url: optional URL the status badge links to (e.g., the
        shadow-runs artifact path or the PR comment anchor).
    """
    if state not in _VALID_STATES:
        raise ValueError(
            f"state must be one of {sorted(_VALID_STATES)}; got {state!r}"
        )
    # GitHub caps description at 140 chars. Truncate with ellipsis.
    if description and len(description) > 140:
        description = description[:137].rstrip() + "..."
    payload: dict[str, Any] = {
        "state": state,
        "context": context,
        "description": description or "",
    }
    if target_url:
        payload["target_url"] = target_url
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/statuses/{head_sha}"
    resp = _http(
        method="POST",
        url=url,
        body=json.dumps(payload).encode("utf-8"),
        headers=_auth_headers(github_token),
    )
    if not (200 <= resp.status < 300):
        raise RuntimeError(
            f"post_status failed: status={resp.status} body={resp.body[:500]!r}"
        )
    return resp.json() or {}


def post_pending_status(
    *,
    repo_full_name: str,
    head_sha: str,
    description: str = "grading in progress",
    github_token: str,
    _http: Callable = http_request,
) -> dict[str, Any]:
    """Convenience wrapper: POST state=pending. Initial entry on PR-open."""
    return post_status(
        repo_full_name=repo_full_name,
        head_sha=head_sha,
        state="pending",
        description=description,
        github_token=github_token,
        _http=_http,
    )


def post_final_status(
    *,
    repo_full_name: str,
    head_sha: str,
    state: str,
    description: str,
    target_url: Optional[str] = None,
    github_token: str,
    _http: Callable = http_request,
) -> dict[str, Any]:
    """Convenience wrapper: POST state=success|failure|error. Replaces
    the prior pending status on the (sha, context) pair (GitHub shows
    only the latest in the UI).
    """
    return post_status(
        repo_full_name=repo_full_name,
        head_sha=head_sha,
        state=state,
        description=description,
        target_url=target_url,
        github_token=github_token,
        _http=_http,
    )


# Backward-compat aliases — for code paths that still reference the
# Check Runs-style names. Removed once nothing in atlas-shadow imports
# them.
def create_check_in_progress(*, repo_full_name, head_sha, github_token,
                             name=STATUS_CONTEXT, details_url=None,
                             _http=http_request):  # pragma: no cover
    """DEPRECATED: kept for backward import compatibility. Use
    :func:`post_pending_status` instead. The Check Runs API requires a
    GitHub App; this wrapper now posts a commit status."""
    return post_pending_status(
        repo_full_name=repo_full_name,
        head_sha=head_sha,
        github_token=github_token,
        _http=_http,
    )


def update_check_complete(*, repo_full_name, check_run_id, conclusion,
                          output_title, output_summary, github_token,
                          output_text=None, _http=http_request):  # pragma: no cover
    """DEPRECATED: kept for backward import compatibility. Use
    :func:`post_final_status` instead."""
    # check_run_id is ignored; commit statuses don't have an id-based
    # update model (each POST replaces the prior latest per context).
    # Map Check Runs conclusion values to commit-status states.
    _MAP = {"success": "success", "failure": "failure", "neutral": "success",
            "cancelled": "error", "skipped": "success", "timed_out": "error",
            "action_required": "failure"}
    state = _MAP.get(conclusion, "error")
    # The caller hands us a `head_sha` indirectly via the prior
    # `post_pending_status` call; recovering it via the (unused)
    # `check_run_id` isn't possible, so this back-compat wrapper requires
    # callers that still use it to also pass `head_sha` explicitly.
    raise NotImplementedError(
        "update_check_complete back-compat wrapper requires explicit head_sha; "
        "switch the caller to post_final_status(head_sha=..., state=..., "
        "description=...) directly."
    )
