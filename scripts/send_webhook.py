"""Send a pull_request webhook to a running server - handy for demos and local testing.

  python scripts/send_webhook.py --url http://127.0.0.1:5000 --repo OWNER/REPO --pr 1
  python scripts/send_webhook.py --url https://YOUR-APP.onrender.com --repo OWNER/REPO --pr 1 --bad-signature

* The repo and PR number must be REAL: the server fetches the real diff from GitHub.
* GITHUB_WEBHOOK_SECRET is read from the environment / .env (same value the server uses).
* --bad-signature signs with the wrong secret, so the server must answer 401.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid

import requests
from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="server base URL, e.g. http://127.0.0.1:5000")
    parser.add_argument("--repo", required=True, help="owner/name of a real repository")
    parser.add_argument("--pr", required=True, type=int, help="a real pull request number")
    parser.add_argument("--action", default="opened", choices=["opened", "synchronize", "reopened"])
    parser.add_argument("--delivery", default=None, help="reuse a delivery id to test duplicate handling")
    parser.add_argument("--bad-signature", action="store_true", help="sign with the wrong secret")
    args = parser.parse_args()

    load_dotenv()
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET is not set (put it in .env)", file=sys.stderr)
        return 2
    if args.bad_signature:
        secret = "definitely-the-wrong-secret"

    payload = {
        "action": args.action,
        "repository": {"full_name": args.repo},
        "pull_request": {
            "number": args.pr, "title": f"Manual test of PR #{args.pr}",
            "html_url": f"https://github.com/{args.repo}/pull/{args.pr}",
            "user": {"login": "manual-test"}, "base": {"ref": "main"}, "head": {"ref": "feature"},
        },
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    resp = requests.post(
        args.url.rstrip("/") + "/webhook/github", data=body, timeout=60,
        headers={"Content-Type": "application/json", "X-GitHub-Event": "pull_request",
                 "X-GitHub-Delivery": args.delivery or str(uuid.uuid4()), "X-Hub-Signature-256": signature})
    print(f"HTTP {resp.status_code}\n{resp.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
