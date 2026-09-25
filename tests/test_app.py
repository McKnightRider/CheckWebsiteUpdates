import unittest
from unittest.mock import patch

import app as app_module
from monitor import MonitorResult


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_index_returns_running_status_without_last_result(self):
        with patch.object(app_module, "ensure_service_started", return_value=None), patch.object(
            app_module.service, "last_result", None
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "running")
        self.assertIsNone(response.get_json()["last_check"])

    def test_index_returns_last_result(self):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=False,
            current_digest="same",
            previous_digest="same",
            page_count=1,
        )

        with patch.object(app_module, "ensure_service_started", return_value=None), patch.object(
            app_module.service, "last_result", result
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["last_check"]["checked_at"], result.checked_at)

    def test_check_now_route_is_not_available(self):
        with patch.object(app_module, "ensure_service_started", return_value=None):
            response = self.client.post("/check-now")

        self.assertEqual(response.status_code, 404)
