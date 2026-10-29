# Demo script (~4 minutes)

**Before you start (5 min earlier):** open `/health?deep=1` so Render is awake and the DB shows `ok`. Have ready:
the dashboard, the GitHub test repo with a branch to PR, the webhook "Recent Deliveries" page, a terminal.

## 1. Problem (20 s)
"Reviewers lose time working out what a PR does and whether it is risky. Our bot reads the real diff and gives a
summary and a risk rating within seconds of the PR being opened."

## 2. Architecture (40 s)
Show the diagram in the README. "GitHub webhook → Flask → HMAC check → PostgreSQL → worker → GitHub API diff →
Gemini → PostgreSQL → dashboard." Mention: raw-bytes HMAC, delivery-id idempotency, durable `pending` row before we answer.

## 3. Live workflow (90 s)
1. Show the empty/current dashboard.
2. Open a PR in the test repo.
3. GitHub → Webhooks → Recent Deliveries: **202 Accepted**.
4. Dashboard (auto-refreshes while jobs run): the PR appears as *Analyzing…* then gets a risk badge.
5. **View details**: summary, key changes, risk reasons (Observed vs Possible), testing suggestions, labels, files with +/−.
6. If comments are enabled: show the bot comment on the PR.

## 4. Security (40 s)
```
python scripts/send_webhook.py --url https://<app> --repo OWNER/REPO --pr 1 --bad-signature
```
→ **401 Invalid or missing signature**, nothing stored. Then redeliver the real webhook in GitHub →
`{"status":"duplicate"}`, still one record. Say: "signature is checked on the raw bytes before anything is processed;
secrets live only in environment variables."

## 5. AI + database (30 s)
"The model must return strict JSON; we validate the risk level, drop invented filenames, and the diff is treated as
untrusted data. If the AI fails we store `failed` with a reason — never a made-up summary. Supabase enforces one row
per PR and refuses a 'success' row without AI output."

## 6. Bonus + honest limits (30 s)
Bonus: PR comment (edits one comment, no duplicates), labels, bot-PR skipping, retries with backoff + jitter,
restart recovery. Limits: partial analysis on huge diffs is flagged, no dashboard login, one analysis per PR, free-tier cold start.

## Likely questions
- *Why 202 and not 200?* GitHub gives 10 s; AI takes longer. We answer only after the job is saved in Postgres.
- *What if the server restarts mid-job?* The row is still `pending`/`processing`; it is re-queued at startup.
- *What if two pushes arrive quickly?* The job saves only if the commit SHA still matches, so an old result can't overwrite a new one.
- *Prompt injection?* Reduced (delimiters, validation, no tools/actions for the model) but not eliminated.
