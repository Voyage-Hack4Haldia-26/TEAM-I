"""End-to-end style tests of the Flask app with fake DB / GitHub / LLM."""
import json

from app import create_app
from config import Settings
from conftest import SECRET, make_diff, make_file, post_webhook, pr_payload, sign
from github_client import GitHubError
from llm import AnalysisError
from webhook import PayloadError, extract_pr_event, verify_github_signature
import pytest


# ------------------------------------------------------------------ 1. health
def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_health_deep_reports_database_down(client, fake_db):
    fake_db.fail = True
    body = client.get("/health?deep=1").get_json()
    assert body["status"] == "ok" and body["database"] == "unavailable"


# ------------------------------------------------------- 2. valid signature accepted
def test_valid_signature_is_accepted_and_saved(client, fake_db, analyzer):
    resp = post_webhook(client, pr_payload())
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"
    row = fake_db.rows[1]
    assert row["analysis_status"] == "success" and row["risk_level"] == "MEDIUM"
    assert row["repository_full_name"] == "octo/demo" and row["pr_number"] == 7
    assert row["model_name"] == "test-model" and row["analysis_scope"] == "full"
    assert [f["filename"] for f in row["files_changed"]] == ["app/main.py", "README.md"]
    assert "patch" not in row["files_changed"][0]  # patch text is never stored
    assert len(analyzer.calls) == 1


def test_async_mode_returns_202_after_recording_pending(fake_db, github, analyzer):
    class NeverRuns:  # executor that never runs the job, so we can observe the 'pending' state
        def __init__(self):
            self.queued = []

        def submit(self, fn, *args):
            self.queued.append(args)

    executor = NeverRuns()
    settings = Settings(SECRET, "t", "k", "m", "postgresql://x", recover_on_start=False)  # process_sync=False
    client = create_app(settings, github=github, analyzer=analyzer, executor=executor).test_client()
    resp = post_webhook(client, pr_payload())
    assert resp.status_code == 202 and resp.get_json()["status"] == "accepted"
    assert fake_db.rows[1]["analysis_status"] == "pending"  # durably recorded BEFORE we answered
    assert executor.queued == [(1,)]                        # and handed to the background worker


# ---------------------------------------------- 3. missing / invalid signature rejected
@pytest.mark.parametrize("signature", [None, "", "sha256=deadbeef", "sha1=abc", "garbage"])
def test_bad_or_missing_signature_rejected(client, fake_db, analyzer, signature):
    resp = post_webhook(client, pr_payload(), signature=signature)
    assert resp.status_code == 401
    assert fake_db.rows == {} and analyzer.calls == []  # nothing processed


def test_signature_made_with_wrong_secret_rejected(client, fake_db):
    assert post_webhook(client, pr_payload(), secret="wrong-secret").status_code == 401
    assert fake_db.rows == {}


def test_signature_is_checked_against_raw_bytes_not_parsed_json(client, fake_db):
    """Same JSON, different whitespace -> different bytes -> must NOT validate."""
    original = json.dumps(pr_payload()).encode()
    reformatted = json.dumps(pr_payload(), indent=2).encode()
    resp = post_webhook(client, None, raw=reformatted, signature=sign(original))
    assert resp.status_code == 401


def test_verify_helper_directly():
    body = b'{"a": 1}'
    assert verify_github_signature("s", body, sign(body, "s"))
    assert not verify_github_signature("s", body, sign(body, "other"))
    assert not verify_github_signature("s", body, None)
    assert not verify_github_signature("s", body, "sha256=é")  # non-ASCII must not crash


# ----------------------------------------------------- 4. unsupported events ignored
def test_unsupported_event_ignored(client, fake_db):
    resp = post_webhook(client, {"zen": "x"}, event="issues")
    assert resp.status_code == 200 and resp.get_json()["status"] == "ignored"
    assert fake_db.rows == {}


def test_unsupported_action_ignored(client, fake_db):
    resp = post_webhook(client, pr_payload(action="closed"))
    assert resp.status_code == 200 and resp.get_json()["status"] == "ignored"
    assert fake_db.rows == {}


def test_ping_event_answered(client):
    assert post_webhook(client, {"zen": "hi"}, event="ping").get_json()["message"] == "pong"


def test_signed_but_malformed_json_returns_400(client, fake_db):
    resp = post_webhook(client, None, raw=b"{not json")
    assert resp.status_code == 400 and fake_db.rows == {}


def test_signed_payload_missing_fields_returns_400(client, fake_db):
    resp = post_webhook(client, {"action": "opened", "pull_request": {}, "repository": {}})
    assert resp.status_code == 400


def test_missing_delivery_header_returns_400(client):
    assert post_webhook(client, pr_payload(), delivery="").status_code == 400


# ------------------------------------------------------ 5. payload extraction
def test_extract_pr_event_fields():
    pr = extract_pr_event(pr_payload(number=12, repo="octo/demo", login="bob"))
    assert pr["repository_full_name"] == "octo/demo" and pr["owner"] == "octo" and pr["repo"] == "demo"
    assert pr["pr_number"] == 12 and pr["pr_author"] == "bob"
    assert pr["base_branch"] == "main" and pr["head_branch"] == "feature"
    assert pr["pr_url"] == "https://github.com/octo/demo/pull/12"


@pytest.mark.parametrize("mutate", [
    lambda p: p["repository"].update(full_name="evil/../x"),
    lambda p: p["repository"].update(full_name="no-slash"),
    lambda p: p["pull_request"].update(number="7"),
    lambda p: p["pull_request"].update(number=-1),
    lambda p: p["pull_request"].update(html_url="http://evil.example/x"),
])
def test_extract_pr_event_rejects_bad_payloads(mutate):
    payload = pr_payload()
    mutate(payload)
    with pytest.raises(PayloadError):
        extract_pr_event(payload)


# ---------------------------------------------------------- 6. GitHub API errors
def test_github_404_is_recorded_as_failed(client, fake_db, github, analyzer):
    github.error = GitHubError("Pull request or repository not found.", 404)
    resp = post_webhook(client, pr_payload())
    assert resp.status_code == 502
    row = fake_db.rows[1]
    assert row["analysis_status"] == "failed" and "not found" in row["analysis_note"]
    assert row["summary"] is None and analyzer.calls == []  # no fake summary stored


def test_full_diff_refused_falls_back_to_per_file_patches(client, fake_db, github, analyzer):
    github.diff_error = GitHubError("too large", 406)
    resp = post_webhook(client, pr_payload())
    assert resp.status_code == 200
    assert "rebuilt from the per-file patches" in fake_db.rows[1]["analysis_note"]
    assert "diff --git a/app/main.py" in analyzer.calls[0].diff


# ------------------------------------------------------- 7. invalid LLM output
def test_llm_failure_saves_failed_status_not_fake_summary(client, fake_db, analyzer):
    analyzer.error = AnalysisError("AI returned malformed JSON.")
    resp = post_webhook(client, pr_payload())
    assert resp.status_code == 502
    row = fake_db.rows[1]
    assert row["analysis_status"] == "failed" and row["summary"] is None and row["risk_level"] is None
    assert "malformed JSON" in row["analysis_note"]


def test_unexpected_exception_is_contained(client, fake_db, analyzer):
    analyzer.error = RuntimeError("secret internals")
    resp = post_webhook(client, pr_payload())
    assert resp.status_code == 502
    assert fake_db.rows[1]["analysis_status"] == "failed"
    assert "secret internals" not in json.dumps(resp.get_json())
    assert "secret internals" not in fake_db.rows[1]["analysis_note"]


# ------------------------------------------------------------ 8. database failures
def test_database_down_during_webhook_returns_503(client, fake_db):
    fake_db.fail = True
    resp = post_webhook(client, pr_payload())
    assert resp.status_code == 503
    assert "postgresql" not in resp.get_data(as_text=True).lower()


def test_database_down_on_list_returns_503_without_details(client, fake_db):
    fake_db.fail = True
    resp = client.get("/api/summaries")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "Database unavailable"


# ---------------------------------------------------- 9. /api/summaries returns data
def test_list_and_detail_endpoints(client, fake_db):
    post_webhook(client, pr_payload(number=1), delivery="aaaaaaaa-0000-0000-0000-000000000001")
    post_webhook(client, pr_payload(number=2), delivery="aaaaaaaa-0000-0000-0000-000000000002")
    data = client.get("/api/summaries").get_json()
    assert data["total"] == 2 and data["limit"] == 20
    assert [i["pr_number"] for i in data["items"]] == [2, 1]  # most recently updated first
    assert data["items"][0]["files_count"] == 2
    assert "last_delivery_id" not in data["items"][0]

    detail = client.get(f"/api/summaries/{data['items'][0]['id']}").get_json()
    assert detail["summary"] == "Adds a feature." and detail["risk_reasons"]
    assert "T" in detail["updated_at"]  # ISO-8601 timestamp
    assert client.get("/api/summaries/999").status_code == 404


def test_list_filters_and_pagination(client, fake_db):
    for n in (1, 2, 3):
        post_webhook(client, pr_payload(number=n), delivery=f"bbbbbbbb-0000-0000-0000-00000000000{n}")
    assert client.get("/api/summaries?limit=2").get_json()["total"] == 3
    assert len(client.get("/api/summaries?limit=2").get_json()["items"]) == 2
    assert len(client.get("/api/summaries?limit=2&offset=2").get_json()["items"]) == 1
    assert client.get("/api/summaries?risk=high").get_json()["total"] == 0
    assert client.get("/api/summaries?risk=medium").get_json()["total"] == 3
    assert client.get("/api/summaries?repo=octo/demo").get_json()["total"] == 3


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=abc", "offset=-1", "risk=EXTREME",
                                   "status=weird", "repo=../etc/passwd"])
def test_list_rejects_invalid_params(client, query):
    assert client.get(f"/api/summaries?{query}").status_code == 400


def test_stats_come_from_stored_data(client, fake_db, analyzer):
    assert client.get("/api/stats").get_json()["analyzed"] == 0
    post_webhook(client, pr_payload(number=1), delivery="cccccccc-0000-0000-0000-000000000001")
    stats = client.get("/api/stats").get_json()
    assert stats["analyzed"] == 1 and stats["medium"] == 1 and stats["low"] == 0 and stats["high"] == 0


# ------------------------------------------ 10. duplicates: no duplicate rows/comments
def test_duplicate_delivery_creates_no_second_record_or_comment(make_client, fake_db, github, analyzer):
    client = make_client(enable_pr_comments=True)
    first = post_webhook(client, pr_payload())
    second = post_webhook(client, pr_payload())  # same X-GitHub-Delivery
    assert first.status_code == 200
    assert second.status_code == 200 and second.get_json()["status"] == "duplicate"
    assert len(fake_db.rows) == 1
    assert len(analyzer.calls) == 1 and github.comment_calls == 1


def test_new_push_updates_same_row_and_same_comment(make_client, fake_db, github):
    client = make_client(enable_pr_comments=True)
    post_webhook(client, pr_payload(), delivery="dddddddd-0000-0000-0000-000000000001")
    post_webhook(client, pr_payload(action="synchronize", sha="fff999"), delivery="dddddddd-0000-0000-0000-000000000002")
    assert len(fake_db.rows) == 1                 # one row per PR
    assert len(github.comments) == 1              # comment edited, not duplicated
    assert github.comment_calls == 2
    assert fake_db.rows[1]["bot_actions"]["comment"] == "updated"


def test_redelivery_of_failed_event_is_retried(client, fake_db, analyzer):
    analyzer.error = AnalysisError("AI service is temporarily unavailable.")
    assert post_webhook(client, pr_payload()).status_code == 502
    analyzer.error = None
    resp = post_webhook(client, pr_payload())  # GitHub "Redeliver" button: same delivery id
    assert resp.status_code == 200 and fake_db.rows[1]["analysis_status"] == "success"


def test_comment_text_cannot_ping_users_or_inject_markdown():
    import pipeline
    body = pipeline.build_comment({
        "summary": "Thanks @octocat\n## injected heading", "risk_level": "LOW", "risk_reasons": ["@team look"],
        "testing_suggestions": [], "scope": "full", "note": None, "model": "m"})
    assert "@octocat" not in body and "@team" not in body      # mentions are neutralised
    assert "\n## injected heading" not in body                  # newlines flattened
    assert body.startswith(pipeline.COMMENT_MARKER)             # marker lets us find/edit it later


# --------------------------------------------------------- 11. oversized diffs
def test_oversized_diff_is_partial_and_says_so(make_client, fake_db, github, analyzer):
    names = [f"src/file{i}.py" for i in range(6)]
    github.files = [make_file(n) for n in names]
    github.diff = make_diff(*names, body_chars=600)
    client = make_client(max_diff_chars=1500, max_patch_chars_per_file=700)
    resp = post_webhook(client, pr_payload())
    assert resp.status_code == 200
    row = fake_db.rows[1]
    assert row["analysis_status"] == "success" and row["analysis_scope"] == "partial"
    assert "omitted" in row["analysis_note"] and "MAX_DIFF_CHARS=1500" in row["analysis_note"]
    assert len(analyzer.calls[0].diff) <= 1500 + 200      # budget respected
    assert analyzer.calls[0].coverage.startswith("PARTIAL")  # the model is told too


def test_too_many_files_is_partial(make_client, fake_db, github):
    github.files = [make_file(f"f{i}.py") for i in range(5)]
    client = make_client(max_changed_files=3)
    post_webhook(client, pr_payload())
    row = fake_db.rows[1]
    assert row["analysis_scope"] == "partial" and "first 3 changed files" in row["analysis_note"]
    assert len(row["files_changed"]) == 3


# -------------------------------------------------------------------- extras
def test_bot_pr_is_skipped_without_calling_ai(client, fake_db, analyzer):
    resp = post_webhook(client, pr_payload(login="dependabot[bot]"))
    assert resp.status_code == 200 and fake_db.rows[1]["analysis_status"] == "skipped"
    assert analyzer.calls == []


def test_newer_push_supersedes_running_job(client, fake_db, analyzer):
    def newer_push_arrives(pr):
        fake_db.rows[1]["head_sha"] = "newer-sha"
        fake_db.rows[1]["analysis_status"] = "pending"
    analyzer.on_call = newer_push_arrives
    resp = post_webhook(client, pr_payload())
    assert resp.get_json()["status"] == "skipped"          # old job's result discarded
    assert fake_db.rows[1]["analysis_status"] == "pending"  # waiting for the newer job


def test_labels_applied_only_when_enabled(make_client, fake_db, github):
    client = make_client(enable_auto_labels=True)
    post_webhook(client, pr_payload())
    assert github.labels_added == [["feature"]]
    assert fake_db.rows[1]["bot_actions"]["labels_applied"] == ["feature"]


# ----------------------------------------------------------- dashboard + dev endpoint
def test_dashboard_files_served_but_source_is_not(client):
    assert client.get("/").status_code == 200
    assert b"PR Summarizer Bot" in client.get("/").data
    assert client.get("/script.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    for path in ("/app.py", "/.env", "/config.py", "/schema.sql"):
        assert client.get(path).status_code == 404
    assert "default-src 'self'" in client.get("/").headers["Content-Security-Policy"]


def test_test_analysis_disabled_by_default(client):
    assert client.post("/api/test-analysis", json={}).status_code == 404


def test_test_analysis_requires_token_and_allowlist(make_client, fake_db):
    client = make_client(admin_api_token="adm", allowed_repos=("octo/demo",))
    body = {"repository": "octo/demo", "pr_number": 7}
    assert client.post("/api/test-analysis", json=body).status_code == 401
    assert client.post("/api/test-analysis", json=body, headers={"X-Admin-Token": "nope"}).status_code == 401
    other = {"repository": "evil/repo", "pr_number": 1}
    assert client.post("/api/test-analysis", json=other, headers={"X-Admin-Token": "adm"}).status_code == 403
    ok = client.post("/api/test-analysis", json=body, headers={"X-Admin-Token": "adm"})
    assert ok.status_code == 200 and ok.get_json()["status"] == "success"
    assert client.post("/api/test-analysis", json={"repository": "octo/demo", "pr_number": "x"},
                       headers={"X-Admin-Token": "adm"}).status_code == 400
