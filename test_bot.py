import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot


def response(text: str, ok: bool = True, status_code: int = 200) -> Mock:
    result = Mock()
    result.text = text
    result.ok = ok
    result.status_code = status_code
    result.headers = {}
    return result


class ParsingTests(unittest.TestCase):
    def test_parse_number(self) -> None:
        self.assertEqual(
            bot.parse_number("ACCESS_NUMBER:123:447700900123"),
            ("123", "447700900123"),
        )
        self.assertIsNone(bot.parse_number("NO_NUMBERS"))

    def test_parse_code(self) -> None:
        self.assertEqual(bot.parse_code("STATUS_OK:123456"), "123456")
        self.assertIsNone(bot.parse_code("STATUS_WAIT_CODE"))


class StateStoreTests(unittest.TestCase):
    def test_persists_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = bot.StateStore(str(Path(directory) / "state.db"))
            activation = bot.Activation("123", "447700900123", 1.0, "waiting_for_sms")
            store.save(activation)
            self.assertEqual(store.load(), activation)
            store.clear()
            self.assertIsNone(store.load())


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
            self.assertEqual(bot.Config.from_env().sms_poll_seconds, 5)


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


class LifecycleTests(unittest.TestCase):
    def test_delivers_code_then_completes_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = bot.Config(
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
                sms_poll_seconds=1,
                activation_timeout_seconds=900,
                state_db_path=str(Path(directory) / "state.db"),
            )
            watcher = bot.Bot(config)
            watcher.notifier = Mock()
            watcher.notifier.send.return_value = True
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
            watcher.notifier.send.assert_called_once_with(
                "GRIZZLY SMS CODE RECEIVED",
                "Code: 123456\nActivation: 123",
                True,
            )


if __name__ == "__main__":
    unittest.main()
