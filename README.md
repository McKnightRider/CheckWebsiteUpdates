# Check DC Website Updates

A small website monitor for [cdsdeterminationscommittees.org](https://www.cdsdeterminationscommittees.org/) and its internal sub-pages.

## What it does

- Crawls the CDS Determinations Committee site (including internal sub-pages)
- Builds a content digest of fetched HTML pages
- Runs checks every 12 hours
- Tracks the date and time of each check plus the pages changed in that check
- Publishes a GitHub Pages status site with the latest check and recent history
- Sends change notifications by webhook and email when configured
- Exposes a small status website

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Run this project with `python app.py` (or another Flask/WSGI entrypoint). The monitor service starts once the app receives its first request.

To run a single check and generate the static Pages site locally:

```bash
python monitor.py
```

## Configuration

- `START_URL` (optional, default `https://www.cdsdeterminationscommittees.org`): site to monitor.
- `NOTIFICATION_WEBHOOK_URL` (optional): webhook URL to receive change notifications.
- `STATE_PATH` (optional, default `site_data/monitor_state.json`): file where the last site digest and per-page digests are stored.
- `HISTORY_PATH` (optional, default `site_data/history.json`): file where recent check history is stored.
- `SITE_OUTPUT_DIR` (optional, default `site`): directory for the generated GitHub Pages site.
- `CHECK_NOW_TOKEN` (optional but recommended): required token for `POST /check-now`, sent as `X-Check-Token` header.
- `EMAIL_TO` (optional): recipient for change emails.
- `EMAIL_FROM` (required for email sending): sender address used for change emails.
- `EMAIL_SMTP_HOST` (required for email sending): SMTP server hostname.
- `EMAIL_SMTP_PORT` (optional, default `587`): SMTP server port.
- `EMAIL_SMTP_USERNAME` (optional): SMTP username.
- `EMAIL_SMTP_PASSWORD` (optional): SMTP password.
- `EMAIL_USE_TLS` (optional, default `true`): whether to use STARTTLS.

## Endpoints

- `GET /` - monitor status and last check result
- `POST /check-now` - trigger an immediate check (requires `X-Check-Token` header matching `CHECK_NOW_TOKEN`)

## GitHub Pages workflow

The repository includes a GitHub Actions workflow that:

- runs on a 12-hour schedule, on manual dispatch, and on pushes to `main`
- generates the static GitHub Pages site in `site/`
- stores persistent monitor state and check history in `site_data/`
- deploys the generated site to GitHub Pages
- commits updated `site_data/` files back to the repository so the next run can compare against the previous check

To enable email delivery in GitHub Actions, add these repository secrets if your SMTP server requires them:

- `EMAIL_SMTP_HOST`
- `EMAIL_SMTP_PORT`
- `EMAIL_SMTP_USERNAME`
- `EMAIL_SMTP_PASSWORD`
- `EMAIL_TO`
- `EMAIL_FROM`
- `EMAIL_USE_TLS` (optional)
