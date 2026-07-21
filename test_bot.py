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
            parsed = bot.Config.from_env()
            self.assertEqual(parsed.sms_poll_seconds, 5)
            self.assertIsNone(parsed.ntfy_url)


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
            watcher.notifiers[0].send.assert_called_once_with(
                "GRIZZLY SMS CODE RECEIVED",
                "Code: 123456\nActivation: 123",
                True,
            )


if __name__ == "__main__":
    unittest.main()
