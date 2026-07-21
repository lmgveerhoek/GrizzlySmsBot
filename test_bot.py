import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

import bot


def response(text: str, ok: bool = True, status_code: int = 200) -> Mock:
    result = Mock()
    result.text = text
    result.ok = ok
    result.status_code = status_code
    result.headers = {}
    return result


def config(state_db_path: str, ntfy_url: Optional[str] = None) -> bot.Config:
    return bot.Config(
        api_key="key",
        service="wx",
        country="62",
        max_price="2",
        provider_ids=None,
        rate=1,
        timeout=1,
        status_every=100,
        discord_webhook_url="https://example.test/webhook",
        discord_max_retries=1,
        ntfy_url=ntfy_url,
        ntfy_max_retries=1,
        sms_poll_seconds=1,
        activation_timeout_seconds=900,
        state_db_path=state_db_path,
    )


class ParsingTests(unittest.TestCase):
    def test_parse_number_v2(self) -> None:
        self.assertEqual(
            bot.parse_number_v2(
                '{"activationId":123,"phoneNumber":"447700900123",'
                '"activationCost":0.4,"currency":978,"countryCode":"16"}'
            ),
            bot.AcquisitionDetails("123", "447700900123", "0.4", "978", "16"),
        )
        self.assertIsNone(bot.parse_number_v2("NO_NUMBERS"))

    def test_parse_code(self) -> None:
        self.assertEqual(bot.parse_code("STATUS_OK:123456"), "123456")
        self.assertIsNone(bot.parse_code("STATUS_WAIT_CODE"))

    def test_formats_international_and_national_phone_number(self) -> None:
        self.assertEqual(
            bot.phone_representations("905314393988"),
            ("+90 531 439 39 88", "5314393988"),
        )


class StateStoreTests(unittest.TestCase):
    def test_persists_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.StateStore(str(Path(directory) / "state.db"))
            activation = bot.Activation("123", "447700900123", 1.0, "waiting_for_sms")
            store.save(activation)
            self.assertEqual(store.load(), activation)

    def test_records_and_summarizes_activation_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.StateStore(str(Path(directory) / "state.db"))
            first = bot.Activation("123", "905314393988", 1.0, "acquired")
            second = bot.Activation("456", "31612345678", 2.0, "acquired")
            store.record_acquisition(
                first,
                bot.AcquisitionDetails("123", first.phone_number, "0.4", "978", "62"),
                "311,415",
            )
            store.save(
                bot.Activation("123", first.phone_number, 1.0, "completed", "654321")
            )
            store.record_acquisition(
                second,
                bot.AcquisitionDetails("456", second.phone_number, "1.2", "978", "48"),
                "311",
            )
            store.save(bot.Activation("456", second.phone_number, 2.0, "cancelled"))

            history = store.history()
            summary = store.history_summary()

            self.assertEqual([entry["activationId"] for entry in history], ["456", "123"])
            self.assertEqual(history[0]["providerFilter"], "311")
            self.assertTrue(history[1]["codeReceived"])
            self.assertEqual(summary["attempts"], 2)
            self.assertEqual(summary["codesReceived"], 1)
            self.assertEqual(summary["unsuccessful"], 1)
            self.assertEqual(summary["grossPurchaseValues"], {"978": "1.6"})
            store.clear()
            self.assertIsNone(store.load())

    def test_migrates_existing_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.db")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE activation (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        activation_id TEXT NOT NULL,
                        phone_number TEXT NOT NULL,
                        acquired_at REAL NOT NULL,
                        phase TEXT NOT NULL
                    )
                    """
                )
            store = bot.StateStore(path)
            activation = bot.Activation(
                "123", "447700900123", 1.0, "code_notification_pending", "123456"
            )
            store.save(activation)
            self.assertEqual(store.load(), activation)


class ConfigTests(unittest.TestCase):
    def test_sms_poll_interval_defaults_to_five_seconds(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GRIZZLY_API_KEY": "key",
                "SERVICE": "wx",
                "COUNTRY": "62",
                "MAX_PRICE": "2",
                "MAX_REQUESTS_PER_SECOND": "1",
                "REQUEST_TIMEOUT_SECONDS": "1",
                "DISCORD_WEBHOOK_URL": "https://example.test/webhook",
            },
            clear=True,
        ):
            parsed = bot.Config.from_env()
            self.assertEqual(parsed.sms_poll_seconds, 5)
            self.assertIsNone(parsed.ntfy_url)
            self.assertFalse(parsed.debug_logs)
            self.assertFalse(parsed.web_request_logs)

    def test_debug_logging_flags(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GRIZZLY_API_KEY": "key",
                "SERVICE": "wx",
                "COUNTRY": "62",
                "MAX_PRICE": "2",
                "MAX_REQUESTS_PER_SECOND": "1",
                "REQUEST_TIMEOUT_SECONDS": "1",
                "DISCORD_WEBHOOK_URL": "https://example.test/webhook",
                "DEBUG_LOGS": "true",
                "WEB_REQUEST_LOGS": "true",
            },
            clear=True,
        ):
            parsed = bot.Config.from_env()
            self.assertTrue(parsed.debug_logs)
            self.assertTrue(parsed.web_request_logs)

    def test_web_ui_requires_password(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GRIZZLY_API_KEY": "key",
                "SERVICE": "wx",
                "COUNTRY": "62",
                "MAX_PRICE": "2",
                "MAX_REQUESTS_PER_SECOND": "1",
                "REQUEST_TIMEOUT_SECONDS": "1",
                "DISCORD_WEBHOOK_URL": "https://example.test/webhook",
                "WEB_UI": "true",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "UI_PASSWORD"):
                bot.Config.from_env()


class DiscordNotifierTests(unittest.TestCase):
    def test_retries_rate_limit_without_mentions(self) -> None:
        limited = response("", ok=False, status_code=429)
        limited.headers = {"Retry-After": "0.1"}
        delivered = response("")
        with patch("bot.time.sleep"):
            notifier = bot.DiscordNotifier("https://example.test/webhook", 1, 2)
            notifier.session.post = Mock(side_effect=[limited, delivered])
            self.assertTrue(notifier.send("Title", "hello @everyone"))

        payload = notifier.session.post.call_args.args[0]
        self.assertEqual(payload, "https://example.test/webhook")
        self.assertEqual(
            notifier.session.post.call_args.kwargs["json"]["allowed_mentions"], {"parse": []}
        )


class NtfyNotifierTests(unittest.TestCase):
    def test_sends_original_ntfy_payload(self) -> None:
        notifier = bot.NtfyNotifier("https://ntfy.sh/topic", 1, 1)
        notifier.session.post = Mock(return_value=response(""))

        self.assertTrue(notifier.send("Title", "message", urgent=True))

        notifier.session.post.assert_called_once_with(
            "https://ntfy.sh/topic",
            data=b"message",
            headers={
                "Title": "Title",
                "Priority": "urgent",
                "Tags": "telephone_receiver",
            },
            timeout=1,
        )


class NotificationFanoutTests(unittest.TestCase):
    def test_configures_discord_without_ntfy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            self.assertEqual(len(watcher.notifiers), 1)
            watcher.close()

    def test_sends_to_both_providers_and_accepts_partial_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(
                config(str(Path(directory) / "state.db"), "https://ntfy.sh/topic")
            )
            discord = Mock()
            ntfy = Mock()
            discord.send.return_value = True
            ntfy.send.return_value = False
            watcher.notifiers = [discord, ntfy]

            self.assertTrue(watcher.send_notification("Title", "message", urgent=True))

            discord.send.assert_called_once_with("Title", "message", True)
            ntfy.send.assert_called_once_with("Title", "message", True)

    def test_fails_when_both_providers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(
                config(str(Path(directory) / "state.db"), "https://ntfy.sh/topic")
            )
            discord = Mock(send=Mock(return_value=False))
            ntfy = Mock(send=Mock(return_value=False))
            watcher.notifiers = [discord, ntfy]

            self.assertFalse(watcher.send_notification("Title", "message"))


class LifecycleTests(unittest.TestCase):
    def test_stops_on_missing_api_key_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            session = Mock()
            session.get.return_value = response("NO_KEY")

            with self.assertRaisesRegex(ValueError, "NO_KEY"):
                watcher.acquire(session)

    def test_acquires_v2_number_and_records_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            session = Mock()
            session.get.return_value = response(
                '{"activationId":123,"phoneNumber":"905314393988",'
                '"activationCost":0.55,"currency":840,"countryCode":"62"}'
            )

            activation = watcher.acquire(session)

            self.assertEqual(activation.activation_id, "123")
            self.assertEqual(session.get.call_args.kwargs["params"]["action"], "getNumberV2")
            history = watcher.store.history()
            self.assertEqual(history[0]["cost"], "0.55")
            self.assertEqual(history[0]["currency"], "840")
            self.assertEqual(history[0]["providerFilter"], None)

    def test_delivers_code_then_completes_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            watcher.notifiers = [Mock()]
            watcher.notifiers[0].send.return_value = True
            session = Mock()
            session.get.side_effect = [
                response("ACCESS_READY"),
                response("STATUS_OK:123456"),
                response("ACCESS_ACTIVATION"),
            ]
            activation = bot.Activation(
                "123", "447700900123", time.time(), "acquired"
            )

            watcher.wait_for_code(session, activation)

            self.assertTrue(watcher.stop.requested)
            self.assertEqual(watcher.store.load().phase, "completed")
            self.assertEqual(watcher.store.load().sms_code, "123456")
            watcher.notifiers[0].send.assert_called_once_with(
                "GRIZZLY SMS CODE RECEIVED",
                "Code: 123456\nActivation: 123",
                True,
            )

    def test_keeps_acquired_activation_when_number_notification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            activation = bot.Activation("123", "447700900123", time.time(), "acquired")
            watcher.store.save(activation)
            watcher.notifiers = [Mock()]
            watcher.notifiers[0].send.return_value = False
            session = Mock()
            session.__enter__ = Mock(return_value=session)
            session.__exit__ = Mock(return_value=False)

            with patch("bot.new_session", return_value=session):
                watcher.run()

            self.assertEqual(watcher.store.load(), activation)
            session.get.assert_not_called()

    def test_resumes_acquired_activation_without_another_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            activation = bot.Activation("123", "447700900123", time.time(), "acquired")
            watcher.store.save(activation)
            watcher.notifiers = [Mock()]
            watcher.notifiers[0].send.return_value = True
            watcher.wait_for_code = Mock()
            session = Mock()
            session.__enter__ = Mock(return_value=session)
            session.__exit__ = Mock(return_value=False)

            with patch("bot.new_session", return_value=session):
                watcher.run()

            watcher.wait_for_code.assert_called_once_with(session, activation)
            session.get.assert_not_called()

    def test_retries_pending_code_notification_until_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            watcher.notifiers = [Mock()]
            watcher.notifiers[0].send.side_effect = [False, True]
            session = Mock()
            session.get.side_effect = [
                response("STATUS_OK:123456"),
                response("ACCESS_ACTIVATION"),
            ]
            watcher.stop.wait = Mock(return_value=False)
            activation = bot.Activation(
                "123", "447700900123", time.time(), "waiting_for_sms"
            )

            watcher.wait_for_code(session, activation)

            self.assertEqual(watcher.store.load().phase, "completed")
            self.assertEqual(watcher.notifiers[0].send.call_count, 2)

    def test_resumes_persisted_pending_code_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            watcher.notifiers = [Mock()]
            watcher.notifiers[0].send.return_value = True
            session = Mock()
            session.get.return_value = response("ACCESS_ACTIVATION")
            activation = bot.Activation(
                "123",
                "447700900123",
                time.time(),
                "code_notification_pending",
                "123456",
            )
            watcher.store.save(activation)

            watcher.wait_for_code(session, watcher.store.load())

            self.assertEqual(watcher.store.load().phase, "completed")
            watcher.notifiers[0].send.assert_called_once_with(
                "GRIZZLY SMS CODE RECEIVED",
                "Code: 123456\nActivation: 123",
                True,
            )

    def test_retries_readiness_without_cancelling_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            watcher.notifiers = [Mock()]
            session = Mock()
            session.get.side_effect = [
                response("ERROR_SQL"),
                response("ACCESS_READY"),
                response("STATUS_CANCEL"),
            ]
            watcher.stop.wait = Mock(return_value=False)
            activation = bot.Activation("123", "447700900123", time.time(), "acquired")

            watcher.wait_for_code(session, activation)

            statuses = [call.kwargs["params"].get("status") for call in session.get.call_args_list]
            self.assertEqual(statuses[:2], ["1", "1"])
            self.assertNotIn("8", statuses)
            self.assertEqual(watcher.store.load().phase, "failed")

    def test_persists_failed_timeout_cancellation_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            watcher.notifiers = [Mock()]
            watcher.notifiers[0].send.return_value = True
            watcher.stop.wait = Mock(side_effect=lambda _: watcher.stop.set())
            session = Mock()
            session.get.return_value = response("ERROR_SQL")
            activation = bot.Activation(
                "123",
                "447700900123",
                time.time() - 901,
                "waiting_for_sms",
            )

            watcher.wait_for_code(session, activation)

            self.assertEqual(watcher.store.load().phase, "cancellation_pending")

    def test_resumes_pending_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            session = Mock()
            session.get.return_value = response("ACCESS_CANCEL")
            activation = bot.Activation(
                "123", "447700900123", time.time(), "cancellation_pending"
            )

            watcher.wait_for_code(session, activation)

            self.assertEqual(watcher.store.load().phase, "cancelled")

    def test_reconciles_activation_already_cancelled_at_grizzly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            session = Mock()
            session.get.side_effect = [
                response("BAD_ACTION"),
                response("STATUS_CANCEL"),
            ]
            activation = bot.Activation(
                "123", "447700900123", time.time(), "cancellation_pending"
            )

            watcher.wait_for_code(session, activation)

            self.assertEqual(watcher.store.load().phase, "cancelled")
            self.assertTrue(watcher.stop.requested)

    def test_reconciles_activation_missing_at_grizzly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            session = Mock()
            session.get.side_effect = [
                response("BAD_ACTION"),
                response("NO_ACTIVATION"),
            ]
            activation = bot.Activation(
                "123", "447700900123", time.time(), "cancellation_pending"
            )

            watcher.wait_for_code(session, activation)

            self.assertEqual(watcher.store.load().phase, "cancelled")

    def test_reports_resend_requirement_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            watcher.notifiers = [Mock()]
            watcher.notifiers[0].send.return_value = True
            watcher.stop.wait = Mock(side_effect=lambda _: watcher.stop.set())
            session = Mock()
            session.get.return_value = response("STATUS_WAIT_RESEND")
            activation = bot.Activation(
                "123", "447700900123", time.time(), "waiting_for_sms"
            )

            watcher.wait_for_code(session, activation)

            self.assertEqual(watcher.store.load().phase, "resend_required")
            watcher.notifiers[0].send.assert_called_once_with(
                "Grizzly SMS resend required",
                "Activation: 123\nStatus: STATUS_WAIT_RESEND",
                True,
            )

    def test_stops_on_terminal_status_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            watcher = bot.Bot(config(str(Path(directory) / "state.db")))
            session = Mock()
            session.get.return_value = response("BAD_KEY")
            activation = bot.Activation(
                "123", "447700900123", time.time(), "waiting_for_sms"
            )

            with self.assertRaisesRegex(ValueError, "BAD_KEY"):
                watcher.wait_for_code(session, activation)


class ActivationControllerTests(unittest.TestCase):
    def test_starts_idle_without_purchasing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )

            self.assertEqual(controller.status()["phase"], "idle")
            self.assertTrue(controller.status()["canPurchase"])

    def test_purchase_requires_idle_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )
            controller.store.save(
                bot.Activation("123", "447700900123", time.time(), "waiting_for_sms")
            )

            accepted, message = controller.start_purchase()

            self.assertFalse(accepted)
            self.assertIn("current activation", message)

    def test_cancel_persists_before_starting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )
            controller.store.save(
                bot.Activation("123", "447700900123", time.time(), "waiting_for_sms")
            )
            controller._start_worker = Mock()

            accepted, _ = controller.cancel_active_activation()

            self.assertTrue(accepted)
            self.assertEqual(controller.store.load().phase, "cancellation_pending")
            controller._start_worker.assert_called_once_with(acquire_if_idle=False)

    def test_status_exposes_pending_sms_code_to_authenticated_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )
            controller.store.save(
                bot.Activation(
                    "123",
                    "447700900123",
                    time.time(),
                    "code_notification_pending",
                    "123456",
                )
            )

            status = controller.status()

            self.assertNotIn("smsCode", status)
            self.assertEqual(status["smsMessage"], "123456")

    def test_completed_activation_keeps_code_for_authenticated_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )
            controller.store.save(
                bot.Activation(
                    "123",
                    "905314393988",
                    time.time(),
                    "completed",
                    "123456",
                )
            )

            status = controller.status()

            self.assertEqual(status["phoneNumber"], "+90 531 439 39 88")
            self.assertEqual(status["phoneNumberCopy"], "+905314393988")
            self.assertEqual(status["phoneNumberNational"], "5314393988")
            self.assertEqual(status["smsMessage"], "123456")

    def test_auto_retry_toggle_works(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )

            self.assertFalse(controller.status()["autoRetryEnabled"])

            accepted, message = controller.toggle_auto_retry()

            self.assertTrue(accepted)
            self.assertEqual(message, "Auto-retry toggled")
            self.assertTrue(controller.status()["autoRetryEnabled"])

            accepted, message = controller.toggle_auto_retry()

            self.assertTrue(accepted)
            self.assertFalse(controller.status()["autoRetryEnabled"])

    def test_auto_retry_continues_after_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )
            controller.auto_retry_enabled = True
            controller._start_worker = Mock()
            controller.store.save(
                bot.Activation("123", "447700900123", time.time(), "cancelled")
            )

            # Simulate worker completion
            watcher = Mock()
            watcher.stop = Mock()
            watcher.stop.wait = Mock(return_value=False)
            watcher.close = Mock()

            controller._run_worker(watcher, acquire_if_idle=True)

            # Should have started a new worker after delay
            controller._start_worker.assert_called_once_with(acquire_if_idle=True)

    def test_auto_retry_stops_on_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )
            controller.auto_retry_enabled = True
            controller._start_worker = Mock()
            controller.store.save(
                bot.Activation("123", "447700900123", time.time(), "completed")
            )

            # Simulate worker completion
            watcher = Mock()
            watcher.stop = Mock()
            watcher.stop.wait = Mock(return_value=False)
            watcher.close = Mock()

            controller._run_worker(watcher, acquire_if_idle=True)

            # Should NOT have started a new worker
            controller._start_worker.assert_not_called()

    def test_auto_retry_stops_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = bot.ActivationController(
                config(str(Path(directory) / "state.db"))
            )
            controller.auto_retry_enabled = False
            controller._start_worker = Mock()
            controller.store.save(
                bot.Activation("123", "447700900123", time.time(), "cancelled")
            )

            # Simulate worker completion
            watcher = Mock()
            watcher.stop = Mock()
            watcher.stop.wait = Mock(return_value=False)
            watcher.close = Mock()

            controller._run_worker(watcher, acquire_if_idle=True)

            # Should NOT have started a new worker
            controller._start_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
