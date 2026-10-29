"""Configuration for the PR Summarizer Bot.

Every setting comes from environment variables:
  * locally  -> a `.env` file (loaded with python-dotenv)
  * on Render -> the "Environment" tab of the web service

The app refuses to start (ConfigError) if a required secret is missing, and the
error message only names the variable - it never prints values.
"""
import os
from dataclasses import dataclass
from typing import Tuple

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised at startup when required configuration is missing or invalid."""


REQUIRED_VARS = (
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "SUPABASE_DB_URL",
)


def _read_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- required secrets -------------------------------------------------
    github_webhook_secret: str
    github_token: str
    gemini_api_key: str
    gemini_model: str
    database_url: str

    # --- analysis limits (see README "Tuning the limits") -----------------
    max_diff_chars: int = 60_000          # characters of diff sent to the LLM
    max_patch_chars_per_file: int = 12_000  # per-file cap inside that budget
    max_changed_files: int = 100          # changed files listed/analysed
    max_diff_download_bytes: int = 2_000_000  # raw diff read from GitHub

    # --- behaviour flags ---------------------------------------------------
    enable_pr_comments: bool = False
    enable_auto_labels: bool = False
    ignore_bot_prs: bool = True
    process_sync: bool = False            # True = analyse inside the webhook request
    recover_on_start: bool = True         # re-run jobs left pending after a restart

    # --- network behaviour -------------------------------------------------
    http_timeout: int = 15                # seconds, GitHub API
    llm_timeout: int = 60                 # seconds, Gemini API
    max_attempts: int = 3                 # bounded retries for transient errors

    # --- optional dev endpoint protection ----------------------------------
    admin_api_token: str = ""
    allowed_repos: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()  # does not override variables already set (e.g. on Render)
        missing = [name for name in REQUIRED_VARS if not os.getenv(name, "").strip()]
        if missing:
            raise ConfigError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )
        allowed = tuple(
            r.strip() for r in os.getenv("ALLOWED_REPOS", "").split(",") if r.strip()
        )
        return cls(
            github_webhook_secret=os.environ["GITHUB_WEBHOOK_SECRET"].strip(),
            github_token=os.environ["GITHUB_TOKEN"].strip(),
            gemini_api_key=os.environ["GEMINI_API_KEY"].strip(),
            gemini_model=os.environ["GEMINI_MODEL"].strip(),
            database_url=os.environ["SUPABASE_DB_URL"].strip(),
            max_diff_chars=_read_int("MAX_DIFF_CHARS", 60_000, 1_000, 500_000),
            max_patch_chars_per_file=_read_int("MAX_PATCH_CHARS_PER_FILE", 12_000, 500, 200_000),
            max_changed_files=_read_int("MAX_CHANGED_FILES", 100, 1, 3000),
            max_diff_download_bytes=_read_int("MAX_DIFF_DOWNLOAD_BYTES", 2_000_000, 10_000, 20_000_000),
            enable_pr_comments=_read_bool("ENABLE_PR_COMMENTS", False),
            enable_auto_labels=_read_bool("ENABLE_AUTO_LABELS", False),
            ignore_bot_prs=_read_bool("IGNORE_BOT_PRS", True),
            process_sync=_read_bool("PROCESS_SYNC", False),
            recover_on_start=_read_bool("RECOVER_PENDING_ON_START", True),
            http_timeout=_read_int("HTTP_TIMEOUT_SECONDS", 15, 3, 120),
            llm_timeout=_read_int("LLM_TIMEOUT_SECONDS", 60, 10, 300),
            max_attempts=_read_int("MAX_ATTEMPTS", 3, 1, 6),
            admin_api_token=os.getenv("ADMIN_API_TOKEN", "").strip(),
            allowed_repos=allowed,
        )
