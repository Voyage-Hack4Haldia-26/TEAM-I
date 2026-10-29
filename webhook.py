"""GitHub webhook helpers: HMAC signature verification and payload extraction."""
import hashlib
import hmac
from typing import Optional

from github_client import parse_repo_full_name, GitHubError

SUPPORTED_ACTIONS = {"opened", "synchronize", "reopened"}


class PayloadError(ValueError):
    """The webhook JSON was valid but did not look like a pull_request event."""


def verify_github_signature(secret: str, raw_body: bytes, header: Optional[str]) -> bool:
    """Check X-Hub-Signature-256 against HMAC-SHA256(secret, raw_body).

    `raw_body` must be the exact bytes GitHub sent - re-serialising parsed JSON
    would change whitespace/ordering and break the signature.
    """
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # compare_digest runs in constant time, so response timing can't leak the signature
    return hmac.compare_digest(expected.encode("utf-8"), header.encode("utf-8"))


def extract_pr_event(payload) -> dict:
    """Pull the fields we need out of a pull_request payload, validating each one."""
    if not isinstance(payload, dict):
        raise PayloadError("Payload must be a JSON object")
    pr = payload.get("pull_request")
    repo = payload.get("repository")
    if not isinstance(pr, dict) or not isinstance(repo, dict):
        raise PayloadError("Payload is missing 'pull_request' or 'repository'")

    try:
        owner, name = parse_repo_full_name(repo.get("full_name"))
    except GitHubError as exc:
        raise PayloadError(exc.message) from None

    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise PayloadError("Invalid pull request number")

    html_url = pr.get("html_url")
    if not isinstance(html_url, str) or not html_url.startswith("https://github.com/"):
        raise PayloadError("Invalid pull request URL")

    user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}

    return {
        "repository_full_name": f"{owner}/{name}",
        "owner": owner,
        "repo": name,
        "pr_number": number,
        "pr_title": str(pr.get("title") or "")[:500],
        "pr_url": html_url[:500],
        "pr_author": str(user.get("login") or "unknown")[:100],
        "base_branch": str(base.get("ref") or "")[:250],
        "head_branch": str(head.get("ref") or "")[:250],
        "head_sha": str(head.get("sha") or "")[:64] or None,
    }
