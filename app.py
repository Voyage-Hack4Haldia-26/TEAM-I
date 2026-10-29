"""PR Summarizer Bot - Flask application.

Flow: GitHub webhook -> verify HMAC -> record delivery + queue job in PostgreSQL ->
worker fetches the diff -> Gemini summarises -> result saved -> dashboard shows it.
"""
import hmac
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import db
import pipeline
from config import ConfigError, Settings
from github_client import GitHubClient, GitHubError, parse_repo_full_name
from llm import analyze_pull_request
from webhook import SUPPORTED_ACTIONS, PayloadError, extract_pr_event, verify_github_signature

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RISKS = {"LOW", "MEDIUM", "HIGH"}
STATUSES = {"pending", "processing", "success", "skipped", "failed"}
_DELIVERY_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def _error(message: str, status: int, code: str = "error"):
    return jsonify({"error": message, "code": code}), status


def _serialize(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if key == "last_delivery_id":
            continue  # internal
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def create_app(settings=None, github=None, analyzer=None, executor=None) -> Flask:
    """Application factory. Tests inject fakes via github / analyzer / executor."""
    settings = settings or Settings.from_env()  # raises ConfigError if a secret is missing
    db.configure(settings.database_url)
    github = github or GitHubClient(settings.github_token, settings.http_timeout, settings.max_attempts)
    analyzer = analyzer or analyze_pull_request
    executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # webhook bodies are small; reject huge ones

    def run_job(summary_id: int):
        try:
            return pipeline.process_job(settings, summary_id, github, analyzer)
        except Exception:  # noqa: BLE001 - background thread must never die silently
            log.exception("Job %s crashed", summary_id)

    def recover_pending():
        try:
            ids = db.recoverable_ids()
        except db.DBError:
            log.warning("Could not check for unfinished jobs at startup")
            return
        for summary_id in ids:
            executor.submit(run_job, summary_id)
        if ids:
            log.info("Re-queued %d unfinished job(s) after startup", len(ids))

    if settings.recover_on_start:
        threading.Thread(target=recover_pending, daemon=True).start()

    # ------------------------------------------------------------ security headers
    @app.after_request
    def add_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if request.path.startswith("/api") or request.path == "/health":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------- errors
    @app.errorhandler(HTTPException)
    def handle_http(exc):
        return _error(exc.description if exc.code != 500 else "Internal server error", exc.code, "http_error")

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        log.exception("Unhandled error")  # full detail stays in server logs only
        return _error("Internal server error", 500, "internal_error")

    # ------------------------------------------------------------- static dashboard
    @app.get("/")
    def index():
        return send_from_directory(BASE_DIR, "index.html")

    @app.get("/script.js")
    def script():
        return send_from_directory(BASE_DIR, "script.js")

    @app.get("/style.css")
    def style():
        return send_from_directory(BASE_DIR, "style.css")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    # ------------------------------------------------------------------- health
    @app.get("/health")
    def health():
        body = {"status": "ok"}
        if request.args.get("deep") == "1":
            try:
                db.ping()
                body["database"] = "ok"
            except db.DBError:
                body["database"] = "unavailable"
        return jsonify(body), 200

    # ------------------------------------------------------------------ webhook
    @app.post("/webhook/github")
    def github_webhook():
        raw = request.get_data()  # exact bytes GitHub sent - read BEFORE any JSON parsing
        if not verify_github_signature(settings.github_webhook_secret, raw,
                                       request.headers.get("X-Hub-Signature-256")):
            log.warning("Rejected webhook: missing or invalid signature")
            return _error("Invalid or missing signature", 401, "bad_signature")

        # ---- from here on the request is authenticated ----
        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return jsonify({"status": "ok", "message": "pong"}), 200
        if event != "pull_request":
            return jsonify({"status": "ignored", "reason": "unsupported event"}), 200

        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return _error("Request body is not valid JSON", 400, "bad_json")
        if not isinstance(payload, dict) or payload.get("action") not in SUPPORTED_ACTIONS:
            return jsonify({"status": "ignored", "reason": "unsupported action"}), 200
        try:
            pr = extract_pr_event(payload)
        except PayloadError as exc:
            return _error(str(exc), 400, "bad_payload")

        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        if not _DELIVERY_RE.match(delivery_id):
            return _error("Missing or invalid X-GitHub-Delivery header", 400, "bad_delivery_id")

        try:
            reg = db.register_delivery(delivery_id, event, pr)
        except db.DBError:
            # nothing was recorded, so a re-delivery from GitHub will be processed normally
            return _error("Database unavailable; please re-deliver this webhook", 503, "db_unavailable")

        if reg["duplicate"]:
            # A re-delivery of an event we already handled. Only retry it if it had FAILED.
            if reg["status"] == "failed" and reg["id"]:
                try:
                    db.requeue(reg["id"])
                except db.DBError:
                    return _error("Database unavailable", 503, "db_unavailable")
            else:
                log.info("Duplicate delivery %s ignored", delivery_id)
                return jsonify({"status": "duplicate", "id": reg["id"]}), 200

        log.info("Accepted %s PR #%s (delivery %s)", pr["repository_full_name"], pr["pr_number"], delivery_id)
        if settings.process_sync:
            result = run_job(reg["id"])
            if result is None:
                return jsonify({"status": "skipped", "id": reg["id"],
                                "message": "Job was already running or superseded"}), 200
            code = 502 if result["status"] == "failed" else 200
            return jsonify({"status": result["status"], "id": reg["id"], "note": result["note"]}), code

        executor.submit(run_job, reg["id"])
        # 202 = durably recorded as 'pending' in PostgreSQL; the analysis finishes in the background.
        return jsonify({"status": "accepted", "id": reg["id"],
                        "message": "Queued. Poll /api/summaries/<id> for the result."}), 202

    # --------------------------------------------------------------------- API
    @app.get("/api/summaries")
    def list_summaries():
        try:
            limit = int(request.args.get("limit", 20))
            offset = int(request.args.get("offset", 0))
        except ValueError:
            return _error("limit and offset must be integers", 400, "bad_params")
        if not 1 <= limit <= 100 or offset < 0:
            return _error("limit must be 1-100 and offset must be >= 0", 400, "bad_params")
        repo = request.args.get("repo") or None
        risk = (request.args.get("risk") or "").upper() or None
        status = (request.args.get("status") or "").lower() or None
        if repo:
            try:
                parse_repo_full_name(repo)
            except GitHubError:
                return _error("repo must look like 'owner/name'", 400, "bad_params")
        if risk and risk not in RISKS:
            return _error("risk must be LOW, MEDIUM or HIGH", 400, "bad_params")
        if status and status not in STATUSES:
            return _error("status must be one of: " + ", ".join(sorted(STATUSES)), 400, "bad_params")
        try:
            rows, total = db.list_summaries(limit, offset, repo, risk, status)
        except db.DBError:
            return _error("Database unavailable", 503, "db_unavailable")
        return jsonify({"items": [_serialize(r) for r in rows], "total": total,
                        "limit": limit, "offset": offset})

    @app.get("/api/summaries/<int:summary_id>")
    def get_summary(summary_id):
        try:
            row = db.get_summary(summary_id)
        except db.DBError:
            return _error("Database unavailable", 503, "db_unavailable")
        if row is None:
            return _error("Summary not found", 404, "not_found")
        return jsonify(_serialize(row))

    @app.get("/api/stats")
    def get_stats():
        try:
            return jsonify(db.stats())
        except db.DBError:
            return _error("Database unavailable", 503, "db_unavailable")

    @app.post("/api/test-analysis")
    def test_analysis():
        """Dev-only: analyse one real PR on demand. Needs ADMIN_API_TOKEN + ALLOWED_REPOS."""
        if not settings.admin_api_token or not settings.allowed_repos:
            return _error("Endpoint disabled (set ADMIN_API_TOKEN and ALLOWED_REPOS to enable)", 404, "disabled")
        supplied = request.headers.get("X-Admin-Token", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), settings.admin_api_token.encode("utf-8")):
            return _error("Unauthorized", 401, "unauthorized")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _error("JSON body required", 400, "bad_params")
        number = body.get("pr_number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            return _error("pr_number must be a positive integer", 400, "bad_params")
        try:
            owner, name = parse_repo_full_name(body.get("repository"))
        except GitHubError as exc:
            return _error(exc.message, 400, "bad_params")
        full_name = f"{owner}/{name}"
        if full_name not in settings.allowed_repos:
            return _error("Repository is not in ALLOWED_REPOS", 403, "forbidden")
        try:
            pr_json = github.get_pr(owner, name, number)
            pr = extract_pr_event({"pull_request": pr_json, "repository": {"full_name": full_name}})
            summary_id = db.upsert_pending(pr)
        except GitHubError as exc:
            return _error(exc.message, 502, "github_error")
        except PayloadError as exc:
            return _error(str(exc), 502, "github_error")
        except db.DBError:
            return _error("Database unavailable", 503, "db_unavailable")
        result = run_job(summary_id)
        if result is None:
            return _error("Analysis could not be started (already running?)", 409, "conflict")
        return jsonify({"id": summary_id, "status": result["status"], "note": result["note"]}), \
            (502 if result["status"] == "failed" else 200)

    return app


try:
    app = create_app()  # `gunicorn app:app` imports this
except ConfigError as exc:
    raise SystemExit(f"Configuration error: {exc}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")),
            debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
