"""PostgreSQL (Supabase) access layer.

All SQL lives here. Values are always passed as parameters (never string-formatted),
and any database failure is converted into a DBError with a safe message.
"""
import logging
import os
import sys
from contextlib import contextmanager
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

_url: Optional[str] = None


class DBError(Exception):
    """Database unavailable or query failed. The message is safe to show to users."""


def configure(url: str) -> None:
    global _url
    _url = url


@contextmanager
def _conn():
    if not _url:
        raise DBError("Database is not configured")
    try:
        # prepare_threshold=None -> works with Supabase's pooled connections
        with psycopg.connect(_url, connect_timeout=8, row_factory=dict_row,
                             prepare_threshold=None) as conn:
            yield conn  # commits on success, rolls back on error, then closes
    except psycopg.Error as exc:
        first_line = (str(exc).splitlines() or [""])[0][:200]
        log.error("Database error: %s: %s", exc.__class__.__name__, first_line)
        raise DBError("Database unavailable") from exc


# ----------------------------------------------------------------- housekeeping
def init_schema() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(path, encoding="utf-8") as fh:
        sql = fh.read()
    with _conn() as conn:
        conn.execute(sql)


def ping() -> bool:
    with _conn() as conn:
        conn.execute("SELECT 1")
    return True


# ---------------------------------------------------------------------- writing
_UPSERT_PENDING = """
INSERT INTO pr_summaries
    (repository_full_name, pr_number, pr_title, pr_url, pr_author,
     base_branch, head_branch, head_sha, analysis_status, last_delivery_id)
VALUES
    (%(repository_full_name)s, %(pr_number)s, %(pr_title)s, %(pr_url)s, %(pr_author)s,
     %(base_branch)s, %(head_branch)s, %(head_sha)s, 'pending', %(delivery_id)s)
ON CONFLICT (repository_full_name, pr_number) DO UPDATE SET
    pr_title = EXCLUDED.pr_title,
    pr_url = EXCLUDED.pr_url,
    pr_author = EXCLUDED.pr_author,
    base_branch = EXCLUDED.base_branch,
    head_branch = EXCLUDED.head_branch,
    head_sha = EXCLUDED.head_sha,
    analysis_status = 'pending',
    analysis_note = NULL,
    last_delivery_id = EXCLUDED.last_delivery_id,
    updated_at = now()
RETURNING id
"""


def _upsert_pending(cur, pr: dict, delivery_id: Optional[str]) -> int:
    cur.execute(_UPSERT_PENDING, {**pr, "delivery_id": delivery_id})
    return cur.fetchone()["id"]


def register_delivery(delivery_id: str, event: str, pr: dict) -> dict:
    """Atomically record the delivery GUID and queue the PR as 'pending'.

    If this GUID was already seen, nothing is written and duplicate=True is returned.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webhook_deliveries (delivery_id, event, repository_full_name) "
            "VALUES (%s, %s, %s) ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id",
            (delivery_id, event, pr["repository_full_name"]))
        if cur.fetchone() is None:
            cur.execute(
                "SELECT id, analysis_status FROM pr_summaries "
                "WHERE repository_full_name = %s AND pr_number = %s",
                (pr["repository_full_name"], pr["pr_number"]))
            row = cur.fetchone()
            return {"id": row["id"] if row else None, "duplicate": True,
                    "status": row["analysis_status"] if row else None}
        return {"id": _upsert_pending(cur, pr, delivery_id), "duplicate": False, "status": "pending"}


def upsert_pending(pr: dict, delivery_id: Optional[str] = None) -> int:
    with _conn() as conn, conn.cursor() as cur:
        return _upsert_pending(cur, pr, delivery_id)


def requeue(summary_id: int) -> bool:
    """Put a FAILED analysis back to 'pending' (used when GitHub re-delivers the same event)."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE pr_summaries SET analysis_status = 'pending', analysis_note = NULL, updated_at = now() "
            "WHERE id = %s AND analysis_status = 'failed'", (summary_id,))
        return cur.rowcount == 1


def claim_job(summary_id: int) -> Optional[dict]:
    """Atomically move pending -> processing. Returns None if someone else owns the job.

    A job stuck in 'processing' for >10 minutes (crashed worker) can be claimed again.
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE pr_summaries SET analysis_status = 'processing', updated_at = now() "
            "WHERE id = %s AND (analysis_status = 'pending' OR "
            "(analysis_status = 'processing' AND updated_at < now() - interval '10 minutes')) "
            "RETURNING *", (summary_id,))
        return cur.fetchone()


def recoverable_ids() -> list:
    """Jobs left unfinished (e.g. the server restarted mid-analysis)."""
    with _conn() as conn:
        cur = conn.execute(
            "SELECT id FROM pr_summaries WHERE analysis_status = 'pending' OR "
            "(analysis_status = 'processing' AND updated_at < now() - interval '10 minutes') "
            "ORDER BY updated_at LIMIT 50")
        return [r["id"] for r in cur.fetchall()]


_SAVE_RESULT = """
UPDATE pr_summaries SET
    analysis_status = %(status)s,
    analysis_scope = %(scope)s,
    analysis_note = %(note)s,
    model_name = %(model)s,
    summary = %(summary)s,
    key_changes = %(key_changes)s,
    risk_level = %(risk_level)s,
    risk_reasons = %(risk_reasons)s,
    testing_suggestions = %(testing_suggestions)s,
    suggested_labels = %(suggested_labels)s,
    files_changed = %(files_changed)s,
    files_analyzed = %(files_analyzed)s,
    bot_actions = %(bot_actions)s,
    analyzed_at = CASE WHEN %(status)s = 'success' THEN now() ELSE analyzed_at END,
    updated_at = now()
WHERE id = %(id)s AND analysis_status = 'processing'
  AND head_sha IS NOT DISTINCT FROM %(head_sha)s
"""


def save_result(summary_id: int, head_sha: Optional[str], result: dict) -> bool:
    """Store the outcome. Returns False if a newer push superseded this job (nothing written)."""
    params = {
        "id": summary_id,
        "head_sha": head_sha,
        "status": result["status"],
        "scope": result.get("scope"),
        "note": result.get("note"),
        "model": result.get("model"),
        "summary": result.get("summary"),
        "risk_level": result.get("risk_level"),
    }
    for key in ("key_changes", "risk_reasons", "testing_suggestions", "suggested_labels",
                "files_changed", "files_analyzed"):
        params[key] = Jsonb(result.get(key) or [])
    params["bot_actions"] = Jsonb(result.get("bot_actions") or {})
    with _conn() as conn:
        return conn.execute(_SAVE_RESULT, params).rowcount == 1


# ---------------------------------------------------------------------- reading
_LIST_COLUMNS = (
    "id, repository_full_name, pr_number, pr_title, pr_url, pr_author, base_branch, head_branch, "
    "summary, risk_level, suggested_labels, analysis_status, analysis_scope, analysis_note, "
    "model_name, analyzed_at, created_at, updated_at, "
    "COALESCE(jsonb_array_length(files_changed), 0) AS files_count"
)


def list_summaries(limit: int, offset: int, repo: Optional[str] = None,
                   risk: Optional[str] = None, status: Optional[str] = None):
    where, params = [], {"limit": limit, "offset": offset}
    if repo:
        where.append("repository_full_name = %(repo)s")
        params["repo"] = repo
    if risk:
        where.append("risk_level = %(risk)s")
        params["risk"] = risk
    if status:
        where.append("analysis_status = %(status)s")
        params["status"] = status
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _conn() as conn:
        total = conn.execute(f"SELECT count(*) AS n FROM pr_summaries {clause}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM pr_summaries {clause} "
            "ORDER BY updated_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s", params).fetchall()
    return rows, total


def get_summary(summary_id: int) -> Optional[dict]:
    with _conn() as conn:
        return conn.execute("SELECT * FROM pr_summaries WHERE id = %s", (summary_id,)).fetchone()


def stats() -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT "
            "count(*) AS total_prs, "
            "count(*) FILTER (WHERE analysis_status = 'success') AS analyzed, "
            "count(*) FILTER (WHERE analysis_status = 'success' AND risk_level = 'LOW') AS low, "
            "count(*) FILTER (WHERE analysis_status = 'success' AND risk_level = 'MEDIUM') AS medium, "
            "count(*) FILTER (WHERE analysis_status = 'success' AND risk_level = 'HIGH') AS high, "
            "count(*) FILTER (WHERE analysis_status IN ('pending', 'processing')) AS pending, "
            "count(*) FILTER (WHERE analysis_status = 'failed') AS failed, "
            "count(*) FILTER (WHERE analysis_status = 'skipped') AS skipped "
            "FROM pr_summaries").fetchone()
    return dict(row)


if __name__ == "__main__":
    # `python db.py init` -> create the tables using SUPABASE_DB_URL from .env
    from config import ConfigError, Settings

    if len(sys.argv) != 2 or sys.argv[1] != "init":
        sys.exit("Usage: python db.py init")
    try:
        configure(Settings.from_env().database_url)
        init_schema()
    except (ConfigError, DBError) as exc:
        sys.exit(f"Failed: {exc}")
    print("Schema applied successfully.")
