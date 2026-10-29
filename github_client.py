"""Small GitHub REST API client (authenticated with a server-side token).

URLs are always built from validated owner/repo/number values - never from a
URL supplied by a user or found inside a webhook payload.
"""
import logging
import re
import time
from typing import List, Optional, Tuple

import requests

from retry import retry_call

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_RATE_LIMIT_WAIT = 30  # seconds we are willing to sleep for a rate limit


class GitHubError(Exception):
    """A GitHub API problem, with a safe message that can be shown to users."""

    def __init__(self, message: str, status: Optional[int] = None,
                 retryable: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


def parse_repo_full_name(full_name) -> Tuple[str, str]:
    """Validate 'owner/repo' and return (owner, repo)."""
    if not isinstance(full_name, str) or full_name.count("/") != 1:
        raise GitHubError("Repository must look like 'owner/name'")
    owner, name = full_name.split("/")
    for part in (owner, name):
        if not _NAME_RE.match(part) or part in {".", ".."}:
            raise GitHubError("Repository owner or name contains invalid characters")
    return owner, name


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        try:
            return max(0.0, float(resp.headers["X-RateLimit-Reset"]) - time.time()) + 1
        except (KeyError, ValueError):
            return None
    return None


def _normalize_file(item: dict) -> dict:
    return {
        "filename": str(item.get("filename", ""))[:500],
        "status": str(item.get("status", "modified")),
        "additions": int(item.get("additions") or 0),
        "deletions": int(item.get("deletions") or 0),
        "changes": int(item.get("changes") or 0),
        "previous_filename": item.get("previous_filename"),
        "patch_available": bool(item.get("patch")),
        "patch": item.get("patch"),  # used only to rebuild a diff; never stored in the DB
    }


def build_diff_from_files(files: List[dict]) -> str:
    """Fallback: rebuild a unified diff from per-file patches when GitHub won't return the full diff."""
    parts = []
    for f in files:
        old = f.get("previous_filename") or f["filename"]
        header = f"diff --git a/{old} b/{f['filename']}\n"
        if f.get("status") == "renamed":
            header += f"rename from {old}\nrename to {f['filename']}\n"
        if f.get("patch"):
            parts.append(f"{header}--- a/{old}\n+++ b/{f['filename']}\n{f['patch']}\n")
        else:
            parts.append(f"{header}[no textual patch available: binary file, or too large for GitHub to include]\n")
    return "".join(parts)


class GitHubClient:
    def __init__(self, token: str, timeout: int = 15, attempts: int = 3):
        self.timeout = timeout
        self.attempts = attempts
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-summarizer-bot",
        })

    # ------------------------------------------------------------------ core
    def _raise_for_status(self, resp: requests.Response) -> None:
        status = resp.status_code
        if status < 400:
            return
        if status == 401:
            raise GitHubError("GitHub rejected the token (401). Check GITHUB_TOKEN.", 401)
        if status in (403, 429):
            wait = _retry_after_seconds(resp)
            if status == 429 or wait is not None:
                if wait is not None and wait > MAX_RATE_LIMIT_WAIT:
                    raise GitHubError(
                        f"GitHub rate limit reached; resets in about {int(wait)}s.", status)
                raise GitHubError("GitHub rate limit reached.", status, retryable=True, retry_after=wait)
            raise GitHubError(
                "GitHub denied access (403). The token may lack permission for this repository.", 403)
        if status == 404:
            raise GitHubError("Pull request or repository not found (or the token cannot see it).", 404)
        if status in (406, 413, 422):
            raise GitHubError("GitHub could not return this data (too large or invalid request).", status)
        if status >= 500:
            raise GitHubError(f"GitHub server error ({status}).", status, retryable=True)
        raise GitHubError(f"Unexpected GitHub response ({status}).", status)

    def _send(self, method: str, url: str, *, params=None, json_body=None,
              accept: str = "application/vnd.github+json", stream: bool = False) -> requests.Response:
        def attempt():
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body,
                    headers={"Accept": accept}, timeout=self.timeout, stream=stream)
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise GitHubError("GitHub API was unreachable or timed out.", retryable=True) from exc
            self._raise_for_status(resp)
            return resp

        return retry_call(
            attempt,
            is_retryable=lambda e: isinstance(e, GitHubError) and e.retryable,
            attempts=self.attempts,
            get_delay_hint=lambda e: getattr(e, "retry_after", None),
        )

    @staticmethod
    def _json(resp: requests.Response):
        try:
            return resp.json()
        except ValueError:
            raise GitHubError("GitHub returned a response that is not valid JSON.") from None

    # --------------------------------------------------------------- reading
    def get_pr(self, owner: str, repo: str, number: int) -> dict:
        data = self._json(self._send("GET", f"{API_ROOT}/repos/{owner}/{repo}/pulls/{int(number)}"))
        if not isinstance(data, dict):
            raise GitHubError("Unexpected response shape from GitHub.")
        return data

    def list_pr_files(self, owner: str, repo: str, number: int, max_files: int) -> Tuple[List[dict], bool]:
        """Return (files, truncated). Follows pagination until max_files is reached."""
        files: List[dict] = []
        truncated = False
        page = 1
        while True:
            resp = self._send(
                "GET", f"{API_ROOT}/repos/{owner}/{repo}/pulls/{int(number)}/files",
                params={"per_page": 100, "page": page})
            batch = self._json(resp)
            if not isinstance(batch, list):
                raise GitHubError("Unexpected response shape from GitHub.")
            for item in batch:
                if len(files) >= max_files:
                    truncated = True
                    break
                files.append(_normalize_file(item))
            if truncated or len(batch) < 100:
                break
            page += 1
            if page > 30:  # GitHub exposes at most 3000 files per PR
                truncated = True
                break
        return files, truncated

    def get_diff(self, owner: str, repo: str, number: int, max_bytes: int) -> Tuple[str, bool]:
        """Download the unified diff (media type application/vnd.github.diff).

        Returns (text, truncated). Reading stops after max_bytes so a huge PR cannot exhaust memory.
        """
        resp = self._send(
            "GET", f"{API_ROOT}/repos/{owner}/{repo}/pulls/{int(number)}",
            accept="application/vnd.github.diff", stream=True)
        buf = bytearray()
        truncated = False
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    truncated = True
                    break
        finally:
            resp.close()
        return bytes(buf[:max_bytes]).decode("utf-8", errors="replace"), truncated

    # --------------------------------------------------------------- writing
    def find_bot_comment(self, owner: str, repo: str, number: int, marker: str, max_pages: int = 5) -> Optional[int]:
        for page in range(1, max_pages + 1):
            resp = self._send(
                "GET", f"{API_ROOT}/repos/{owner}/{repo}/issues/{int(number)}/comments",
                params={"per_page": 100, "page": page})
            batch = self._json(resp)
            if not isinstance(batch, list):
                return None
            for comment in batch:
                if marker in str(comment.get("body", "")):
                    return comment.get("id")
            if len(batch) < 100:
                break
        return None

    def upsert_comment(self, owner: str, repo: str, number: int, marker: str, body: str) -> str:
        """Create the bot comment, or edit the existing one (so retries never duplicate it)."""
        existing = self.find_bot_comment(owner, repo, number, marker)
        if existing:
            try:
                self._send("PATCH", f"{API_ROOT}/repos/{owner}/{repo}/issues/comments/{int(existing)}",
                           json_body={"body": body})
                return "updated"
            except GitHubError as exc:
                if exc.status not in (403, 404):
                    raise
        self._send("POST", f"{API_ROOT}/repos/{owner}/{repo}/issues/{int(number)}/comments",
                   json_body={"body": body})
        return "created"

    def add_labels(self, owner: str, repo: str, number: int, labels: List[str]) -> None:
        self._send("POST", f"{API_ROOT}/repos/{owner}/{repo}/issues/{int(number)}/labels",
                   json_body={"labels": labels})
