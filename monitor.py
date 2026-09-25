import hashlib
import json
import logging
import os
import queue
import re
import threading
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class MonitorResult:
    checked_at: str
    changed: bool
    current_digest: str
    previous_digest: Optional[str]
    page_count: int


def _normalize_url(raw_url: str) -> str:
    normalized, _ = urldefrag(raw_url)
    return normalized.rstrip("/") or normalized


def _extract_links(html: str, page_url: str, allowed_host: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for anchor in soup.find_all("a", href=True):
        candidate = _normalize_url(urljoin(page_url, anchor["href"]))
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc != allowed_host:
            continue
        links.add(candidate)
    return links


def _normalize_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text


def crawl_site(start_url: str, max_pages: int = 200, timeout: int = 20) -> Dict[str, str]:
    start_url = _normalize_url(start_url)
    allowed_host = urlparse(start_url).netloc

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

            text = _normalize_text(response.text)
            content_by_url[current] = text

            for link in _extract_links(response.text, current, allowed_host):
                if link not in visited:
                    urls.put(link)
        except requests.RequestException as exc:
            logger.warning("Failed to fetch %s: %s", current, exc)

    return content_by_url


def calculate_digest(content_by_url: Dict[str, str]) -> str:
    hasher = hashlib.sha256()
    for url in sorted(content_by_url):
        hasher.update(url.encode("utf-8"))
        hasher.update(b"\n")
        hasher.update(content_by_url[url].encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _read_previous_digest(state_path: str) -> Optional[str]:
    if not os.path.exists(state_path):
        return None

    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    return state.get("digest")


def _write_state(state_path: str, digest: str) -> None:
    parent_dir = os.path.dirname(state_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    state = {"digest": digest, "updated_at": datetime.now(timezone.utc).isoformat()}
    write_dir = parent_dir or "."
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=write_dir, delete=False) as fh:
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())
        temp_path = fh.name
    os.replace(temp_path, state_path)


def send_notification(webhook_url: str, result: MonitorResult) -> None:
    if not webhook_url:
        logger.info("Change detected but NOTIFICATION_WEBHOOK_URL is not configured")
        return

    payload = {
        "text": (
            "CDS Determinations Committee website change detected. "
            f"Pages checked: {result.page_count}. "
            f"Previous digest: {result.previous_digest}. "
            f"Current digest: {result.current_digest}."
        ),
        "checked_at": result.checked_at,
    }
    response = requests.post(webhook_url, json=payload, timeout=20)
    response.raise_for_status()


def run_monitor_check(
    start_url: str,
    state_path: str,
    webhook_url: str,
    max_pages: int = 200,
) -> MonitorResult:
    contents = crawl_site(start_url=start_url, max_pages=max_pages)
    current_digest = calculate_digest(contents)
    previous_digest = _read_previous_digest(state_path)
    changed = previous_digest is not None and previous_digest != current_digest
    _write_state(state_path, current_digest)

    result = MonitorResult(
        checked_at=datetime.now(timezone.utc).isoformat(),
        changed=changed,
        current_digest=current_digest,
        previous_digest=previous_digest,
        page_count=len(contents),
    )

    if changed:
        send_notification(webhook_url=webhook_url, result=result)

    return result


class MonitorService:
    def __init__(
        self,
        start_url: str,
        state_path: str,
        webhook_url: str,
        interval_seconds: int = 12 * 60 * 60,
    ):
        self.start_url = start_url
        self.state_path = state_path
        self.webhook_url = webhook_url
        self.interval_seconds = interval_seconds
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._check_lock = threading.Lock()
        self.last_result: Optional[MonitorResult] = None

    def perform_check(self) -> MonitorResult:
        with self._check_lock:
            self.last_result = run_monitor_check(
                start_url=self.start_url,
                state_path=self.state_path,
                webhook_url=self.webhook_url,
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
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
