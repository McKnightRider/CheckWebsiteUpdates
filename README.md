# CheckWebsiteUpdates

A small website monitor for [cdsdeterminationscommittees.org](https://www.cdsdeterminationscommittees.org/) and its internal sub-pages.

## What it does

- Crawls the CDS Determinations Committee site (including internal sub-pages)
- Builds a content digest of fetched HTML pages
- Runs checks every 12 hours
- Sends a notification when the digest changes (via webhook)
- Exposes a small status website

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Run this project with `python app.py`; this startup path starts both the web server and the background monitor.

## Configuration

- `NOTIFICATION_WEBHOOK_URL` (optional): webhook URL to receive change notifications.
- `STATE_PATH` (optional, default `data/state.json`): file where the last digest is stored.
- `CHECK_NOW_TOKEN` (optional but recommended): required token for `POST /check-now`, sent as `X-Check-Token` header.

## Endpoints

- `GET /` - monitor status and last check result
- `POST /check-now` - trigger an immediate check (requires `X-Check-Token` header matching `CHECK_NOW_TOKEN`)
