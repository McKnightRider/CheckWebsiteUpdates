import csv
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
import requests

from monitor import (
    CrawlResult,
    EmailSettings,
    MonitorResult,
    MonitorService,
    PageChange,
    build_page_digests,
    calculate_digest,
    get_github_repository,
    get_manual_refresh_workflow_url,
    run_monitor_check,
    send_email_notification,
    send_notification,
    write_site_files,
)


class MonitorTests(unittest.TestCase):
    def test_digest_is_stable_for_same_inputs(self):
        content = {
            "https://example.com": "Hello",
            "https://example.com/a": "World",
        }
        self.assertEqual(calculate_digest(content), calculate_digest(dict(reversed(content.items()))))

    def test_build_page_digests_and_detected_changes_are_stable(self):
        previous = build_page_digests(
            {
                "https://example.com": "A",
                "https://example.com/removed": "Gone",
            }
        )
        current = build_page_digests(
            {
                "https://example.com": "B",
                "https://example.com/new": "New",
            }
        )

        changes = monitor.detect_page_changes(previous, current)

        self.assertEqual(
            changes,
            [
                PageChange(url="https://example.com", change_type="updated"),
                PageChange(url="https://example.com/new", change_type="added"),
                PageChange(url="https://example.com/removed", change_type="removed"),
            ],
        )

    @patch("monitor.send_email_notification")
    @patch("monitor.send_notification")
    @patch("monitor.write_site_files")
    @patch("monitor._append_history")
    @patch("monitor.fetch_inventory_pages")
    @patch("monitor.crawl_site")
    def test_change_detection_triggers_notifications_and_tracks_pages(
        self,
        crawl_site_mock,
        fetch_inventory_pages_mock,
        append_history_mock,
        write_site_files_mock,
        send_notification_mock,
        send_email_notification_mock,
    ):
        crawl_site_mock.side_effect = [
            CrawlResult(
                content_by_url={"https://example.com": "A"},
                discovered_urls={"https://example.com"},
                fetch_failures={},
            ),
            CrawlResult(
                content_by_url={"https://example.com": "B", "https://example.com/new": "C"},
                discovered_urls={"https://example.com", "https://example.com/new"},
                fetch_failures={},
            ),
        ]
        fetch_inventory_pages_mock.side_effect = [
            ({"https://example.com": "A"}, {}),
            ({"https://example.com": "B", "https://example.com/new": "C"}, {}),
        ]
        append_history_mock.side_effect = lambda history_path, result, limit=100: [result.to_dict()]

        first_digest = calculate_digest({"https://example.com": "A"})
        first_page_digests = build_page_digests({"https://example.com": "A"})

        with patch("monitor._write_state") as write_state_mock, patch("monitor._read_state") as read_state_mock:
            read_state_mock.side_effect = [
                None,
                {"digest": first_digest, "page_digests": first_page_digests},
            ]

            first = run_monitor_check(
                start_url="https://example.com",
                state_path="/tmp/state.json",
                webhook_url="https://hooks.example.com",
                history_path="/tmp/history.json",
                site_output_dir="/tmp/site",
                email_settings=EmailSettings(),
            )
            second = run_monitor_check(
                start_url="https://example.com",
                state_path="/tmp/state.json",
                webhook_url="https://hooks.example.com",
                history_path="/tmp/history.json",
                site_output_dir="/tmp/site",
                email_settings=EmailSettings(),
            )

        self.assertFalse(first.changed)
        self.assertTrue(second.changed)
        self.assertEqual(write_state_mock.call_count, 2)
        send_notification_mock.assert_called_once()
        send_email_notification_mock.assert_called_once()
        write_site_files_mock.assert_called()
        self.assertEqual(
            second.page_changes,
            [
                PageChange(url="https://example.com", change_type="updated"),
            ],
        )

    @patch("monitor.requests.post")
    def test_send_notification_posts_expected_payload(self, post_mock):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
            page_changes=[PageChange(url="https://example.com/a", change_type="updated")],
        )

        send_notification("https://hooks.example.com", result)

        post_mock.assert_called_once()
        _, kwargs = post_mock.call_args
        self.assertEqual(kwargs["timeout"], 20)
        self.assertEqual(kwargs["json"]["checked_at"], result.checked_at)
        self.assertIn("website change detected", kwargs["json"]["text"])
        self.assertIn("Changed pages: updated: https://example.com/a", kwargs["json"]["text"])

    @patch("monitor.requests.post")
    def test_send_notification_raises_for_webhook_error(self, post_mock):
        post_mock.return_value.raise_for_status.side_effect = requests.HTTPError("boom")
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
            page_changes=[PageChange(url="https://example.com/a", change_type="updated")],
        )

        with self.assertRaises(requests.HTTPError):
            send_notification("https://hooks.example.com", result)

    @patch("monitor.requests.post")
    def test_send_notification_skips_when_webhook_missing(self, post_mock):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
            page_changes=[PageChange(url="https://example.com/a", change_type="updated")],
        )
        send_notification("", result)
        post_mock.assert_not_called()

    @patch("monitor.smtplib.SMTP")
    def test_send_email_notification_includes_changed_pages(self, smtp_mock):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
            page_changes=[PageChange(url="https://example.com/a", change_type="updated")],
        )
        settings = EmailSettings(
            "smtp.example.com",
            587,
            "user",
            "secret",
            "alerts@example.com",
            "mcknightrider@hotmail.com",
            True,
        )

        send_email_notification(settings, result)

        smtp_mock.assert_called_once_with("smtp.example.com", 587, timeout=20)
        smtp = smtp_mock.return_value.__enter__.return_value
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("user", "secret")
        smtp.send_message.assert_called_once()
        sent_message = smtp.send_message.call_args.args[0]
        self.assertIn("https://example.com/a", sent_message.get_content())
        self.assertEqual(sent_message["To"], "mcknightrider@hotmail.com")

    @patch("monitor.smtplib.SMTP")
    def test_send_email_notification_skips_when_incomplete(self, smtp_mock):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
            page_changes=[PageChange(url="https://example.com/a", change_type="updated")],
        )

        send_email_notification(EmailSettings(), result)

        smtp_mock.assert_not_called()

    @patch("monitor.smtplib.SMTP")
    def test_send_email_notification_skips_when_recipient_missing(self, smtp_mock):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
            page_changes=[PageChange(url="https://example.com/a", change_type="updated")],
        )
        settings = EmailSettings("smtp.example.com", 587, "", "", "alerts@example.com", "", True)

        send_email_notification(settings, result)

        smtp_mock.assert_not_called()

    def test_write_site_files_outputs_html_and_history(self):
        history = [
            {
                "checked_at": "2026-01-01T00:00:00+00:00",
                "changed": True,
                "current_digest": "new",
                "previous_digest": "old",
                "page_count": 2,
                "page_changes": [{"url": "https://example.com/a", "change_type": "updated"}],
            },
            {
                "checked_at": "2026-01-02T00:00:00+00:00",
                "changed": False,
                "current_digest": "same",
                "previous_digest": "same",
                "page_count": 2,
                "page_changes": [],
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "site"
            write_site_files(
                str(output_dir),
                "https://example.com",
                history,
                manual_refresh_url="https://github.com/octo/repo/actions/workflows/monitor-pages.yml",
            )

            redirect_html = (output_dir / "index.html").read_text(encoding="utf-8")
            website_dir = output_dir / "website"
            index_html = (website_dir / "index.html").read_text(encoding="utf-8")
            history_json = json.loads((website_dir / "history.json").read_text(encoding="utf-8"))
            history_csv = list(csv.DictReader((website_dir / "history.csv").read_text(encoding="utf-8").splitlines()))
            stylesheet = (website_dir / "styles.css").read_text(encoding="utf-8")
            script = (website_dir / "app.js").read_text(encoding="utf-8")
            asset_manifest = json.loads((website_dir / "asset-manifest.json").read_text(encoding="utf-8"))

        self.assertIn('href="website/"', redirect_html)
        self.assertNotIn("http-equiv", redirect_html)
        self.assertIn("Latest check", index_html)
        self.assertIn('href="history.csv"', index_html)
        self.assertIn('src="app.js"', index_html)
        self.assertIn('id="refresh-button"', index_html)
        self.assertIn("Open Run workflow", index_html)
        self.assertIn("Run workflow", index_html)
        self.assertIn("reload this page to see the latest site output.", index_html)
        self.assertIn('rel="noopener noreferrer"', index_html)
        self.assertNotIn('data-check-now-endpoint=', index_html)
        self.assertNotIn("Refresh PIN", index_html)
        self.assertNotIn('id="refresh-form"', index_html)
        self.assertIn('id="refresh-button"', index_html)
        self.assertIn("https://example.com/a", index_html)
        self.assertIn('href="https://example.com/a"', index_html)
        self.assertEqual(history_json, history)
        self.assertEqual(history_csv[0]["checked_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(history_csv[0]["checked_at_display"], "1 January 2026 at 12:00:00 AM GMT")
        self.assertEqual(history_csv[0]["changed"], "true")
        self.assertIn('"url":"https://example.com/a"', history_csv[0]["page_changes"])
        self.assertEqual(history_csv[1]["checked_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(history_csv[1]["checked_at_display"], "2 January 2026 at 12:00:00 AM GMT")
        self.assertEqual(history_csv[1]["changed"], "false")
        self.assertIn(".resource-list", stylesheet)
        self.assertIn(".refresh-link", stylesheet)
        self.assertIn("history-count", script)
        self.assertNotIn("refresh-form", script)
        self.assertNotIn("data-check-now-endpoint", script)
        self.assertNotIn("Enter your refresh PIN.", script)
        self.assertEqual(asset_manifest, ["index.html", "styles.css", "app.js", "history.json", "history.csv"])

    def test_write_site_files_labels_first_history_item(self):
        history = [
            {
                "checked_at": "2026-01-01T00:00:00+00:00",
                "changed": False,
                "current_digest": "same",
                "previous_digest": None,
                "page_count": 1,
                "page_changes": [],
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "site"
            write_site_files(str(output_dir), "https://example.com", history)
            index_html = (output_dir / "website" / "index.html").read_text(encoding="utf-8")

        self.assertIn("First Check: 1 January 2026 at 12:00:00 AM GMT", index_html)

    def test_write_site_files_uses_configured_manual_refresh_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "site"
            write_site_files(
                str(output_dir),
                "https://example.com",
                [],
                manual_refresh_url="https://github.com/octo/repo/actions/workflows/monitor-pages.yml",
            )
            index_html = (output_dir / "website" / "index.html").read_text(encoding="utf-8")

        self.assertIn(
            'href="https://github.com/octo/repo/actions/workflows/monitor-pages.yml"',
            index_html,
        )

    def test_write_site_files_without_manual_refresh_url_renders_actions_instructions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "site"
            write_site_files(str(output_dir), "https://example.com", [])
            index_html = (output_dir / "website" / "index.html").read_text(encoding="utf-8")

        self.assertIn("Open the repository Actions tab", index_html)

    def test_get_github_repository_reads_origin_remote_when_env_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            git_dir = repo_dir / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text(
                '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = git@github.com:octo/repo.git\n',
                encoding="utf-8",
            )
            with patch.object(monitor, "__file__", str(repo_dir / "monitor.py")), patch.dict(
                os.environ, {"GITHUB_REPOSITORY": "invalid"}
            ):
                repository = get_github_repository()

        self.assertEqual(repository, "octo/repo")

    def test_get_manual_refresh_workflow_url_returns_none_for_invalid_repository(self):
        self.assertIsNone(get_manual_refresh_workflow_url("invalid"))

    def test_get_manual_refresh_workflow_url_accepts_trailing_slash(self):
        self.assertEqual(
            get_manual_refresh_workflow_url("octo/repo/"),
            "https://github.com/octo/repo/actions/workflows/monitor-pages.yml",
        )

    def test_write_site_files_does_not_link_unsafe_urls(self):
        history = [
            {
                "checked_at": "2026-01-01T00:00:00+00:00",
                "changed": True,
                "current_digest": "new",
                "previous_digest": "old",
                "page_count": 1,
                "page_changes": [{"url": "javascript:alert(1)", "change_type": "updated"}],
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "site"
            write_site_files(str(output_dir), "https://example.com", history)
            index_html = (output_dir / "website" / "index.html").read_text(encoding="utf-8")

        self.assertIn("javascript:alert(1)", index_html)
        self.assertNotIn('href="javascript:alert(1)"', index_html)

    def test_write_site_files_preserves_unmanaged_assets(self):
        history = [
            {
                "checked_at": "2026-01-01T00:00:00+00:00",
                "changed": False,
                "current_digest": "same",
                "previous_digest": "same",
                "page_count": 1,
                "page_changes": [],
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "site"
            stale_file = output_dir / "website" / "stale.txt"
            stale_file.parent.mkdir(parents=True, exist_ok=True)
            stale_file.write_text("old", encoding="utf-8")

            write_site_files(str(output_dir), "https://example.com", history)

            self.assertTrue(stale_file.exists())

    def test_write_site_files_replaces_symlinked_website_dir(self):
        history = [
            {
                "checked_at": "2026-01-01T00:00:00+00:00",
                "changed": False,
                "current_digest": "same",
                "previous_digest": "same",
                "page_count": 1,
                "page_changes": [],
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "site"
            external_dir = Path(tmpdir) / "external"
            external_dir.mkdir(parents=True, exist_ok=True)
            protected_file = external_dir / "protected.txt"
            protected_file.write_text("keep", encoding="utf-8")

            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "website").symlink_to(external_dir, target_is_directory=True)

            write_site_files(str(output_dir), "https://example.com", history)

            self.assertTrue(protected_file.exists())
            self.assertTrue((output_dir / "website" / "index.html").exists())

    def test_normalize_url_canonicalizes_scheme_for_same_host(self):
        normalized = monitor._normalize_url(
            "http://www.example.com/path/",
            canonical_host="www.example.com",
            canonical_scheme="https",
        )
        self.assertEqual(normalized, "https://www.example.com/path")
        normalized_with_port = monitor._normalize_url(
            "https://www.example.com:443/path/",
            canonical_host="www.example.com",
            canonical_scheme="https",
        )
        self.assertEqual(normalized_with_port, "https://www.example.com/path")
        normalized_with_non_default_port = monitor._normalize_url(
            "https://www.example.com:8443/path/",
            canonical_host="www.example.com",
            canonical_scheme="https",
            canonical_port=8443,
        )
        self.assertEqual(normalized_with_non_default_port, "https://www.example.com:8443/path")
        normalized_with_explicit_default_port_and_canonical_port = monitor._normalize_url(
            "https://www.example.com:443/path/",
            canonical_host="www.example.com",
            canonical_scheme="https",
            canonical_port=443,
        )
        self.assertEqual(
            normalized_with_explicit_default_port_and_canonical_port,
            "https://www.example.com:443/path",
        )
        normalized_with_userinfo = monitor._normalize_url(
            "https://user@www.example.com:8443/path/",
            canonical_host="www.example.com",
            canonical_scheme="https",
            canonical_port=8443,
        )
        self.assertEqual(normalized_with_userinfo, "https://user@www.example.com:8443/path")
        normalized_preserves_explicit_port_when_scheme_changes = monitor._normalize_url(
            "http://www.example.com:443/path/",
            canonical_host="www.example.com",
            canonical_scheme="https",
        )
        self.assertEqual(normalized_preserves_explicit_port_when_scheme_changes, "https://www.example.com:443/path")

    def test_extract_links_stays_within_same_origin_port(self):
        html = """
        <a href="https://www.example.com/path-a">A</a>
        <a href="https://www.example.com:8443/path-b">B</a>
        """
        links = monitor._extract_links(
            html=html,
            page_url="https://www.example.com",
            allowed_host="www.example.com",
            allowed_port=443,
            canonical_scheme="https",
        )
        self.assertEqual(links, {"https://www.example.com/path-a"})

    def test_normalize_url_strips_tracking_query_params(self):
        normalized = monitor._normalize_url(
            "https://www.example.com/path?utm_source=a&z=1&fbclid=abc&y=2",
            canonical_host="www.example.com",
            canonical_scheme="https",
        )
        self.assertEqual(normalized, "https://www.example.com/path?y=2&z=1")

    def test_extract_links_ignores_noisy_paths(self):
        html = """
        <a href="https://www.example.com/category/news">Category</a>
        <a href="https://www.example.com/committee/americas/page/2">Pagination</a>
        <a href="https://www.example.com/about">About</a>
        """
        links = monitor._extract_links(
            html=html,
            page_url="https://www.example.com",
            allowed_host="www.example.com",
            allowed_port=443,
            canonical_scheme="https",
        )
        self.assertEqual(links, {"https://www.example.com/about"})

    @patch("monitor.send_email_notification")
    @patch("monitor.send_notification")
    @patch("monitor.write_site_files")
    @patch("monitor._append_history")
    @patch("monitor.fetch_inventory_pages")
    @patch("monitor.crawl_site")
    def test_structural_url_changes_do_not_alert_until_content_changes(
        self,
        crawl_site_mock,
        fetch_inventory_pages_mock,
        append_history_mock,
        write_site_files_mock,
        send_notification_mock,
        send_email_notification_mock,
    ):
        crawl_site_mock.side_effect = [
            CrawlResult({"https://example.com": "A"}, {"https://example.com"}, {}),
            CrawlResult({"https://example.com": "A", "https://example.com/new": "N"}, {"https://example.com", "https://example.com/new"}, {}),
            CrawlResult({"https://example.com": "A", "https://example.com/new": "N"}, {"https://example.com", "https://example.com/new"}, {}),
        ]
        fetch_inventory_pages_mock.side_effect = [
            ({"https://example.com": "A"}, {}),
            ({"https://example.com": "A"}, {}),
            ({"https://example.com": "A", "https://example.com/new": "N"}, {}),
        ]
        append_history_mock.side_effect = lambda history_path, result, limit=100: [result.to_dict()]

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = str(Path(tmpdir) / "state.json")
            history_path = str(Path(tmpdir) / "history.json")
            first = run_monitor_check(
                start_url="https://example.com",
                state_path=state_path,
                webhook_url="https://hooks.example.com",
                history_path=history_path,
                site_output_dir="/tmp/site",
                email_settings=EmailSettings(),
                structure_confirmation_runs=2,
            )
            second = run_monitor_check(
                start_url="https://example.com",
                state_path=state_path,
                webhook_url="https://hooks.example.com",
                history_path=history_path,
                site_output_dir="/tmp/site",
                email_settings=EmailSettings(),
                structure_confirmation_runs=2,
            )
            third = run_monitor_check(
                start_url="https://example.com",
                state_path=state_path,
                webhook_url="https://hooks.example.com",
                history_path=history_path,
                site_output_dir="/tmp/site",
                email_settings=EmailSettings(),
                structure_confirmation_runs=2,
            )

        self.assertFalse(first.changed)
        self.assertFalse(second.changed)
        self.assertFalse(third.changed)
        send_notification_mock.assert_not_called()
        send_email_notification_mock.assert_not_called()
        write_site_files_mock.assert_called()

    def test_format_timestamp_uses_day_month_year_and_uk_timezone(self):
        self.assertEqual(
            monitor._format_timestamp("2026-09-25T12:48:20+00:00"),
            "25 September 2026 at 1:48:20 PM BST",
        )
        self.assertEqual(
            monitor._format_timestamp("2026-01-09T12:48:20+00:00"),
            "9 January 2026 at 12:48:20 PM GMT",
        )
        with patch("monitor.ZoneInfo", side_effect=monitor.ZoneInfoNotFoundError("missing tzdata")):
            self.assertEqual(
                monitor._format_timestamp("2026-01-09T12:48:20+00:00"),
                "9 January 2026 at 12:48:20 PM GMT",
            )
        self.assertEqual(
            monitor._format_timestamp("2026-01-09T12:48:20Z"),
            "9 January 2026 at 12:48:20 PM GMT",
        )
        self.assertEqual(
            monitor._format_timestamp("2026-01-09T12:48:20"),
            "9 January 2026 at 12:48:20 PM GMT",
        )

    @patch("monitor.run_monitor_check")
    def test_perform_check_serializes_concurrent_calls(self, run_check_mock):
        service = MonitorService(
            start_url="https://example.com",
            state_path="/tmp/state.json",
            webhook_url="",
        )
        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()

        def fake_run(*args, **kwargs):
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.05)
            with state_lock:
                state["active"] -= 1
            return MonitorResult(
                checked_at="2026-01-01T00:00:00+00:00",
                changed=False,
                current_digest="x",
                previous_digest="x",
                page_count=1,
            )

        run_check_mock.side_effect = fake_run

        t1 = threading.Thread(target=service.perform_check)
        t2 = threading.Thread(target=service.perform_check)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(run_check_mock.call_count, 2)
        self.assertEqual(state["max_active"], 1)

    @patch("monitor.run_monitor_check")
    def test_service_start_and_stop_manage_leader_lock(self, run_check_mock):
        run_check_mock.return_value = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=False,
            current_digest="x",
            previous_digest="x",
            page_count=1,
        )
        service = MonitorService(
            start_url="https://example.com",
            state_path="/tmp/test-service-state.json",
            webhook_url="",
            interval_seconds=3600,
        )

        service.start()
        time.sleep(0.02)
        self.assertIsNotNone(service._leader_lock_file)
        service.stop()
        self.assertIsNone(service._leader_lock_file)

    @patch("monitor.run_monitor_check")
    def test_service_start_skips_when_leader_lock_is_owned(self, run_check_mock):
        if monitor.fcntl is None:
            self.skipTest("fcntl not available on this platform")

        service = MonitorService(
            start_url="https://example.com",
            state_path="/tmp/test-service-state-2.json",
            webhook_url="",
        )
        with patch("monitor.fcntl.flock", side_effect=BlockingIOError):
            service.start()

        self.assertIsNone(service._thread)
        self.assertIsNone(service._leader_lock_file)
        run_check_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
