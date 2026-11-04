# LinkedIn Job Bot (CDP + Playwright)

Simple Python app for LinkedIn job discovery with a persistent Chrome profile, SQLite storage, and a delay between completed search cycles.

## Requirements

- Python 3.12+
- Google Chrome installed
- Windows or Linux

## Setup

1. Create and activate a virtual environment.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies.

```powershell
pip install -r requirements.txt
```

3. Install Playwright browser tooling.

```powershell
python -m playwright install chromium
```

4. Create `.env` from the example.

```powershell
Copy-Item .env.example .env
```

5. Optional: set `CHROME_EXECUTABLE_PATH` in `.env` if Chrome is not auto-detected.

## CDP and Chrome Configuration

Environment variables:

- `AUTOMATION_ENABLED` kill switch (`true`/`false`)
- `CHROME_EXECUTABLE_PATH` optional explicit path to Chrome
- `CHROME_DEBUG_PORT` default `9222`
- `CHROME_CDP_URL` must be localhost URL (default `http://127.0.0.1:9222`)
- `CHROME_USER_DATA_DIR` default `./data/chrome-profile`
- `KEEP_CHROME_OPEN` keep Chrome running on exit (`true`/`false`)
- `SQLITE_DB_PATH` default `./data/jobs.db`
- `SEARCH_INTERVAL_MINUTES` default `5` (wait time after one cycle finishes before the next cycle starts)
- `SEARCH_LAST_24H_ONLY` default `true` (adds LinkedIn `f_TPR=r86400`)
- `APPLICATION_SCORE_THRESHOLD` default `60` (used for score calculation only)
- `TELEGRAM_ENABLED` default `false`
- `TELEGRAM_BOT_TOKEN` required when `TELEGRAM_ENABLED=true`
- `TELEGRAM_RELEVANT_JOBS_CHAT_ID` required when `TELEGRAM_ENABLED=true`
- `TELEGRAM_NON_RELEVANT_JOBS_CHAT_ID` required when `TELEGRAM_ENABLED=true`
- `TELEGRAM_RELEVANT_SCORE_THRESHOLD` default `30` (scores `>= threshold` go to `Relevant Jobs`, lower scores to `Non-Relevant Jobs`)
- `SEARCH_QUERIES` format: `keyword|location;keyword|location`

Default searches:

- `Java or Full Stack or Software Engineer posted in the past 24 hours`

## Manual LinkedIn Authentication

Run setup mode once, log in manually in Chrome, then stop with `Ctrl+C`.

```powershell
python -m app.main --setup
```

This app does not enter credentials automatically.

## Running

One-time search cycle:

```powershell
python -m app.main --once
```

Status summary (jobs/applications counts by status):

```powershell
python -m app.main --status
```

Continuous mode (immediate run, then a 5-minute wait after each completed cycle):

```powershell
python -m app.main
```

Standalone metadata backfill (fills missing title/company/description/posted/apply method for existing DB rows):

```powershell
python -m app.backfill_metadata --limit 200
```

Backfill always applies a small built-in delay between jobs to improve metadata loading reliability.

## Database

SQLite file location is controlled by `SQLITE_DB_PATH` (default `data/jobs.db`).

Tables:

- `jobs`
- `applications`

Duplicate prevention uses `source + external_id`, and falls back to a hash of normalized URL if external ID is missing.
`jobs.apply_method` stores `EASY_APPLY`, `EXTERNAL`, or `UNKNOWN`.
`jobs.score` and `jobs.score_keywords` store rule-based scoring output.
All newly inserted jobs are stored as `NEW` and remain `NEW` until Telegram delivery succeeds.
After successful delivery, status becomes `SENT_TO_RELEVANT_JOBS` or `SENT_TO_NON_RELEVANT_JOBS` based on score.

Scoring (max 90):

- Title contains `Java` or `Spring`: `60`
- Otherwise title contains `Backend`, `Full Stack`, `Software Engineer`, or `Software Developer`: `30`
- Description contains `Java`: `+15`
- Description contains `Spring`: `+15`

## Tests

Tests mock browser interactions and do not require a LinkedIn account.

```powershell
pytest -q
```

Covered:

- URL normalization
- duplicate job prevention
- database CRUD
- job status transitions
- config validation
- scheduler overlap prevention
- job cycle behavior under failures/restrictions

## Known Limitations

- LinkedIn markup can change and may require selector updates in `app/linkedin.py`.
- Access restrictions (CAPTCHA/verification) stop the affected cycle by design.
- No automatic application submission is implemented yet (manual review flow only).
