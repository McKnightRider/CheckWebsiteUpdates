import unittest
from unittest.mock import patch

from monitor import calculate_digest, run_monitor_check


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


if __name__ == "__main__":
    unittest.main()
