"""Shared test fixtures. No test touches the network, Supabase, GitHub or Gemini."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

# Dummy values so importing `app` works. Hard assignment (not setdefault) so a real
# .env or shell variable can never make the tests talk to real services.
for _name, _value in {
    "GITHUB_WEBHOOK_SECRET": "test-secret",
    "GITHUB_TOKEN": "test-token",
    "GEMINI_API_KEY": "test-key",
    "GEMINI_MODEL": "test-model",
    "SUPABASE_DB_URL": "postgresql://user:pass@localhost:5432/test",
    "RECOVER_PENDING_ON_START": "false",
}.items():
    os.environ[_name] = _value

import pytest  # noqa: E402

import db  # noqa: E402
from app import create_app  # noqa: E402
from config import Settings  # noqa: E402
from github_client import GitHubError  # noqa: E402

SECRET = "test-secret"


# ------------------------------------------------------------------ fake database
class FakeDB:
    """Test double with the same functions as db.py, backed by a dict."""

    def __init__(self):
        self.rows, self.deliveries, self.next_id, self.fail = {}, set(), 1, False

    def _check(self):
        if self.fail:
            raise db.DBError("Database unavailable")

    def _find(self, key):
        return next((r for r in self.rows.values()
                     if (r["repository_full_name"], r["pr_number"]) == key), None)

    def _now(self):
        return datetime.now(timezone.utc)

    def ping(self):
        self._check()
        return True

    def upsert_pending(self, pr, delivery_id=None):
        self._check()
        row = self._find((pr["repository_full_name"], pr["pr_number"]))
        if row is None:
            row = {"id": self.next_id, "created_at": self._now(), "summary": None, "risk_level": None,
                   "key_changes": [], "risk_reasons": [], "testing_suggestions": [], "suggested_labels": [],
                   "files_changed": [], "files_analyzed": [], "analysis_scope": None, "model_name": None,
                   "bot_actions": {}, "analyzed_at": None}
            self.rows[self.next_id] = row
            self.next_id += 1
        for key in ("repository_full_name", "pr_number", "pr_title", "pr_url", "pr_author",
                    "base_branch", "head_branch", "head_sha"):
            row[key] = pr[key]
        row.update(analysis_status="pending", analysis_note=None, updated_at=self._now())
        return row["id"]

    def register_delivery(self, delivery_id, event, pr):
        self._check()
        if delivery_id in self.deliveries:
            row = self._find((pr["repository_full_name"], pr["pr_number"]))
            return {"id": row["id"] if row else None, "duplicate": True,
                    "status": row["analysis_status"] if row else None}
        self.deliveries.add(delivery_id)
        return {"id": self.upsert_pending(pr, delivery_id), "duplicate": False, "status": "pending"}

    def requeue(self, summary_id):
        self._check()
        row = self.rows.get(summary_id)
        if row and row["analysis_status"] == "failed":
            row["analysis_status"] = "pending"
            return True
        return False

    def claim_job(self, summary_id):
        self._check()
        row = self.rows.get(summary_id)
        if row and row["analysis_status"] == "pending":
            row["analysis_status"] = "processing"
            return dict(row)
        return None

    def save_result(self, summary_id, head_sha, result):
        self._check()
        row = self.rows.get(summary_id)
        if not row or row["analysis_status"] != "processing" or row["head_sha"] != head_sha:
            return False
        row.update(
            analysis_status=result["status"], analysis_scope=result.get("scope"),
            analysis_note=result.get("note"), model_name=result.get("model"),
            summary=result.get("summary"), risk_level=result.get("risk_level"),
            key_changes=result.get("key_changes") or [], risk_reasons=result.get("risk_reasons") or [],
            testing_suggestions=result.get("testing_suggestions") or [],
            suggested_labels=result.get("suggested_labels") or [],
            files_changed=result.get("files_changed") or [], files_analyzed=result.get("files_analyzed") or [],
            bot_actions=result.get("bot_actions") or {}, updated_at=self._now())
        if result["status"] == "success":
            row["analyzed_at"] = self._now()
        return True

    def recoverable_ids(self):
        self._check()
        return [r["id"] for r in self.rows.values() if r["analysis_status"] == "pending"]

    def list_summaries(self, limit, offset, repo=None, risk=None, status=None):
        self._check()
        rows = [r for r in self.rows.values()
                if (not repo or r["repository_full_name"] == repo)
                and (not risk or r["risk_level"] == risk)
                and (not status or r["analysis_status"] == status)]
        rows.sort(key=lambda r: (r["updated_at"], r["id"]), reverse=True)
        page = [{**r, "files_count": len(r["files_changed"])} for r in rows[offset:offset + limit]]
        return page, len(rows)

    def get_summary(self, summary_id):
        self._check()
        row = self.rows.get(summary_id)
        return dict(row) if row else None

    def stats(self):
        self._check()
        rows = list(self.rows.values())
        ok = [r for r in rows if r["analysis_status"] == "success"]
        count = lambda lvl: sum(1 for r in ok if r["risk_level"] == lvl)  # noqa: E731
        return {"total_prs": len(rows), "analyzed": len(ok), "low": count("LOW"), "medium": count("MEDIUM"),
                "high": count("HIGH"), "pending": sum(1 for r in rows if r["analysis_status"] in ("pending", "processing")),
                "failed": sum(1 for r in rows if r["analysis_status"] == "failed"),
                "skipped": sum(1 for r in rows if r["analysis_status"] == "skipped")}


# ----------------------------------------------------------- fake GitHub / analyzer
def make_file(name, additions=3, deletions=1, status="modified", patch="@@ -1 +1 @@\n-old\n+new"):
    return {"filename": name, "status": status, "additions": additions, "deletions": deletions,
            "changes": additions + deletions, "previous_filename": None,
            "patch_available": bool(patch), "patch": patch}


def make_diff(*names, body_chars=60):
    return "".join(
        f"diff --git a/{n} b/{n}\n--- a/{n}\n+++ b/{n}\n@@ -1 +1 @@\n-old\n+{'x' * body_chars}\n" for n in names)


class FakeGitHub:
    def __init__(self):
        self.files = [make_file("app/main.py"), make_file("README.md")]
        self.diff = make_diff("app/main.py", "README.md")
        self.error = None        # raised by list_pr_files
        self.diff_error = None   # raised by get_diff
        self.comments, self.comment_calls, self.labels_added = {}, 0, []

    def list_pr_files(self, owner, repo, number, max_files):
        if self.error:
            raise self.error
        return self.files[:max_files], len(self.files) > max_files

    def get_diff(self, owner, repo, number, max_bytes):
        if self.diff_error:
            raise self.diff_error
        return self.diff, False

    def get_pr(self, owner, repo, number):
        return pr_payload(number=number, repo=f"{owner}/{repo}")["pull_request"]

    def upsert_comment(self, owner, repo, number, marker, body):
        key = (owner, repo, number)
        created = key not in self.comments
        self.comments[key] = body
        self.comment_calls += 1
        return "created" if created else "updated"

    def add_labels(self, owner, repo, number, labels):
        self.labels_added.append(list(labels))


class FakeAnalyzer:
    def __init__(self):
        self.calls, self.error, self.on_call = [], None, None

    def __call__(self, settings, pr, files, diff_text, coverage):
        self.calls.append(SimpleNamespace(pr=pr, files=files, diff=diff_text, coverage=coverage))
        if self.on_call:
            self.on_call(pr)
        if self.error:
            raise self.error
        return {"summary": "Adds a feature.", "key_changes": ["Added main logic"], "risk_level": "MEDIUM",
                "risk_reasons": ["Observed: app/main.py changes core logic"],
                "testing_suggestions": ["Run the unit tests"], "suggested_labels": ["feature"],
                "files_analyzed": [f["filename"] for f in files], "analysis_note": "", "model": "test-model"}


# ----------------------------------------------------------------------- helpers
def pr_payload(action="opened", number=7, repo="octo/demo", login="alice", sha="abc123def456"):
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number, "title": "Add feature", "html_url": f"https://github.com/{repo}/pull/{number}",
            "user": {"login": login}, "base": {"ref": "main"}, "head": {"ref": "feature", "sha": sha},
        },
    }


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(client, payload, event="pull_request", delivery="11111111-aaaa-bbbb-cccc-000000000001",
                 secret=SECRET, signature="auto", raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}
    if signature == "auto":
        headers["X-Hub-Signature-256"] = sign(body, secret)
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhook/github", data=body, headers=headers)


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDB()
    for name in ("ping", "upsert_pending", "register_delivery", "requeue", "claim_job", "save_result",
                 "recoverable_ids", "list_summaries", "get_summary", "stats"):
        monkeypatch.setattr(db, name, getattr(fake, name))
    return fake


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import retry
    monkeypatch.setattr(retry, "_sleep", lambda s: None)


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def analyzer():
    return FakeAnalyzer()


@pytest.fixture
def make_client(fake_db, github, analyzer):
    """Factory: make_client(enable_pr_comments=True, ...) -> Flask test client (sync processing)."""
    def factory(**overrides):
        values = dict(
            github_webhook_secret=SECRET, github_token="t", gemini_api_key="k", gemini_model="test-model",
            database_url="postgresql://x", process_sync=True, recover_on_start=False)
        values.update(overrides)
        return create_app(Settings(**values), github=github, analyzer=analyzer).test_client()
    return factory


@pytest.fixture
def client(make_client):
    return make_client()


__all__ = ["GitHubError"]
