import logging
import os
from hmac import compare_digest
from threading import Lock

from flask import Flask, jsonify, request

from monitor import (
    DEFAULT_EMAIL_TO,
    DEFAULT_SITE_URL,
    EmailSettings,
    MonitorService,
    get_monitored_urls_from_env,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

START_URL = os.getenv("START_URL", DEFAULT_SITE_URL)
MONITORED_URLS = get_monitored_urls_from_env(START_URL)
STATE_PATH = os.getenv("STATE_PATH", "site_data/monitor_state.json")
HISTORY_PATH = os.getenv("HISTORY_PATH", "site_data/history.json")
SITE_OUTPUT_DIR = os.getenv("SITE_OUTPUT_DIR", "site")
WEBHOOK_URL = os.getenv("NOTIFICATION_WEBHOOK_URL", "")
CHECK_NOW_TOKEN = os.getenv("CHECK_NOW_TOKEN", "")
CHECK_NOW_ENDPOINT = os.getenv("CHECK_NOW_ENDPOINT") or "/check-now"
CHECK_NOW_ALLOWED_ORIGINS = tuple(
    origin.strip()
    for origin in os.getenv("CHECK_NOW_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
)
EMAIL_TO = os.getenv("EMAIL_TO", DEFAULT_EMAIL_TO)

app = Flask(__name__)
service = MonitorService(
    start_url=START_URL,
    state_path=STATE_PATH,
    webhook_url=WEBHOOK_URL,
    interval_seconds=12 * 60 * 60,
    monitored_urls=MONITORED_URLS,
    history_path=HISTORY_PATH,
    site_output_dir=SITE_OUTPUT_DIR,
    email_settings=EmailSettings.from_env(),
    check_now_endpoint=CHECK_NOW_ENDPOINT,
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


def _resolve_check_now_allow_origin(request_origin: str) -> str | None:
    if not request_origin:
        return None
    if "*" in CHECK_NOW_ALLOWED_ORIGINS:
        return "*"
    if request_origin in CHECK_NOW_ALLOWED_ORIGINS:
        return request_origin
    return None


def _cors_json_response(payload: dict, status_code: int = 200):
    response = jsonify(payload)
    response.status_code = status_code
    request_origin = request.headers.get("Origin", "")
    allowed_origin = _resolve_check_now_allow_origin(request_origin)
    if request_origin and allowed_origin is None:
        return response
    if allowed_origin is not None:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "X-Check-Token"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "600"
    return response


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


@app.route("/check-now", methods=["POST", "OPTIONS"])
def check_now():
    if request.method == "OPTIONS":
        return _cors_json_response({"ok": True})

    request_origin = request.headers.get("Origin", "")
    if request_origin and _resolve_check_now_allow_origin(request_origin) is None:
        return _cors_json_response({"ok": False, "error": "Origin not allowed"}, status_code=403)

    submitted_token = request.headers.get("X-Check-Token", "")
    if not CHECK_NOW_TOKEN or not compare_digest(submitted_token, CHECK_NOW_TOKEN):
        return _cors_json_response({"ok": False, "error": "Forbidden"}, status_code=403)

    result = service.perform_check()
    return _cors_json_response({"ok": True, "result": result.to_dict()})


@app.before_request
def startup():
    ensure_service_started()


if __name__ == "__main__":
    ensure_service_started()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
