import logging
import os
from hmac import compare_digest

from flask import Flask, abort, jsonify, request

from monitor import MonitorService

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

START_URL = "https://www.cdsdeterminationscommittees.org"
STATE_PATH = os.getenv("STATE_PATH", "data/state.json")
WEBHOOK_URL = os.getenv("NOTIFICATION_WEBHOOK_URL", "")
CHECK_NOW_TOKEN = os.getenv("CHECK_NOW_TOKEN", "")

app = Flask(__name__)
service = MonitorService(
    start_url=START_URL,
    state_path=STATE_PATH,
    webhook_url=WEBHOOK_URL,
    interval_seconds=12 * 60 * 60,
)


@app.get("/")
def index():
    result = service.last_result
    if result is None:
        return jsonify({"status": "running", "last_check": None})

    return jsonify(
        {
            "status": "running",
            "last_check": {
                "checked_at": result.checked_at,
                "changed": result.changed,
                "current_digest": result.current_digest,
                "previous_digest": result.previous_digest,
                "page_count": result.page_count,
            },
        }
    )


@app.post("/check-now")
def check_now():
    submitted_token = request.headers.get("X-Check-Token", "")
    if not CHECK_NOW_TOKEN or not compare_digest(submitted_token, CHECK_NOW_TOKEN):
        abort(403)

    result = service.perform_check()
    return jsonify({"ok": True, "result": result.__dict__})


service.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
