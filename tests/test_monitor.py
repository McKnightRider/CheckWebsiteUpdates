import threading
import time
import unittest
from unittest.mock import patch

import monitor
import requests

from monitor import MonitorResult, MonitorService, calculate_digest, run_monitor_check, send_notification


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

    @patch("monitor.requests.post")
    def test_send_notification_skips_when_webhook_missing(self, post_mock):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=True,
            current_digest="new",
            previous_digest="old",
            page_count=3,
        )
        send_notification("", result)
        post_mock.assert_not_called()

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
