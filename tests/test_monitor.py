import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
import requests

from monitor import (
    EmailSettings,
    MonitorResult,
    MonitorService,
    PageChange,
    build_page_digests,
    calculate_digest,
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
    @patch("monitor.crawl_site")
    def test_change_detection_triggers_notifications_and_tracks_pages(
        self,
        crawl_site_mock,
        append_history_mock,
        write_site_files_mock,
        send_notification_mock,
        send_email_notification_mock,
    ):
        crawl_site_mock.side_effect = [
            {"https://example.com": "A"},
            {"https://example.com": "B", "https://example.com/new": "C"},
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
                PageChange(url="https://example.com/new", change_type="added"),
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
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "site"
            write_site_files(str(output_dir), "https://example.com", history)

            index_html = (output_dir / "index.html").read_text(encoding="utf-8")
            history_json = json.loads((output_dir / "history.json").read_text(encoding="utf-8"))

        self.assertIn("Latest check", index_html)
        self.assertIn("https://example.com/a", index_html)
        self.assertIn('href="https://example.com/a"', index_html)
        self.assertEqual(history_json, history)

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
            index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        self.assertIn("javascript:alert(1)", index_html)
        self.assertNotIn('href="javascript:alert(1)"', index_html)

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
        normalized_with_userinfo = monitor._normalize_url(
            "https://user@www.example.com:8443/path/",
            canonical_host="www.example.com",
            canonical_scheme="https",
            canonical_port=8443,
        )
        self.assertEqual(normalized_with_userinfo, "https://user@www.example.com:8443/path")

    def test_format_timestamp_uses_day_month_year_and_uk_timezone(self):
        self.assertEqual(
            monitor._format_timestamp("2026-09-25T12:48:20+00:00"),
            "25 September 2026 at 1:48:20 PM BST",
        )
        self.assertEqual(
            monitor._format_timestamp("2026-01-09T12:48:20+00:00"),
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
