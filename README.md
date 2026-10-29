# PR Summarizer Bot — Team I (Hack4Haldia'26)

AI-powered GitHub Pull Request summaries and risk analysis.

A GitHub webhook fires when a PR is opened/updated → the server **verifies the HMAC-SHA256 signature** →
records the event in **Supabase PostgreSQL** → fetches the **real diff** from the GitHub API →
asks **Gemini** for a structured summary + risk assessment → saves it → shows it on a dashboard.

```
GitHub ──webhook──▶ POST /webhook/github
                      │ 1. verify HMAC on raw bytes (401 if bad)
                      │ 2. insert delivery id + 'pending' row  (one transaction, idempotent)
                      │ 3. reply 202 Accepted
                      ▼
               background worker (claims the row: pending → processing)
                      │ 4. GitHub REST: changed files (paginated) + diff
                      │ 5. Gemini: JSON summary / risk / tests / labels  (validated)
                      │ 6. save result: success | skipped | failed
                      │ 7. optional: PR comment + labels
                      ▼
Browser ◀── GET /api/summaries, /api/stats ◀── PostgreSQL
```

## Folder structure

```
TEAM-I/
├── app.py                  Flask app: routes, webhook handler, error handling
├── config.py               Environment variables → Settings (fails fast if a secret is missing)
├── webhook.py              HMAC verification + pull_request payload extraction
├── github_client.py        GitHub REST client (pagination, rate limits, retries, comments, labels)
├── diffing.py              Splits the diff per file and fits it into the LLM budget honestly
├── llm.py                  Gemini prompt, structured output, JSON validation
├── pipeline.py             The job: fetch → analyse → save → comment/labels
├── db.py                   All SQL (psycopg) + `python db.py init`
├── retry.py                Bounded retries with exponential backoff and jitter
├── schema.sql              Database schema (run once in Supabase)
├── index.html              Dashboard markup
├── style.css               Dashboard styles
├── script.js               Dashboard logic (talks to the Flask API)
├── requirements.txt        Runtime dependencies
├── requirements-dev.txt    + pytest
├── pytest.ini
├── render.yaml             Optional Render blueprint
├── .env.example            Placeholders only
├── .gitignore
├── scripts/
│   └── send_webhook.py     Send a signed (or deliberately bad) webhook for demos
├── tests/
│   ├── conftest.py         Fakes for DB / GitHub / Gemini (no network, no cost)
│   ├── test_webhook_and_api.py
│   └── test_units.py
└── docs/
    ├── PR_DESCRIPTION.md   Submission template
    └── DEMO_SCRIPT.md      3-minute demo script
```

> `package.json` from the starter is not needed (Flask is the only backend). Remove it with `git rm package.json`.

## 1. Run locally (Windows PowerShell)

```powershell
git checkout -b team-i-solution          # or: git switch team-i-solution
python -m venv .venv                     # skip if .venv already exists
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env                   # then fill in the values (sections 2-4)
python db.py init                        # creates the tables in Supabase
python app.py                            # http://127.0.0.1:5000
```

macOS/Linux: `source .venv/bin/activate` and `cp .env.example .env`.

## 2. Supabase (PostgreSQL)

1. Create a project at supabase.com.
2. **Project Settings → Database → Connection string → _Session pooler_** (not "Direct connection": that one is
   IPv6-only and Render's free tier cannot reach it). Copy it and put your DB password in:
   `SUPABASE_DB_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`
3. Create the tables: `python db.py init`, **or** paste `schema.sql` into **SQL Editor → Run**.

Design: one row per PR (`UNIQUE (repository_full_name, pr_number)`); a new push updates that row.
`webhook_deliveries(delivery_id PRIMARY KEY)` makes duplicate deliveries impossible to process twice.
Row-level security is enabled so Supabase's public REST API cannot read your data; the app connects directly.
The database refuses a `success` row without a summary and risk level.

## 3. GitHub token and webhook

**Token** (`GITHUB_TOKEN`): GitHub → Settings → Developer settings → Fine-grained tokens → select the test
repositories → permissions: **Pull requests: Read**. For the bonus features also **Issues: Read and write**
(PR comments and labels go through the Issues API).

**Webhook** (repo → Settings → Webhooks → Add webhook):

| Field | Value |
|---|---|
| Payload URL | `https://<your-app>.onrender.com/webhook/github` |
| Content type | `application/json` |
| Secret | the same value as `GITHUB_WEBHOOK_SECRET` |
| Events | "Let me select" → **Pull requests** only |

Generate a secret: `python -c "import secrets; print(secrets.token_hex(32))"`.
Test deliveries: webhook page → **Recent Deliveries** → click one → see the response code → **Redeliver**.

To receive real webhooks on your laptop you need a tunnel (ngrok / cloudflared) — or just test with
`scripts/send_webhook.py` (section 7) and deploy for the real thing.

## 4. Gemini model

`GEMINI_API_KEY` comes from https://aistudio.google.com/apikey. Model names change, so the model is **not
hard-coded**: choose a currently available one from Google's model list and set `GEMINI_MODEL`. If it is wrong the
analysis is stored as `failed` with the note "Configured Gemini model was not found. Check GEMINI_MODEL."

## 5. API

| Method & path | Purpose |
|---|---|
| `GET /health` | `{"status":"ok"}`. `?deep=1` also reports `database: ok/unavailable` |
| `POST /webhook/github` | Verified webhook. `401` bad signature · `400` bad body · `200` ignored/duplicate · **`202` accepted & recorded** · `503` DB down |
| `GET /api/summaries` | List, newest first. `limit` (1-100), `offset`, `repo`, `risk`, `status` |
| `GET /api/summaries/<id>` | Full saved analysis |
| `GET /api/stats` | Counts used by the dashboard cards |
| `POST /api/test-analysis` | Dev only. Header `X-Admin-Token`, body `{"repository":"o/r","pr_number":1}`. Disabled unless `ADMIN_API_TOKEN` **and** `ALLOWED_REPOS` are set; only allow-listed repos |

**Response semantics.** GitHub waits ~10 s for a reply, and diff + AI can take longer, so the webhook answers
`202` *after the job is durably stored as `pending` in PostgreSQL*. The result appears as `success`, `skipped`
(bot PR / empty diff) or `failed` (with a reason) — never a fake summary. If the server restarts mid-job the row is
still in the database and is picked up again at startup. (Set `PROCESS_SYNC=true` to analyse inside the request
instead: `200` on success, `502` on failure.)

Statuses: `pending` → `processing` → `success` | `skipped` | `failed`. Scope: `full` or `partial`.

## 6. Configuration and limits

| Variable | Default | Meaning |
|---|---|---|
| `MAX_DIFF_CHARS` | 60000 | Max diff characters sent to the LLM |
| `MAX_PATCH_CHARS_PER_FILE` | 12000 | Per-file cap inside that budget (lock files get ~1500) |
| `MAX_CHANGED_FILES` | 100 | Max changed files listed/analysed |
| `MAX_DIFF_DOWNLOAD_BYTES` | 2000000 | Max raw diff bytes read from GitHub |
| `ENABLE_PR_COMMENTS` | false | Post/update one summary comment on the PR |
| `ENABLE_AUTO_LABELS` | false | Apply suggested labels (suggestions are always stored) |
| `IGNORE_BOT_PRS` | true | Skip authors ending in `[bot]` (status `skipped`) |
| `PROCESS_SYNC` | false | Analyse inside the webhook request |
| `MAX_ATTEMPTS` | 3 | Total tries for transient GitHub/Gemini errors |
| `HTTP_TIMEOUT_SECONDS` / `LLM_TIMEOUT_SECONDS` | 15 / 60 | Request timeouts |

**Nothing is silently truncated.** If files or characters are cut, the row gets `analysis_scope = partial`,
`analysis_note` lists what was left out, the model is told the coverage is partial, and the dashboard shows a
"Partial" badge. Lock files and generated files are deprioritised so real code gets the budget.

## 7. Tests

```powershell
pytest -q                       # 95 tests, no network, no API cost
```

They cover: health, valid/missing/invalid/tampered signatures, ignored events, payload extraction, GitHub errors
(401/403/404/422/429/5xx, pagination, retries), invalid LLM JSON, database failures, list/detail API, duplicate
deliveries (no duplicate rows or comments), and oversized diffs.

### One real end-to-end test

1. Create a throw-away GitHub repo with a branch containing a small change (e.g. edit a Python file).
2. Deploy (section 8) and add the webhook (section 3).
3. Open a PR → webhook page → Recent Deliveries should show **202** → within ~10-30 s the dashboard shows the PR.
4. Prove security: `python scripts/send_webhook.py --url https://<app> --repo OWNER/REPO --pr 1 --bad-signature` → **401**.
5. Prove idempotency: Redeliver the same delivery in GitHub → `{"status":"duplicate"}`, still one row.
6. Push another commit to the PR → same row is updated (and the same comment edited if comments are enabled).

Local variant (no tunnel): `python scripts/send_webhook.py --url http://127.0.0.1:5000 --repo OWNER/REPO --pr 1`.

## 8. Deploy on Render

1. Push the branch to GitHub.
2. Render → **New → Web Service** → connect the repo/branch.
3. Runtime **Python 3**, Build command `pip install -r requirements.txt`,
   Start command `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`
   (one worker: background jobs run in that process's thread pool). Health check path `/health`.
4. **Environment** tab: add `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL`,
   `SUPABASE_DB_URL` (and optionally `ENABLE_PR_COMMENTS`, limits). Do not upload `.env`.
5. Deploy, open `https://<app>.onrender.com/health` → `{"status":"ok"}`, and `/health?deep=1` → `database: ok`.
6. Add the webhook with that URL (section 3). `render.yaml` is an optional Blueprint of the same settings.

Free-tier services sleep when idle; the first request after a nap is slow. Open `/health` a few minutes before
the demo. GitHub shows a timeout for a cold start — use **Redeliver** (the event was not recorded, so it is processed).

## 9. Security notes (and honest limits)

- HMAC-SHA256 over the **raw request bytes**, `hmac.compare_digest`, checked before anything else is processed.
- Secrets only in environment variables; never logged, returned, or sent to the browser. Missing secrets stop the app at startup.
- GitHub URLs are built only from validated `owner/repo/number`; users cannot supply URLs. HTTP timeouts everywhere; bounded retries.
- The diff is untrusted: it sits between random per-request markers, the prompt forbids following instructions inside it,
  the output is schema-validated and file names the model invents are dropped. **Prompt injection cannot be fully
  eliminated** — treat AI output as a review aid.
- Frontend never uses `innerHTML`; CSP forbids inline scripts. Posted comments neutralise `@mentions`.
- Not covered: no rate limiting on public read endpoints, no user login for the dashboard (it shows PR titles/summaries
  to anyone with the URL — don't point it at private repos you don't want exposed), one-row-per-PR means no history.
- No analysis is claimed to be "tested": suggested tests are suggestions; the bot never runs code.

**If a secret was ever committed or pasted somewhere public:** revoke it immediately — GitHub token (Settings →
Developer settings → delete), Gemini key (AI Studio → delete key), webhook secret (change it in the webhook *and* in
Render), Supabase DB password (Project Settings → Database → Reset password) — then create new ones. Removing the file
from a later commit does not remove it from git history.

## 10. Submit

```powershell
git add -A
git commit -m "PR Summarizer Bot: webhook, diff fetch, Gemini analysis, Supabase, dashboard"
git push -u origin team-i-solution
# then open a PR to the original repository's main branch (GitHub UI, or: gh pr create --base main)
```

Paste `docs/PR_DESCRIPTION.md` into the PR (live URL first), and submit through the official **MLH OrganizerHQ**
platform as the organisers require.
