import csv
import hashlib
import io
import json
import logging
import os
import queue
import re
import shutil
import smtplib
import ssl
import threading
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional, TextIO
from urllib.parse import urldefrag, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

try:  # pragma: no cover - OS-dependent import
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None

logger = logging.getLogger(__name__)
DEFAULT_SITE_URL = "https://www.cdsdeterminationscommittees.org"
DEFAULT_EMAIL_TO = ""
WEBSITE_DIRNAME = "website"
WEBSITE_STYLESHEET = """\
:root {
  color-scheme: light dark;
  font-family: Arial, sans-serif;
}

body {
  margin: 0;
  padding: 2rem;
  background: #0f172a;
  color: #e2e8f0;
}

a {
  color: #93c5fd;
}

.layout {
  max-width: 960px;
  margin: 0 auto;
}

.card {
  background: rgba(15, 23, 42, 0.75);
  border: 1px solid rgba(148, 163, 184, 0.3);
  border-radius: 12px;
  padding: 1.25rem;
  margin-bottom: 1rem;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.25);
}

.history-item h3 {
  margin-top: 0;
}

ul {
  padding-left: 1.25rem;
}

.resource-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  padding-left: 0;
  list-style: none;
}

.resource-list a {
  display: inline-block;
  padding: 0.65rem 0.9rem;
  border-radius: 999px;
  text-decoration: none;
  background: rgba(59, 130, 246, 0.15);
  border: 1px solid rgba(147, 197, 253, 0.35);
}
"""
WEBSITE_SCRIPT = """\
document.addEventListener("DOMContentLoaded", () => {
  const historyItems = document.querySelectorAll(".history-item");
  const historyCount = document.getElementById("history-count");
  if (historyCount) {
    historyCount.textContent = String(historyItems.length);
  }

  for (const element of document.querySelectorAll("[data-checked-at]")) {
    const checkedAt = element.getAttribute("data-checked-at");
    if (checkedAt) {
      element.title = checkedAt;
    }
  }
});
"""


@dataclass(frozen=True)
class PageChange:
    url: str
    change_type: str


@dataclass
class MonitorResult:
    checked_at: str
    changed: bool
    current_digest: str
    previous_digest: Optional[str]
    page_count: int
    page_changes: list[PageChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "changed": self.changed,
            "current_digest": self.current_digest,
            "previous_digest": self.previous_digest,
            "page_count": self.page_count,
            "page_changes": [asdict(change) for change in self.page_changes],
        }


@dataclass
class EmailSettings:
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    from_address: str = ""
    to_address: str = DEFAULT_EMAIL_TO
    use_tls: bool = True

    @classmethod
    def from_env(cls) -> "EmailSettings":
        smtp_port = os.getenv("EMAIL_SMTP_PORT", "587")
        smtp_password = os.getenv("EMAIL_SMTP_PASSWORD", "")
        use_tls = os.getenv("EMAIL_USE_TLS", "true").lower() not in {"0", "false", "no"}
        try:
            parsed_port = int(smtp_port)
        except ValueError:
            parsed_port = 587

        return cls(
            os.getenv("EMAIL_SMTP_HOST", ""),
            parsed_port,
            os.getenv("EMAIL_SMTP_USERNAME", ""),
            smtp_password,
            os.getenv("EMAIL_FROM", ""),
            os.getenv("EMAIL_TO", DEFAULT_EMAIL_TO),
            use_tls,
        )



def _normalize_url(
    raw_url: str,
    canonical_host: Optional[str] = None,
    canonical_scheme: Optional[str] = None,
    canonical_port: Optional[int] = None,
) -> str:
    normalized, _ = urldefrag(raw_url.strip())
    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        host = (parsed.hostname or "").lower()
        if not host:
            return normalized.rstrip("/") or normalized
        original_scheme = parsed.scheme.lower()
        scheme = original_scheme
        canonical_host_lower = canonical_host.lower() if canonical_host else None
        try:
            port = parsed.port
        except ValueError:
            return normalized.rstrip("/") or normalized
        has_explicit_port = port is not None
        if canonical_host_lower and host == canonical_host_lower:
            host = canonical_host_lower
            if canonical_scheme:
                scheme = canonical_scheme.lower()
            if canonical_port is not None:
                port = canonical_port
        default_port = 443 if scheme == "https" else 80
        should_strip_default_port = canonical_port is not None or scheme == original_scheme
        if has_explicit_port and canonical_port is not None:
            should_strip_default_port = False
        if should_strip_default_port and port == default_port:
            port = None
        userinfo = ""
        if parsed.username is not None:
            userinfo = parsed.username
            if parsed.password is not None:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        netloc = f"{userinfo}{host}"
        if port is not None:
            netloc += f":{port}"
        parsed = parsed._replace(scheme=scheme, netloc=netloc)
        normalized = parsed.geturl()
    return normalized.rstrip("/") or normalized



def _extract_links(
    html: str,
    page_url: str,
    allowed_host: str,
    allowed_port: int,
    canonical_scheme: str,
    canonical_port: Optional[int] = None,
) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for anchor in soup.find_all("a", href=True):
        candidate = _normalize_url(
            urljoin(page_url, anchor["href"]),
            canonical_host=allowed_host,
            canonical_scheme=canonical_scheme,
            canonical_port=canonical_port,
        )
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if (parsed.hostname or "").lower() != allowed_host:
            continue
        candidate_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if candidate_port != allowed_port:
            continue
        links.add(candidate)
    return links



def _normalize_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)



def crawl_site(start_url: str, max_pages: int = 200, timeout: int = 20) -> Dict[str, str]:
    start_url = _normalize_url(start_url)
    parsed_start_url = urlparse(start_url)
    allowed_host = (parsed_start_url.hostname or parsed_start_url.netloc).lower()
    canonical_scheme = parsed_start_url.scheme.lower()
    canonical_port = parsed_start_url.port
    default_port_for_scheme = 443 if canonical_scheme == "https" else 80
    allowed_port = canonical_port or default_port_for_scheme
    if canonical_port == default_port_for_scheme:
        canonical_port = None
    start_url = _normalize_url(
        start_url,
        canonical_host=allowed_host,
        canonical_scheme=canonical_scheme,
        canonical_port=canonical_port,
    )

    session = requests.Session()
    urls = queue.Queue()
    urls.put(start_url)
    visited = set()
    content_by_url: Dict[str, str] = {}

    while not urls.empty() and len(visited) < max_pages:
        current = urls.get()
        if current in visited:
            continue

        visited.add(current)
        try:
            response = session.get(current, timeout=timeout)
            response.raise_for_status()
            if "text/html" not in response.headers.get("content-type", ""):
                continue

            content_by_url[current] = _normalize_text(response.text)

            for link in _extract_links(
                response.text, current, allowed_host, allowed_port, canonical_scheme, canonical_port
            ):
                if link not in visited:
                    urls.put(link)
        except requests.RequestException as exc:
            logger.warning("Failed to fetch %s: %s", current, exc)

    return content_by_url



def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()



def build_page_digests(content_by_url: Dict[str, str]) -> Dict[str, str]:
    return {url: _digest_text(content) for url, content in content_by_url.items()}



def calculate_digest(content_by_url: Dict[str, str]) -> str:
    hasher = hashlib.sha256()
    for url in sorted(content_by_url):
        hasher.update(url.encode("utf-8"))
        hasher.update(b"\n")
        hasher.update(content_by_url[url].encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()



def detect_page_changes(previous_page_digests: Dict[str, str], current_page_digests: Dict[str, str]) -> list[PageChange]:
    changes: list[PageChange] = []
    for url in sorted(set(previous_page_digests) | set(current_page_digests)):
        if url not in previous_page_digests:
            changes.append(PageChange(url=url, change_type="added"))
        elif url not in current_page_digests:
            changes.append(PageChange(url=url, change_type="removed"))
        elif previous_page_digests[url] != current_page_digests[url]:
            changes.append(PageChange(url=url, change_type="updated"))
    return changes



def _read_state(state_path: str) -> Optional[dict[str, Any]]:
    if not os.path.exists(state_path):
        return None

    with open(state_path, "r", encoding="utf-8") as fh:
        return json.load(fh)



def _read_previous_digest(state_path: str) -> Optional[str]:
    state = _read_state(state_path)
    if not state:
        return None
    return state.get("digest")



def _atomic_write_json(path: str, payload: Any) -> None:
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    write_dir = parent_dir or "."
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=write_dir, delete=False) as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
        temp_path = fh.name
    os.replace(temp_path, path)



def _write_state(state_path: str, digest: str, page_digests: Dict[str, str]) -> None:
    _atomic_write_json(
        state_path,
        {
            "digest": digest,
            "page_digests": page_digests,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )



def _with_state_lock(state_path: str):
    lock_path = f"{state_path}.lock"
    lock_dir = os.path.dirname(lock_path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    lock_file = open(lock_path, "w", encoding="utf-8")
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file



def _load_history(history_path: str) -> list[dict[str, Any]]:
    if not os.path.exists(history_path):
        return []

    with open(history_path, "r", encoding="utf-8") as fh:
        history = json.load(fh)

    return history if isinstance(history, list) else []



def _append_history(history_path: Optional[str], result: MonitorResult, limit: int = 100) -> list[dict[str, Any]]:
    if not history_path:
        return [result.to_dict()]

    history = _load_history(history_path)
    history.append(result.to_dict())
    trimmed_history = history[-limit:]
    _atomic_write_json(history_path, trimmed_history)
    return trimmed_history



def _format_timestamp(timestamp: str) -> str:
    try:
        normalized_timestamp = timestamp.strip()
        if normalized_timestamp.endswith(("Z", "z")):
            normalized_timestamp = f"{normalized_timestamp[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized_timestamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        zone_abbr_override = None
        try:
            london_time = parsed.astimezone(ZoneInfo("Europe/London"))
        except ZoneInfoNotFoundError:
            london_time = parsed.astimezone(timezone.utc)
            zone_abbr_override = "GMT"
        month_name = london_time.strftime("%B")
        hour_12 = london_time.hour % 12 or 12
        am_pm = "AM" if london_time.hour < 12 else "PM"
        zone_abbr = zone_abbr_override or london_time.tzname() or "GMT"
        return (
            f"{london_time.day} {month_name} {london_time.year} at "
            f"{hour_12}:{london_time.minute:02d}:{london_time.second:02d} {am_pm} {zone_abbr}"
        )
    except ValueError:
        return timestamp



def _render_change_items(changes: list[dict[str, str]]) -> str:
    if not changes:
        return "<li>No page changes detected.</li>"

    items = []
    for change in changes:
        change_type = escape(change["change_type"].title())
        url = escape(change["url"])
        parsed = urlparse(change["url"])
        if parsed.scheme in {"http", "https"}:
            items.append(f'<li><strong>{change_type}</strong>: <a href="{url}">{url}</a></li>')
        else:
            items.append(f"<li><strong>{change_type}</strong>: {url}</li>")
    return "".join(items)



def _build_history_csv(history: list[dict[str, Any]]) -> str:
    rows = io.StringIO()
    writer = csv.DictWriter(
        rows,
        fieldnames=[
            "checked_at",
            "checked_at_display",
            "changed",
            "page_count",
            "previous_digest",
            "current_digest",
            "page_changes",
        ],
    )
    writer.writeheader()
    for entry in history:
        writer.writerow(
            {
                "checked_at": entry.get("checked_at", ""),
                "checked_at_display": _format_timestamp(str(entry.get("checked_at", ""))),
                "changed": "true" if entry.get("changed") else "false",
                "page_count": entry.get("page_count", ""),
                "previous_digest": entry.get("previous_digest", "") or "",
                "current_digest": entry.get("current_digest", "") or "",
                "page_changes": json.dumps(entry.get("page_changes", []), separators=(",", ":")),
            }
        )
    return rows.getvalue()



def generate_site_html(start_url: str, history: list[dict[str, Any]]) -> str:
    latest = history[-1] if history else None
    title = "CDS Determinations Committee Monitor"

    if latest:
        latest_summary = f"""
        <section class=\"card\">
          <h2>Latest check</h2>
          <p><strong>Checked at:</strong> <span data-checked-at="{escape(latest['checked_at'])}">{escape(_format_timestamp(latest['checked_at']))}</span></p>
          <p><strong>Status:</strong> {'Changes detected' if latest['changed'] else 'No changes detected'}</p>
          <p><strong>Pages checked:</strong> {latest['page_count']}</p>
          <ul>{_render_change_items(latest.get('page_changes', []))}</ul>
        </section>
        """
    else:
        latest_summary = """
        <section class=\"card\">
          <h2>Latest check</h2>
          <p>No checks have run yet.</p>
        </section>
        """

    history_markup = "".join(
        f"""
        <article class=\"card history-item\">
          <h3 data-checked-at="{escape(entry['checked_at'])}">{escape(_format_timestamp(entry['checked_at']))}</h3>
          <p><strong>Status:</strong> {'Changes detected' if entry['changed'] else 'No changes detected'}</p>
          <p><strong>Pages checked:</strong> {entry['page_count']}</p>
          <ul>{_render_change_items(entry.get('page_changes', []))}</ul>
        </article>
        """
        for entry in reversed(history)
    ) or "<p>No checks have run yet.</p>"

    return f"""<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>{title}</title>
    <link rel=\"stylesheet\" href=\"styles.css\">
  </head>
  <body>
    <main class=\"layout\">
      <section class=\"card\">
        <h1>{title}</h1>
        <p>This site tracks checks against <a href=\"{escape(start_url)}\">{escape(start_url)}</a>.</p>
        <ul class=\"resource-list\">
          <li><a href=\"history.csv\">Download history spreadsheet</a></li>
          <li><a href=\"history.json\">Download history JSON</a></li>
        </ul>
      </section>
      {latest_summary}
      <section class=\"card\">
        <h2>Recent checks (<span id=\"history-count\">0</span>)</h2>
        {history_markup}
      </section>
    </main>
    <script src=\"app.js\"></script>
  </body>
</html>
"""



def _generate_site_redirect_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Website entry</title>
  </head>
  <body>
    <main>
      <p>Open the published website: <a href="website/">Open the monitor website</a>.</p>
    </main>
  </body>
</html>
"""



def write_site_files(site_output_dir: Optional[str], start_url: str, history: list[dict[str, Any]]) -> None:
    if not site_output_dir:
        return

    output_dir = Path(site_output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    website_dir = output_dir / WEBSITE_DIRNAME
    if website_dir.is_symlink():
        website_dir.unlink()
    elif website_dir.exists():
        shutil.rmtree(website_dir)
    website_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(_generate_site_redirect_html(), encoding="utf-8")
    (website_dir / "index.html").write_text(generate_site_html(start_url=start_url, history=history), encoding="utf-8")
    (website_dir / "styles.css").write_text(WEBSITE_STYLESHEET, encoding="utf-8")
    (website_dir / "app.js").write_text(WEBSITE_SCRIPT, encoding="utf-8")
    (website_dir / "history.json").write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    (website_dir / "history.csv").write_text(_build_history_csv(history), encoding="utf-8")



def send_notification(webhook_url: str, result: MonitorResult) -> None:
    if not webhook_url:
        logger.info("Change detected but NOTIFICATION_WEBHOOK_URL is not configured")
        return

    changed_pages = "; ".join(f"{change.change_type}: {change.url}" for change in result.page_changes)
    if not changed_pages:
        changed_pages = "No page details available"

    payload = {
        "text": (
            "CDS Determinations Committee website change detected. "
            f"Pages checked: {result.page_count}. "
            f"Changed pages: {changed_pages}. "
            f"Previous digest: {result.previous_digest}. "
            f"Current digest: {result.current_digest}."
        ),
        "checked_at": result.checked_at,
        "page_changes": [asdict(change) for change in result.page_changes],
    }
    response = requests.post(webhook_url, json=payload, timeout=20)
    response.raise_for_status()



def send_email_notification(email_settings: EmailSettings, result: MonitorResult) -> None:
    if not email_settings.smtp_host or not email_settings.from_address or not email_settings.to_address:
        logger.info("Change detected but email settings are incomplete")
        return

    message = EmailMessage()
    message["Subject"] = "CDS Determinations Committee website change detected"
    message["From"] = email_settings.from_address
    message["To"] = email_settings.to_address

    page_lines = "\n".join(f"- {change.change_type.title()}: {change.url}" for change in result.page_changes)
    if not page_lines:
        page_lines = "- A change was detected, but no page details were captured."

    message.set_content(
        "\n".join(
            [
                "A change was detected on the CDS Determinations Committee website.",
                f"Checked at: {result.checked_at}",
                f"Pages checked: {result.page_count}",
                "Changed pages:",
                page_lines,
            ]
        )
    )

    with smtplib.SMTP(email_settings.smtp_host, email_settings.smtp_port, timeout=20) as smtp:
        if email_settings.use_tls:
            smtp.starttls(context=ssl.create_default_context())
        if email_settings.username:
            smtp.login(email_settings.username, email_settings.password)
        smtp.send_message(message)



def run_monitor_check(
    start_url: str,
    state_path: str,
    webhook_url: str,
    max_pages: int = 200,
    history_path: Optional[str] = None,
    site_output_dir: Optional[str] = None,
    email_settings: Optional[EmailSettings] = None,
    history_limit: int = 100,
) -> MonitorResult:
    with _with_state_lock(state_path):
        contents = crawl_site(start_url=start_url, max_pages=max_pages)
        current_page_digests = build_page_digests(contents)
        current_digest = calculate_digest(contents)

        previous_state = _read_state(state_path) or {}
        previous_digest = previous_state.get("digest")
        previous_page_digests = previous_state.get("page_digests") or {}
        changed = previous_digest is not None and previous_digest != current_digest
        page_changes = detect_page_changes(previous_page_digests, current_page_digests) if changed else []

        _write_state(state_path, current_digest, current_page_digests)

        result = MonitorResult(
            checked_at=datetime.now(timezone.utc).isoformat(),
            changed=changed,
            current_digest=current_digest,
            previous_digest=previous_digest,
            page_count=len(contents),
            page_changes=page_changes,
        )
        history = _append_history(history_path, result, limit=history_limit)

    write_site_files(site_output_dir, start_url=start_url, history=history)

    if changed:
        send_notification(webhook_url=webhook_url, result=result)
        if email_settings is not None:
            send_email_notification(email_settings=email_settings, result=result)

    return result


class MonitorService:
    def __init__(
        self,
        start_url: str,
        state_path: str,
        webhook_url: str,
        interval_seconds: int = 12 * 60 * 60,
        history_path: Optional[str] = None,
        site_output_dir: Optional[str] = None,
        email_settings: Optional[EmailSettings] = None,
    ):
        self.start_url = start_url
        self.state_path = state_path
        self.webhook_url = webhook_url
        self.interval_seconds = interval_seconds
        self.history_path = history_path
        self.site_output_dir = site_output_dir
        self.email_settings = email_settings
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._check_lock = threading.Lock()
        self._leader_lock_file: Optional[TextIO] = None
        self.last_result: Optional[MonitorResult] = None

    def perform_check(self) -> MonitorResult:
        with self._check_lock:
            self.last_result = run_monitor_check(
                start_url=self.start_url,
                state_path=self.state_path,
                webhook_url=self.webhook_url,
                history_path=self.history_path,
                site_output_dir=self.site_output_dir,
                email_settings=self.email_settings,
            )
            return self.last_result

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.perform_check()
                logger.info("Completed monitor check: %s", self.last_result)
            except Exception as exc:  # pragma: no cover
                logger.exception("Monitor check failed: %s", exc)

            self._stop_event.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        lock_path = f"{self.state_path}.leader.lock"
        lock_dir = os.path.dirname(lock_path)
        if lock_dir:
            os.makedirs(lock_dir, exist_ok=True)
        lock_file = open(lock_path, "w", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                logger.info("Another process already owns monitor leadership; skipping thread startup")
                return
        self._leader_lock_file = lock_file
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._leader_lock_file:
            if fcntl is not None:
                fcntl.flock(self._leader_lock_file.fileno(), fcntl.LOCK_UN)
            self._leader_lock_file.close()
            self._leader_lock_file = None


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    result = run_monitor_check(
        start_url=os.getenv("START_URL", DEFAULT_SITE_URL),
        state_path=os.getenv("STATE_PATH", "site_data/monitor_state.json"),
        webhook_url=os.getenv("NOTIFICATION_WEBHOOK_URL", ""),
        max_pages=int(os.getenv("MAX_PAGES", "200")),
        history_path=os.getenv("HISTORY_PATH", "site_data/history.json"),
        site_output_dir=os.getenv("SITE_OUTPUT_DIR", "site"),
        email_settings=EmailSettings.from_env(),
    )
    print(json.dumps(result.to_dict(), indent=2))
