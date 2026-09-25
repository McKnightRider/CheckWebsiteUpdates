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
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional, TextIO
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlparse
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
DEFAULT_MONITORED_URLS = (
    DEFAULT_SITE_URL,
    f"{DEFAULT_SITE_URL}/credit-default-swaps-management",
    f"{DEFAULT_SITE_URL}/about-dc-committees",
    f"{DEFAULT_SITE_URL}/dc-rules",
    f"{DEFAULT_SITE_URL}/governance-committee",
)
WEBSITE_DIRNAME = "website"
GENERATED_WEBSITE_MANIFEST = "asset-manifest.json"
GENERATED_WEBSITE_FILES = (
    "index.html",
    "styles.css",
    "app.js",
    "history.json",
    "history.csv",
    GENERATED_WEBSITE_MANIFEST,
)
MONITOR_WORKFLOW_FILENAME = "monitor-pages.yml"
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"gclid", "fbclid", "mc_cid", "mc_eid"}
NOISY_PATH_PREFIXES = ("/category/", "/tag/", "/author/")
NOISY_PATH_PATTERNS = (re.compile(r"/page/\d+/?$"),)
DEFAULT_STRUCTURE_CONFIRMATION_RUNS = 2
DEFAULT_FETCH_RETRIES = 2
DEFAULT_FETCH_BACKOFF_SECONDS = 0.5
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

.refresh-link {
  display: inline-block;
  padding: 0.75rem 0.9rem;
  border-radius: 0.75rem;
  text-decoration: none;
  background: rgba(59, 130, 246, 0.2);
  border: 1px solid rgba(147, 197, 253, 0.45);
}

.refresh-help,
.refresh-status {
  margin: 0;
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
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "changed": self.changed,
            "current_digest": self.current_digest,
            "previous_digest": self.previous_digest,
            "page_count": self.page_count,
            "page_changes": [asdict(change) for change in self.page_changes],
            "diagnostics": self.diagnostics,
        }


@dataclass
class CrawlResult:
    content_by_url: Dict[str, str]
    discovered_urls: set[str]
    fetch_failures: dict[str, str]


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



def get_monitored_urls(start_url: str, raw_urls: Optional[list[str]] = None) -> list[str]:
    normalized_start_url = _normalize_url(start_url)
    parsed_start_url = urlparse(normalized_start_url)
    allowed_host = (parsed_start_url.hostname or parsed_start_url.netloc).lower()
    canonical_scheme = parsed_start_url.scheme.lower()
    canonical_port = parsed_start_url.port
    default_port_for_scheme = 443 if canonical_scheme == "https" else 80
    if canonical_port == default_port_for_scheme:
        canonical_port = None

    if raw_urls is not None:
        selected_urls = raw_urls
    elif normalized_start_url == _normalize_url(DEFAULT_SITE_URL):
        selected_urls = list(DEFAULT_MONITORED_URLS)
    else:
        selected_urls = [normalized_start_url]
    normalized_urls: list[str] = []
    seen_urls: set[str] = set()
    for raw_url in selected_urls:
        normalized_url = _normalize_url(
            raw_url,
            canonical_host=allowed_host,
            canonical_scheme=canonical_scheme,
            canonical_port=canonical_port,
        )
        if normalized_url and normalized_url not in seen_urls:
            normalized_urls.append(normalized_url)
            seen_urls.add(normalized_url)
    return normalized_urls or [normalized_start_url]



def get_monitored_urls_from_env(start_url: str, env_var_name: str = "MONITORED_URLS") -> list[str]:
    raw_value = os.getenv(env_var_name, "")
    configured_urls = [item.strip() for item in raw_value.split(",") if item.strip()]
    return get_monitored_urls(start_url=start_url, raw_urls=configured_urls or None)



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
        query_items = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith(TRACKING_QUERY_PREFIXES) and key.lower() not in TRACKING_QUERY_KEYS
        ]
        query = urlencode(sorted(query_items), doseq=True) if query_items else ""
        parsed = parsed._replace(scheme=scheme, netloc=netloc, query=query)
        normalized = parsed.geturl()
    return normalized.rstrip("/") or normalized



def _is_noise_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(path.startswith(prefix) for prefix in NOISY_PATH_PREFIXES):
        return True
    if path == "/search":
        return True
    if any(pattern.search(path) for pattern in NOISY_PATH_PATTERNS):
        return True
    return False



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
        if _is_noise_url(candidate):
            continue
        links.add(candidate)
    return links



def _normalize_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)



def _fetch_html_with_retries(
    session: requests.Session,
    url: str,
    timeout: int,
    retries: int,
    backoff_seconds: float,
) -> tuple[Optional[str], Optional[str]]:
    last_error: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            if "text/html" not in response.headers.get("content-type", ""):
                return None, "non-html response"
            return response.text, None
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(backoff_seconds * (2**attempt))
    return None, last_error or "unknown fetch error"



def crawl_site(
    start_url: str,
    max_pages: int = 200,
    timeout: int = 20,
    retries: int = DEFAULT_FETCH_RETRIES,
    backoff_seconds: float = DEFAULT_FETCH_BACKOFF_SECONDS,
) -> CrawlResult:
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
    attempted = set()
    content_by_url: Dict[str, str] = {}
    fetch_failures: dict[str, str] = {}
    logger.info("Starting discovery crawl from %s (max_pages=%d)", start_url, max_pages)

    while not urls.empty() and len(visited) < max_pages:
        current = urls.get()
        if current in visited or current in attempted:
            continue
        attempted.add(current)
        html, error = _fetch_html_with_retries(
            session=session,
            url=current,
            timeout=timeout,
            retries=retries,
            backoff_seconds=backoff_seconds,
        )
        if html is None:
            if error:
                fetch_failures[current] = error
                logger.warning("Failed to fetch %s: %s", current, error)
            continue
        visited.add(current)
        content_by_url[current] = _normalize_text(html)
        for link in _extract_links(
            html, current, allowed_host, allowed_port, canonical_scheme, canonical_port
        ):
            if link not in visited and link not in attempted:
                urls.put(link)
        if len(attempted) == 1 or len(attempted) % 10 == 0 or urls.empty() or len(visited) >= max_pages:
            logger.info(
                "Discovery crawl progress: attempted=%d visited=%d queued=%d failures=%d",
                len(attempted),
                len(visited),
                urls.qsize(),
                len(fetch_failures),
            )

    return CrawlResult(content_by_url=content_by_url, discovered_urls=set(content_by_url), fetch_failures=fetch_failures)



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



def calculate_digest_from_page_digests(page_digests: Dict[str, str]) -> str:
    hasher = hashlib.sha256()
    for url in sorted(page_digests):
        hasher.update(url.encode("utf-8"))
        hasher.update(b"\n")
        hasher.update(page_digests[url].encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()



def fetch_inventory_pages(
    inventory_urls: set[str],
    timeout: int = 20,
    retries: int = DEFAULT_FETCH_RETRIES,
    backoff_seconds: float = DEFAULT_FETCH_BACKOFF_SECONDS,
) -> tuple[Dict[str, str], dict[str, str]]:
    session = requests.Session()
    content_by_url: Dict[str, str] = {}
    failures: dict[str, str] = {}
    total_urls = len(inventory_urls)
    logger.info("Fetching %d inventory pages", total_urls)
    for index, url in enumerate(sorted(inventory_urls), start=1):
        html, error = _fetch_html_with_retries(
            session=session,
            url=url,
            timeout=timeout,
            retries=retries,
            backoff_seconds=backoff_seconds,
        )
        if html is None:
            failures[url] = error or "unknown fetch error"
        else:
            content_by_url[url] = _normalize_text(html)
        if index == 1 or index % 25 == 0 or index == total_urls:
            logger.info(
                "Inventory fetch progress: processed=%d/%d successes=%d failures=%d",
                index,
                total_urls,
                len(content_by_url),
                len(failures),
            )
    return content_by_url, failures



def _as_str_int_map(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, int] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        try:
            parsed_value = int(value)
        except (TypeError, ValueError):
            continue
        if parsed_value > 0:
            normalized[key] = parsed_value
    return normalized



def reconcile_inventory(
    inventory_urls: set[str],
    discovered_urls: set[str],
    pending_added: dict[str, int],
    pending_removed: dict[str, int],
    confirmation_runs: int,
    allow_removals: bool,
) -> tuple[set[str], dict[str, int], dict[str, int], list[str]]:
    diagnostics: list[str] = []
    next_inventory = set(inventory_urls)

    add_candidates = discovered_urls - inventory_urls
    remove_candidates = inventory_urls - discovered_urls

    next_pending_added = {url: pending_added.get(url, 0) + 1 for url in add_candidates}
    confirmed_added = sorted(url for url, count in next_pending_added.items() if count >= confirmation_runs)
    for url in confirmed_added:
        next_inventory.add(url)
        next_pending_added.pop(url, None)
    if confirmed_added:
        diagnostics.append(f"Confirmed new URLs ({len(confirmed_added)}): {', '.join(confirmed_added)}")

    if allow_removals:
        next_pending_removed = {url: pending_removed.get(url, 0) + 1 for url in remove_candidates}
        confirmed_removed = sorted(url for url, count in next_pending_removed.items() if count >= confirmation_runs)
        for url in confirmed_removed:
            next_inventory.discard(url)
            next_pending_removed.pop(url, None)
        if confirmed_removed:
            diagnostics.append(f"Confirmed removed URLs ({len(confirmed_removed)}): {', '.join(confirmed_removed)}")
    else:
        next_pending_removed = {}
        if remove_candidates:
            diagnostics.append(
                f"Deferred {len(remove_candidates)} potential removals due to crawl failures in discovery."
            )

    return next_inventory, next_pending_added, next_pending_removed, diagnostics



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



def _write_state(
    state_path: str,
    digest: str,
    page_digests: Dict[str, str],
    canonical_urls: set[str],
    pending_added: dict[str, int],
    pending_removed: dict[str, int],
) -> None:
    _atomic_write_json(
        state_path,
        {
            "digest": digest,
            "page_digests": page_digests,
            "canonical_urls": sorted(canonical_urls),
            "pending_added": pending_added,
            "pending_removed": pending_removed,
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
            "diagnostics",
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
                "diagnostics": json.dumps(entry.get("diagnostics", []), separators=(",", ":")),
            }
        )
    return rows.getvalue()



def _render_history_heading(checked_at: str, *, is_first_check: bool = False) -> str:
    timestamp = escape(_format_timestamp(checked_at))
    if is_first_check:
        return f"First Check: {timestamp}"
    return timestamp



def _extract_github_repository(remote_url: str) -> Optional[str]:
    normalized_remote = remote_url.strip().rstrip("/")
    if not normalized_remote:
        return None
    for prefix in ("https://github.com/", "git@github.com:"):
        if normalized_remote.startswith(prefix):
            repository = normalized_remote[len(prefix):]
            if repository.endswith(".git"):
                repository = repository[:-4]
            owner, separator, repo = repository.partition("/")
            if separator and owner and repo and "/" not in repo:
                return f"{owner}/{repo}"
    return None



def _normalize_github_repository(repository: str) -> Optional[str]:
    owner, separator, repo = repository.strip().strip("/").partition("/")
    if separator and owner and repo and "/" not in repo:
        return f"{owner}/{repo}"
    return None



def _iter_git_config_paths():
    search_roots = [Path.cwd(), Path(__file__).resolve().parent]
    visited: set[Path] = set()
    for root in search_roots:
        for candidate_dir in (root, *root.parents):
            if candidate_dir in visited:
                continue
            visited.add(candidate_dir)
            git_config_path = candidate_dir / ".git" / "config"
            if git_config_path.is_file():
                yield git_config_path



def get_github_repository() -> Optional[str]:
    configured_repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    normalized_repository = _normalize_github_repository(configured_repository)
    if normalized_repository:
        return normalized_repository

    for git_config_path in _iter_git_config_paths():
        current_section = ""
        for raw_line in git_config_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                current_section = line
                continue
            if current_section != '[remote "origin"]' or not line.startswith("url ="):
                continue
            extracted_repository = _extract_github_repository(line.partition("=")[2])
            if extracted_repository:
                return extracted_repository
            break
    return None



def get_manual_refresh_workflow_url(repository: Optional[str]) -> Optional[str]:
    normalized_repository = _normalize_github_repository(repository or "")
    if not normalized_repository:
        return None
    owner, _, repo = normalized_repository.partition("/")
    return f"https://github.com/{owner}/{repo}/actions/workflows/{MONITOR_WORKFLOW_FILENAME}"



def generate_site_html(
    start_url: str,
    monitored_urls: Optional[list[str]],
    history: list[dict[str, Any]],
    manual_refresh_url: Optional[str] = None,
) -> str:
    latest = history[-1] if history else None
    title = "DC Website Update Monitor"
    displayed_monitored_urls = monitored_urls or [_normalize_url(start_url)]
    monitored_pages_markup = "".join(
        f'<li><a href="{escape(url)}">{escape(url)}</a></li>' for url in displayed_monitored_urls
    )

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

    if manual_refresh_url:
        manual_refresh_markup = f"""
        <p class=\"refresh-help\">To run an immediate check, open the GitHub Actions workflow and click <strong>Run workflow</strong>. You must be signed in with permission to run workflows for this repository.</p>
        <p><a id=\"refresh-button\" class=\"refresh-link\" href=\"{escape(manual_refresh_url)}\" target=\"_blank\" rel=\"noopener noreferrer\">Open Run workflow</a></p>
        <p id=\"refresh-status\" class=\"refresh-status\" aria-live=\"polite\">After the workflow finishes, reload this page to see the latest site output.</p>
        """
    else:
        manual_refresh_markup = """
        <p class=\"refresh-help\">Manual refresh is available from this repository's GitHub Actions workflow after the site is built in GitHub.</p>
        <p id=\"refresh-status\" class=\"refresh-status\" aria-live=\"polite\">Open the repository Actions tab and run the monitor workflow to refresh the site.</p>
        """

    history_markup = "".join(
        f"""
        <article class=\"card history-item\">
          <h3 data-checked-at="{escape(entry['checked_at'])}">{_render_history_heading(entry['checked_at'], is_first_check=len(history) == 1)}</h3>
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
        <p>This site tracks checks against selected CDS Determinations Committee pages.</p>
        <ul>{monitored_pages_markup}</ul>
        <ul class=\"resource-list\">
          <li><a href=\"history.csv\">Download history spreadsheet</a></li>
          <li><a href=\"history.json\">Download history JSON</a></li>
        </ul>
      </section>
      <section class=\"card\">
        <h2>Manual refresh</h2>
        {manual_refresh_markup}
      </section>
      {latest_summary}
      <section class=\"card\">
        <h2>Recent checks (<span id=\"history-count\">0</span>)</h2>
        {history_markup}
      </section>
    </main>
    <script src=\"app.js\" defer></script>
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



def _build_generated_website_manifest() -> str:
    managed_files = [name for name in GENERATED_WEBSITE_FILES if name != GENERATED_WEBSITE_MANIFEST]
    return json.dumps(managed_files, indent=2) + "\n"



def write_site_files(
    site_output_dir: Optional[str],
    start_url: str,
    history: list[dict[str, Any]],
    monitored_urls: Optional[list[str]] = None,
    manual_refresh_url: Optional[str] = None,
) -> None:
    if not site_output_dir:
        return

    output_dir = Path(site_output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    website_dir = output_dir / WEBSITE_DIRNAME
    if website_dir.is_symlink():
        website_dir.unlink()
    website_dir.mkdir(parents=True, exist_ok=True)
    for generated_name in GENERATED_WEBSITE_FILES:
        generated_path = website_dir / generated_name
        if generated_path.is_symlink() or generated_path.is_file():
            generated_path.unlink()
        elif generated_path.is_dir():
            shutil.rmtree(generated_path)
    (output_dir / "index.html").write_text(_generate_site_redirect_html(), encoding="utf-8")
    (website_dir / "index.html").write_text(
        generate_site_html(
            start_url=start_url,
            monitored_urls=monitored_urls,
            history=history,
            manual_refresh_url=manual_refresh_url,
        ),
        encoding="utf-8",
    )
    (website_dir / "styles.css").write_text(WEBSITE_STYLESHEET, encoding="utf-8")
    (website_dir / "app.js").write_text(WEBSITE_SCRIPT, encoding="utf-8")
    (website_dir / "history.json").write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    (website_dir / "history.csv").write_text(_build_history_csv(history), encoding="utf-8")
    (website_dir / GENERATED_WEBSITE_MANIFEST).write_text(_build_generated_website_manifest(), encoding="utf-8")



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
    monitored_urls: Optional[list[str]] = None,
    history_path: Optional[str] = None,
    site_output_dir: Optional[str] = None,
    email_settings: Optional[EmailSettings] = None,
    history_limit: int = 100,
    structure_confirmation_runs: int = DEFAULT_STRUCTURE_CONFIRMATION_RUNS,
) -> MonitorResult:
    with _with_state_lock(state_path):
        previous_state = _read_state(state_path) or {}
        previous_digest = previous_state.get("digest")
        previous_page_digests = previous_state.get("page_digests") or {}
        diagnostics: list[str] = []

        normalized_start_url = _normalize_url(start_url)
        configured_monitored_urls = get_monitored_urls(start_url=start_url, raw_urls=monitored_urls)
        inventory_urls = set(configured_monitored_urls or [normalized_start_url])
        pending_added: dict[str, int] = {}
        pending_removed: dict[str, int] = {}
        diagnostics.append(f"Monitoring configured pages only ({len(inventory_urls)} URLs).")

        inventory_contents, inventory_failures = fetch_inventory_pages(inventory_urls=inventory_urls)
        if inventory_failures:
            diagnostics.append(
                f"Inventory fetch failures: {', '.join(sorted(inventory_failures)[:10])}"
            )

        successful_page_digests = build_page_digests(inventory_contents)
        effective_page_digests = dict(previous_page_digests)
        effective_page_digests.update(successful_page_digests)
        for removed_url in set(previous_page_digests) - inventory_urls:
            effective_page_digests.pop(removed_url, None)
        for stale_url in set(effective_page_digests) - inventory_urls:
            effective_page_digests.pop(stale_url, None)

        comparable_urls = {url for url in inventory_urls if url in previous_page_digests}
        comparable_current_digests = {
            url: effective_page_digests[url]
            for url in sorted(comparable_urls)
            if url in effective_page_digests
        }
        comparable_previous_digests = {
            url: previous_page_digests[url]
            for url in sorted(comparable_urls)
            if url in previous_page_digests
        }
        current_digest = calculate_digest_from_page_digests(comparable_current_digests)
        previous_comparable_digest = calculate_digest_from_page_digests(comparable_previous_digests)
        page_changes = [
            PageChange(url=url, change_type="updated")
            for url in sorted(successful_page_digests)
            if previous_page_digests.get(url) and previous_page_digests[url] != successful_page_digests[url]
        ]
        changed = previous_digest is not None and previous_comparable_digest != current_digest and bool(page_changes)

        _write_state(
            state_path,
            current_digest,
            effective_page_digests,
            canonical_urls=inventory_urls,
            pending_added=pending_added,
            pending_removed=pending_removed,
        )

        result = MonitorResult(
            checked_at=datetime.now(timezone.utc).isoformat(),
            changed=changed,
            current_digest=current_digest,
            previous_digest=previous_comparable_digest if previous_digest is not None else None,
            page_count=len(inventory_contents),
            page_changes=page_changes,
            diagnostics=diagnostics,
        )
        history = _append_history(history_path, result, limit=history_limit)

    write_site_files(
        site_output_dir,
        start_url=start_url,
        monitored_urls=configured_monitored_urls,
        history=history,
        manual_refresh_url=get_manual_refresh_workflow_url(get_github_repository()),
    )

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
        monitored_urls: Optional[list[str]] = None,
        history_path: Optional[str] = None,
        site_output_dir: Optional[str] = None,
        email_settings: Optional[EmailSettings] = None,
    ):
        self.start_url = start_url
        self.state_path = state_path
        self.webhook_url = webhook_url
        self.interval_seconds = interval_seconds
        self.monitored_urls = get_monitored_urls(start_url=start_url, raw_urls=monitored_urls)
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
                monitored_urls=self.monitored_urls,
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
    start_url = os.getenv("START_URL", DEFAULT_SITE_URL)
    result = run_monitor_check(
        start_url=start_url,
        state_path=os.getenv("STATE_PATH", "site_data/monitor_state.json"),
        webhook_url=os.getenv("NOTIFICATION_WEBHOOK_URL", ""),
        max_pages=int(os.getenv("MAX_PAGES", "200")),
        monitored_urls=get_monitored_urls_from_env(start_url=start_url),
        history_path=os.getenv("HISTORY_PATH", "site_data/history.json"),
        site_output_dir=os.getenv("SITE_OUTPUT_DIR", "site"),
        email_settings=EmailSettings.from_env(),
        structure_confirmation_runs=int(
            os.getenv("STRUCTURE_CONFIRMATION_RUNS", str(DEFAULT_STRUCTURE_CONFIRMATION_RUNS))
        ),
    )
    print(json.dumps(result.to_dict(), indent=2))
