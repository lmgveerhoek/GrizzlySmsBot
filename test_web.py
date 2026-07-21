import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from test_bot import config
from web import create_app, run_web_ui


class WebUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.config = replace(
            config(str(Path(self.directory.name) / "state.db")),
            web_ui=True,
            ui_password="correct horse battery staple",
        )
        self.controller = Mock()
        self.controller.status.return_value = {
            "phase": "idle",
            "phoneNumber": None,
            "phoneNumberCopy": None,
            "phoneNumberNational": None,
            "activationId": None,
            "smsMessage": None,
            "elapsedSeconds": 0,
            "timeoutRemainingSeconds": 0,
            "workerActive": False,
            "canPurchase": True,
            "canCancel": False,
            "canRetry": False,
            "lastError": None,
            "events": [],
        }
        self.app = create_app(
            self.config, controller=self.controller, start_controller=False
        )
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def login(self) -> str:
        response = self.client.post(
            "/login",
            data={"password": "correct horse battery staple"},
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as user_session:
            return user_session["csrf_token"]

    def test_requires_authentication(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_rejects_incorrect_password(self) -> None:
        response = self.client.post("/login", data={"password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Incorrect password", response.data)

    def test_login_opens_dashboard(self) -> None:
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Get another number", response.data)
        self.assertIn(b"Copy with country code", response.data)
        self.assertIn(b"Received SMS", response.data)
        self.assertIn(b"theme-toggle", response.data)

    def test_status_returns_sanitized_controller_data(self) -> None:
        self.login()
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["phase"], "idle")

    def test_purchase_requires_csrf_and_confirmation(self) -> None:
        csrf = self.login()
        no_csrf = self.client.post("/api/actions/purchase", json={"confirm": True})
        self.assertEqual(no_csrf.status_code, 403)
        no_confirm = self.client.post(
            "/api/actions/purchase",
            json={},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(no_confirm.status_code, 400)

    def test_purchase_starts_controller_action(self) -> None:
        csrf = self.login()
        self.controller.start_purchase.return_value = True, "Looking for a number"
        response = self.client.post(
            "/api/actions/purchase",
            json={"confirm": True},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.controller.start_purchase.assert_called_once_with()

    def test_invalid_action_state_returns_conflict(self) -> None:
        csrf = self.login()
        self.controller.cancel_active_activation.return_value = (
            False,
            "There is no active activation to cancel",
        )
        response = self.client.post(
            "/api/actions/cancel",
            json={"confirm": True},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 409)

    def test_web_server_uses_single_thread_and_quiet_access_logs(self) -> None:
        app = Mock()
        controller = Mock()
        with patch("web.ActivationController", return_value=controller), patch(
            "web.create_app", return_value=app
        ):
            self.assertEqual(run_web_ui(self.config), 0)

        app.run.assert_called_once_with(
            host=self.config.web_ui_host,
            port=self.config.web_ui_port,
            threaded=False,
        )
        controller.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
