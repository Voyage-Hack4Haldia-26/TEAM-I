"""The analysis pipeline: claim job -> fetch diff -> ask the LLM -> save -> (optional) comment/labels."""
import logging
import re

import db
from diffing import select_for_llm, split_sections
from github_client import GitHubError, build_diff_from_files, parse_repo_full_name
from llm import AnalysisError

log = logging.getLogger(__name__)

COMMENT_MARKER = "<!-- pr-summarizer-bot -->"
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9 _.:/-]{0,49}$")


def _result(status, note=None, scope=None, **extra) -> dict:
    base = {
        "status": status, "scope": scope, "note": note, "model": None, "summary": None,
        "risk_level": None, "key_changes": [], "risk_reasons": [], "testing_suggestions": [],
        "suggested_labels": [], "files_changed": [], "files_analyzed": [], "bot_actions": {},
    }
    base.update(extra)
    return base


def is_bot(login: str) -> bool:
    return str(login).lower().endswith("[bot]")


def _public_file(f: dict) -> dict:
    """File info that is safe/small enough to store (no patch text)."""
    return {k: f[k] for k in ("filename", "status", "additions", "deletions", "changes",
                              "previous_filename", "patch_available")}


def _analyze(settings, row, github, analyzer) -> dict:
    if settings.ignore_bot_prs and is_bot(row["pr_author"]):
        return _result("skipped", "Skipped: pull request was opened by a bot (IGNORE_BOT_PRS=true).")

    try:
        owner, repo = parse_repo_full_name(row["repository_full_name"])
        number = row["pr_number"]
        files, files_truncated = github.list_pr_files(owner, repo, number, settings.max_changed_files)
        notes, partial = [], False

        try:
            diff_text, diff_truncated = github.get_diff(owner, repo, number, settings.max_diff_download_bytes)
        except GitHubError as exc:
            if exc.status not in (406, 413, 422):
                raise
            diff_text, diff_truncated = build_diff_from_files(files), False
            notes.append("GitHub refused to return the full diff (too large); "
                         "it was rebuilt from the per-file patches.")
    except GitHubError as exc:
        return _result("failed", f"GitHub error: {exc.message}")

    if not files and not diff_text.strip():
        return _result("skipped", "Skipped: the pull request has no changed files.")

    public_files = [_public_file(f) for f in files]
    if files_truncated:
        partial = True
        notes.append(f"Only the first {len(files)} changed files were analysed (MAX_CHANGED_FILES={settings.max_changed_files}).")
    if diff_truncated:
        partial = True
        notes.append(f"Diff download stopped after {settings.max_diff_download_bytes} bytes; the remainder was not read.")

    selection = select_for_llm(split_sections(diff_text), settings.max_diff_chars, settings.max_patch_chars_per_file)
    if selection.omitted:
        shown = ", ".join(selection.omitted[:5]) + ("..." if len(selection.omitted) > 5 else "")
        notes.append(f"{len(selection.omitted)} file(s) omitted because of the diff size limit "
                     f"(MAX_DIFF_CHARS={settings.max_diff_chars}): {shown}.")
    if selection.truncated_files:
        notes.append(f"{len(selection.truncated_files)} file diff(s) were truncated to fit the limits.")
    partial = partial or selection.partial
    if not selection.text.strip():
        return _result("skipped", "Skipped: no textual diff was available to analyse.", files_changed=public_files)

    coverage = ("PARTIAL - some changes were NOT shown to you: " + " ".join(notes)) if partial else \
               "FULL - the whole diff is included."
    try:
        analysis = analyzer(settings, row, files, selection.text, coverage)
    except AnalysisError as exc:
        return _result("failed", f"AI analysis failed: {exc.message}", files_changed=public_files)

    note = " ".join(notes + ([analysis["analysis_note"]] if analysis["analysis_note"] else []))
    if partial:
        note = ("Partial analysis - not every change was reviewed. " + note).strip()
    return _result(
        "success", note[:1500] or None, scope="partial" if partial else "full",
        model=analysis["model"], summary=analysis["summary"], risk_level=analysis["risk_level"],
        key_changes=analysis["key_changes"], risk_reasons=analysis["risk_reasons"],
        testing_suggestions=analysis["testing_suggestions"], suggested_labels=analysis["suggested_labels"],
        files_changed=public_files, files_analyzed=analysis["files_analyzed"])


# ------------------------------------------------------------ bonus: comment + labels
def _safe_md(text: str) -> str:
    """Stop model/diff-derived text from pinging people (@user) or breaking out of a list."""
    return str(text).replace("@", "@​").replace("\n", " ").strip()


def build_comment(result: dict) -> str:
    def bullets(items):
        return "\n".join(f"- {_safe_md(i)}" for i in items) or "- _none_"

    scope = ("Partial analysis: " + result["note"]) if result["scope"] == "partial" else "Full diff analysed."
    return (
        f"{COMMENT_MARKER}\n"
        f"## PR Summary (AI-generated)\n\n"
        f"{_safe_md(result['summary'])}\n\n"
        f"**Risk level:** `{result['risk_level']}`\n\n"
        f"**Risk reasons**\n{bullets(result['risk_reasons'])}\n\n"
        f"**Suggested tests** (none were executed by this bot)\n{bullets(result['testing_suggestions'])}\n\n"
        f"<sub>{_safe_md(scope)} Model: {result['model']}. "
        f"AI output can be wrong - treat it as a review aid, not a replacement.</sub>\n"
    )


def _post_actions(settings, row, result, github) -> dict:
    actions = {}
    owner, repo = parse_repo_full_name(row["repository_full_name"])
    if settings.enable_pr_comments:
        try:
            actions["comment"] = github.upsert_comment(owner, repo, row["pr_number"], COMMENT_MARKER, build_comment(result))
        except GitHubError as exc:
            actions["comment"] = f"failed: {exc.message}"
    if settings.enable_auto_labels:
        labels = [l for l in result["suggested_labels"] if _LABEL_RE.match(l)][:5]
        actions["labels_suggested"] = result["suggested_labels"]
        if labels:
            try:
                github.add_labels(owner, repo, row["pr_number"], labels)
                actions["labels_applied"] = labels
            except GitHubError as exc:
                actions["labels_applied"] = []
                actions["labels_error"] = exc.message
    return actions


# ------------------------------------------------------------------- entry point
def process_job(settings, summary_id: int, github, analyzer):
    """Run one analysis job. Returns the saved result dict, or None if not claimed/superseded."""
    row = db.claim_job(summary_id)
    if row is None:
        log.info("Job %s not claimed (already running or finished)", summary_id)
        return None
    log.info("Analysing %s#%s", row["repository_full_name"], row["pr_number"])
    try:
        result = _analyze(settings, row, github, analyzer)
        if result["status"] == "success":
            result["bot_actions"] = _post_actions(settings, row, result, github)
    except Exception:  # noqa: BLE001 - never leave a job stuck in 'processing' without a record
        log.exception("Unexpected error while analysing job %s", summary_id)
        result = _result("failed", "Unexpected internal error during analysis.")
    if not db.save_result(summary_id, row["head_sha"], result):
        log.info("Job %s superseded by a newer push; result discarded", summary_id)
        return None
    log.info("Job %s finished: %s", summary_id, result["status"])
    return result
