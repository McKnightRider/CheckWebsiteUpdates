import unittest
from unittest.mock import patch

import requests

from monitor import MonitorResult, calculate_digest, run_monitor_check, send_notification


class MonitorTests(unittest.TestCase):
    def test_digest_is_stable_for_same_inputs(self):
        content = {
            "https://example.com": "Hello",
            "https://example.com/a": "World",
        }
        self.assertEqual(calculate_digest(content), calculate_digest(dict(reversed(content.items()))))

    @patch("monitor.send_notification")
    @patch("monitor.crawl_site")
    def test_change_detection_triggers_notification(self, crawl_site_mock, send_notification_mock):
        crawl_site_mock.side_effect = [
            {"https://example.com": "A"},
            {"https://example.com": "B"},
        ]

        with patch("monitor._write_state") as write_state_mock, patch(
            "monitor._read_previous_digest"
        ) as read_previous_mock:
            first_digest = calculate_digest({"https://example.com": "A"})
            read_previous_mock.side_effect = [None, first_digest]

            first = run_monitor_check(
                start_url="https://example.com",
                state_path="/tmp/state.json",
                webhook_url="https://hooks.example.com",
            )
            second = run_monitor_check(
                start_url="https://example.com",
                state_path="/tmp/state.json",
                webhook_url="https://hooks.example.com",
            )

        self.assertFalse(first.changed)
        self.assertTrue(second.changed)
        self.assertEqual(write_state_mock.call_count, 2)
        send_notification_mock.assert_called_once()

    @patch("monitor.requests.post")
    def test_send_notification_posts_expected_payload(self, post_mock):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
        )

        send_notification("https://hooks.example.com", result)

        post_mock.assert_called_once()
        _, kwargs = post_mock.call_args
        self.assertEqual(kwargs["timeout"], 20)
        self.assertEqual(kwargs["json"]["checked_at"], result.checked_at)
        self.assertIn("website change detected", kwargs["json"]["text"])
        self.assertIn("Previous digest: old", kwargs["json"]["text"])
        self.assertIn("Current digest: new", kwargs["json"]["text"])

    @patch("monitor.requests.post")
    def test_send_notification_raises_for_webhook_error(self, post_mock):
        post_mock.return_value.raise_for_status.side_effect = requests.HTTPError("boom")
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
        )

        with self.assertRaises(requests.HTTPError):
            send_notification("https://hooks.example.com", result)


if __name__ == "__main__":
    unittest.main()
