# Check DC Website Updates

A small website monitor for selected high-priority pages on [cdsdeterminationscommittees.org](https://www.cdsdeterminationscommittees.org/).

## What it does

- Checks a focused set of important CDS Determinations Committee pages
- Builds a content digest of fetched HTML pages
- Runs checks every 12 hours
- Tracks the date and time of each check plus the pages changed in that check
- Publishes a GitHub Pages status site under `website/` with the latest check and recent history
- Keeps a tracked top-level `website/` folder in the repository containing the generated HTML, CSS, JavaScript, JSON, and CSV files
- Saves the Pages history data as a spreadsheet-friendly CSV alongside the site assets
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
- `MONITORED_URLS` (optional): comma-separated list of specific pages to monitor. Defaults to the homepage, credit default swaps management, about DC committees, DC rules, and governance committee pages.
- `NOTIFICATION_WEBHOOK_URL` (optional): webhook URL to receive change notifications.
- `STATE_PATH` (optional, default `site_data/monitor_state.json`): file where the last site digest and per-page digests are stored.
- `HISTORY_PATH` (optional, default `site_data/history.json`): file where recent check history is stored.
- `SITE_OUTPUT_DIR` (optional, default `site`): base directory for the generated GitHub Pages site. The published assets are written under `site/website/`, then mirrored into the tracked repository `website/` folder by GitHub Actions.
- `CHECK_NOW_TOKEN` (optional but recommended): required token for `POST /check-now`, sent as `X-Check-Token` header.
- `CHECK_NOW_ALLOWED_ORIGINS` (optional, default `*`): comma-separated list of browser origins allowed to call `POST /check-now`.
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

## Manual refresh from the website

The generated website includes a **Refresh** form that calls the monitor app's `POST /check-now` endpoint from the browser. Enter the deployed monitor service endpoint URL and the check token when prompted. If you are viewing the static GitHub Pages site, use the full monitor service URL rather than a relative path.

## Default monitored pages

By default, checks and notifications are limited to these pages:

- `https://www.cdsdeterminationscommittees.org/`
- `https://www.cdsdeterminationscommittees.org/credit-default-swaps-management/`
- `https://www.cdsdeterminationscommittees.org/about-dc-committees/`
- `https://www.cdsdeterminationscommittees.org/dc-rules/`
- `https://www.cdsdeterminationscommittees.org/governance-committee/`

## GitHub Pages workflow

The repository includes a GitHub Actions workflow that:

- runs on a 12-hour schedule, on manual dispatch, and on pushes to `main`
- generates the static GitHub Pages site in `site/website/`
- copies the generated HTML, CSS, JavaScript, JSON, and CSV files into the repository's top-level `website/` folder
- saves the rendered history data in `site/website/history.csv`
- stores persistent monitor state and check history in `site_data/`
- commits updated `website/` assets back to the repository so they are visible in GitHub
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
