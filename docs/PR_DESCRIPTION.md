# Team I — PR Summarizer Bot

**Live URL:** https://<REPLACE-WITH-YOUR-RENDER-URL>.onrender.com
**Demo webhook repo:** https://github.com/<OWNER>/<TEST-REPO>

## Overview
A webhook-driven bot that summarises GitHub pull requests with Gemini and rates their risk. When a PR is opened,
updated or reopened, the server verifies the webhook signature, fetches the real diff from the GitHub API, produces a
structured summary/risk assessment, stores it in Supabase PostgreSQL and shows it on a dashboard.

## Features implemented
- [x] `POST /webhook/github` with HMAC-SHA256 verification over the raw body (`hmac.compare_digest`); bad signature → 401
- [x] Real PR diff via the GitHub REST API (authenticated, paginated, rate-limit and error handling, size limits)
- [x] Gemini analysis → `summary`, `key_changes`, `risk_level` (LOW/MEDIUM/HIGH), `risk_reasons`, `testing_suggestions`,
      `suggested_labels`, `files_analyzed`, `analysis_note` — schema-validated; failures stored as `failed`, never faked
- [x] Supabase PostgreSQL persistence, one row per PR, idempotent writes (delivery-id table + unique constraint)
- [x] `GET /health`, `GET /api/summaries` (pagination + filters), `GET /api/summaries/<id>`, `GET /api/stats`,
      protected `POST /api/test-analysis`
- [x] Dashboard: stats from the database, PR list, detail view, loading / empty / error states, backend status, refresh
- [x] Honest partial analysis: truncation is recorded and shown (never silently claimed as full)
- Bonus: [x] PR comment (edits one comment, no duplicates) · [x] auto-labels · [x] bot-PR skip · [x] retries with
  exponential backoff + jitter · [x] duplicate delivery handling · [x] durable job state (pending rows recovered after restart)

## Tech stack
Python 3 · Flask · Gunicorn · requests · psycopg 3 · google-genai (Gemini) · Supabase PostgreSQL · vanilla HTML/CSS/JS ·
Render · pytest

## Database and AI integration
- Tables `pr_summaries` (JSONB for lists, CHECK constraints, indexes, RLS on) and `webhook_deliveries` — see `schema.sql`.
- Gemini is called with a system prompt, JSON response schema and low temperature; the PR diff is treated as untrusted
  data inside random delimiters; output is validated and invented filenames are dropped. Model name comes from `GEMINI_MODEL`
  and is stored with every result.

## Testing performed
- `pytest -q` — 95 automated tests (signatures, ignored events, payload extraction, GitHub errors, malformed LLM output,
  DB failures, list API, duplicate deliveries, oversized diffs). No network or paid services.
- Manual end-to-end on the deployed service: <FILL IN: PR number, delivery response code 202, dashboard screenshot>
- Invalid-signature request returned 401: <FILL IN>
- Redelivery returned `duplicate` with one row: <FILL IN>

## Known limitations
- One analysis per PR (latest push wins); no history table.
- The dashboard has no login — anyone with the URL can read stored summaries.
- Very large diffs are analysed partially (clearly flagged); prompt injection can be reduced but not eliminated.
- Background jobs run in the web process (recovered from PostgreSQL after restarts, but not a dedicated queue).
- Render free tier sleeps when idle.

## Team members
- <Name 1>
- <Name 2>
- <Name 3>
