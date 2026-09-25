import unittest
from unittest.mock import patch

import app as app_module
from monitor import MonitorResult


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_check_now_options_returns_cors_headers(self):
        with patch.object(app_module, "ensure_service_started", return_value=None), patch.object(
            app_module, "CHECK_NOW_ALLOWED_ORIGINS", ("https://pages.example.com",)
        ):
            response = self.client.open(
                "/check-now",
                method="OPTIONS",
                headers={"Origin": "https://pages.example.com"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://pages.example.com")
        self.assertEqual(response.headers.get("Access-Control-Allow-Headers"), "X-Check-Token")

    def test_check_now_runs_monitor_and_returns_cors_headers(self):
        result = MonitorResult(
            checked_at="2026-01-01T00:00:00+00:00",
            changed=False,
            current_digest="same",
            previous_digest="same",
            page_count=1,
        )

        with patch.object(app_module, "ensure_service_started", return_value=None), patch.object(
            app_module, "CHECK_NOW_ALLOWED_ORIGINS", ("https://pages.example.com",)
        ), patch.object(app_module, "CHECK_NOW_TOKEN", "secret"), patch.object(
            app_module.service, "perform_check", return_value=result
        ) as perform_check_mock:
            response = self.client.post(
                "/check-now",
                headers={
                    "Origin": "https://pages.example.com",
                    "X-Check-Token": "secret",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["result"]["checked_at"], result.checked_at)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://pages.example.com")
        perform_check_mock.assert_called_once()

    def test_check_now_rejects_invalid_token(self):
        with patch.object(app_module, "ensure_service_started", return_value=None), patch.object(
            app_module, "CHECK_NOW_ALLOWED_ORIGINS", ("*",)
        ), patch.object(app_module, "CHECK_NOW_TOKEN", "secret"), patch.object(
            app_module.service, "perform_check"
        ) as perform_check_mock:
            response = self.client.post(
                "/check-now",
                headers={
                    "Origin": "https://pages.example.com",
                    "X-Check-Token": "wrong",
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "Forbidden")
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
        perform_check_mock.assert_not_called()
