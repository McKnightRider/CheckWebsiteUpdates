import logging
import os
from hmac import compare_digest
from threading import Lock

from flask import Flask, abort, jsonify, request

from monitor import DEFAULT_EMAIL_TO, DEFAULT_SITE_URL, EmailSettings, MonitorService

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

START_URL = os.getenv("START_URL", DEFAULT_SITE_URL)
STATE_PATH = os.getenv("STATE_PATH", "site_data/monitor_state.json")
HISTORY_PATH = os.getenv("HISTORY_PATH", "site_data/history.json")
SITE_OUTPUT_DIR = os.getenv("SITE_OUTPUT_DIR", "site")
WEBHOOK_URL = os.getenv("NOTIFICATION_WEBHOOK_URL", "")
CHECK_NOW_TOKEN = os.getenv("CHECK_NOW_TOKEN", "")
EMAIL_TO = os.getenv("EMAIL_TO", DEFAULT_EMAIL_TO)

app = Flask(__name__)
service = MonitorService(
    start_url=START_URL,
    state_path=STATE_PATH,
    webhook_url=WEBHOOK_URL,
    interval_seconds=12 * 60 * 60,
    history_path=HISTORY_PATH,
    site_output_dir=SITE_OUTPUT_DIR,
    email_settings=EmailSettings.from_env(),
)
_service_start_lock = Lock()
_service_started = False



def ensure_service_started() -> None:
    global _service_started
    if _service_started:
        return
    with _service_start_lock:
        if _service_started:
            return
        service.start()
        _service_started = True


@app.get("/")
def index():
    result = service.last_result
    if result is None:
        return jsonify(
            {
                "status": "running",
                "last_check": None,
                "pages_site_output": SITE_OUTPUT_DIR,
                "email_to": EMAIL_TO,
            }
        )

    return jsonify(
        {
            "status": "running",
            "last_check": result.to_dict(),
            "pages_site_output": SITE_OUTPUT_DIR,
            "email_to": EMAIL_TO,
        }
    )


@app.post("/check-now")
def check_now():
    submitted_token = request.headers.get("X-Check-Token", "")
    if not CHECK_NOW_TOKEN or not compare_digest(submitted_token, CHECK_NOW_TOKEN):
        abort(403)

    result = service.perform_check()
    return jsonify({"ok": True, "result": result.to_dict()})


@app.before_request
def startup():
    ensure_service_started()


if __name__ == "__main__":
    ensure_service_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
