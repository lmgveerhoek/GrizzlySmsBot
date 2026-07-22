from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import requests
import phonenumbers
from requests.adapters import HTTPAdapter


LOG = logging.getLogger("grizzlysms")
API_URL = "https://api.grizzlysms.com/stubs/handler_api.php"
ACTIVE_PHASES = {
    "acquired",
    "ready_pending",
    "waiting_for_sms",
    "resend_required",
    "cancellation_pending",
    "code_notification_pending",
    "code_delivered",
}


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def env_int_optional(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, default))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def env_float(name: str, minimum: float = 0.1) -> float:
    value = float(env_required(name))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def env_float_optional(name: str, default: float, minimum: float = 0.1) -> float:
    value = float(os.getenv(name, default))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Config:
    api_key: str
    service: str
    country: str
    max_price: str
    provider_ids: str | None
    rate: float
    timeout: float
    status_every: int
    discord_webhook_url: str
    discord_max_retries: int
    ntfy_url: str | None
    ntfy_max_retries: int
    sms_poll_seconds: float
    activation_timeout_seconds: int
    state_db_path: str
    web_ui: bool = False
    ui_password: str | None = None
    web_ui_host: str = "127.0.0.1"
    web_ui_port: int = 8080
    debug_logs: bool = False
    web_request_logs: bool = False
    auto_retry_enabled: bool = False
    auto_retry_timeout_seconds: int = 180
    auto_retry_delay_seconds: int = 5
    auto_retry_sound_lead_seconds: int = 5
    api_url: str = API_URL

    @classmethod
    def from_env(cls) -> Config:
        web_ui = env_bool("WEB_UI")
        ui_password = os.getenv("UI_PASSWORD", "").strip() or None
        if web_ui and not ui_password:
            raise ValueError("UI_PASSWORD is required when WEB_UI=true")
        return cls(
            api_key=env_required("GRIZZLY_API_KEY"),
            service=env_required("SERVICE"),
            country=env_required("COUNTRY"),
            max_price=env_required("MAX_PRICE"),
            provider_ids=os.getenv("PROVIDER_IDS", "").strip() or None,
            rate=env_float("MAX_REQUESTS_PER_SECOND"),
            timeout=env_float("REQUEST_TIMEOUT_SECONDS", 1),
            status_every=env_int_optional("STATUS_EVERY_REQUESTS", 100),
            discord_webhook_url=env_required("DISCORD_WEBHOOK_URL"),
            discord_max_retries=env_int_optional("DISCORD_MAX_RETRIES", 5),
            ntfy_url=os.getenv("NTFY_URL", "").strip() or None,
            ntfy_max_retries=env_int_optional("NTFY_MAX_RETRIES", 5),
            sms_poll_seconds=env_float_optional("SMS_POLL_SECONDS", 5, 1),
            activation_timeout_seconds=env_int_optional(
                "ACTIVATION_TIMEOUT_SECONDS", 900
            ),
            state_db_path=os.getenv("STATE_DB_PATH", "grizzlysms.db"),
            web_ui=web_ui,
            ui_password=ui_password,
            web_ui_host=os.getenv("WEB_UI_HOST", "127.0.0.1"),
            web_ui_port=env_int_optional("WEB_UI_PORT", 8080),
            debug_logs=env_bool("DEBUG_LOGS"),
            web_request_logs=env_bool("WEB_REQUEST_LOGS"),
            auto_retry_enabled=env_bool("AUTO_RETRY_ENABLED"),
            auto_retry_timeout_seconds=env_int_optional("AUTO_RETRY_TIMEOUT_SECONDS", 180),
            auto_retry_delay_seconds=env_int_optional("AUTO_RETRY_DELAY_SECONDS", 5),
            auto_retry_sound_lead_seconds=env_int_optional(
                "AUTO_RETRY_SOUND_LEAD_SECONDS", 5, minimum=5
            ),
            api_url=os.getenv("GRIZZLY_API_URL", API_URL),
        )

    @property
    def params(self) -> dict[str, str]:
        params = {
            "api_key": self.api_key,
            "action": "getNumberV2",
            "service": self.service,
            "country": self.country,
            "maxPrice": self.max_price,
        }
        if self.provider_ids:
            params["providerIds"] = self.provider_ids
        return params


class RateLimiter:
    def __init__(self, rate: float) -> None:
        self.interval = 1 / rate
        self.next_request = time.monotonic()

    def wait(self, stop: "StopSignal") -> bool:
        while not stop.requested:
            delay = self.next_request - time.monotonic()
            if delay <= 0:
                self.next_request = time.monotonic() + self.interval
                return True
            stop.wait(min(delay, 0.25))
        return False

    def pause(self, seconds: float) -> None:
        self.next_request = max(self.next_request, time.monotonic() + seconds)


@dataclass(frozen=True)
class AcquisitionDetails:
    activation_id: str
    phone_number: str
    cost: str
    currency: str | None
    country_code: str | None


def parse_number_v2(body: str) -> AcquisitionDetails | None:
    try:
        payload = json.loads(body, parse_float=Decimal)
        activation_id = str(payload["activationId"])
        phone_number = str(payload["phoneNumber"])
        cost = str(Decimal(str(payload["activationCost"])))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if not activation_id or not phone_number:
        return None
    currency = payload.get("currency")
    country_code = payload.get("countryCode")
    return AcquisitionDetails(
        activation_id,
        phone_number,
        cost,
        str(currency) if currency is not None else None,
        str(country_code) if country_code is not None else None,
    )


def parse_code(body: str) -> str | None:
    prefix = "STATUS_OK:"
    if body.startswith(prefix) and body[len(prefix) :]:
        return body[len(prefix) :]
    return None


def phone_representations(phone_number: str) -> tuple[str, str]:
    digits = "".join(character for character in phone_number if character.isdigit())
    candidate = f"+{digits}"
    try:
        parsed = phonenumbers.parse(candidate, None)
        if phonenumbers.is_possible_number(parsed):
            return (
                phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
                ),
                str(parsed.national_number),
            )
    except phonenumbers.NumberParseException:
        pass
    return candidate, digits


@dataclass(frozen=True)
class Activation:
    activation_id: str
    phone_number: str
    acquired_at: float
    phase: str
    sms_code: str | None = None


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    activation_id TEXT NOT NULL,
                    phone_number TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    phase TEXT NOT NULL,
                    sms_code TEXT
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(activation)")
            }
            if "sms_code" not in columns:
                connection.execute("ALTER TABLE activation ADD COLUMN sms_code TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activation_history (
                    activation_id TEXT PRIMARY KEY,
                    phone_number TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    phase TEXT NOT NULL,
                    cost TEXT NOT NULL,
                    currency TEXT,
                    country_code TEXT,
                    provider_filter TEXT,
                    code_received INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def load(self) -> Activation | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT activation_id, phone_number, acquired_at, phase, sms_code "
                "FROM activation WHERE singleton = 1"
            ).fetchone()
        return Activation(*row) if row else None

    def save(self, activation: Activation) -> None:
        with self.connection() as connection:
            self._save_activation(connection, activation)

    @staticmethod
    def _save_activation(
        connection: sqlite3.Connection, activation: Activation
    ) -> None:
        connection.execute(
            """
            INSERT INTO activation
                (singleton, activation_id, phone_number, acquired_at, phase, sms_code)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                activation_id = excluded.activation_id,
                phone_number = excluded.phone_number,
                acquired_at = excluded.acquired_at,
                phase = excluded.phase,
                sms_code = excluded.sms_code
            """,
            (
                activation.activation_id,
                activation.phone_number,
                activation.acquired_at,
                activation.phase,
                activation.sms_code,
            ),
        )
        connection.execute(
            """
            UPDATE activation_history
            SET phase = ?, updated_at = ?,
                code_received = CASE WHEN ? IS NOT NULL THEN 1 ELSE code_received END
            WHERE activation_id = ?
            """,
            (
                activation.phase,
                time.time(),
                activation.sms_code,
                activation.activation_id,
            ),
        )

    def record_acquisition(
        self,
        activation: Activation,
        details: AcquisitionDetails,
        provider_filter: str | None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO activation_history (
                    activation_id, phone_number, acquired_at, updated_at, phase,
                    cost, currency, country_code, provider_filter, code_received
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    activation.activation_id,
                    activation.phone_number,
                    activation.acquired_at,
                    activation.acquired_at,
                    activation.phase,
                    details.cost,
                    details.currency,
                    details.country_code,
                    provider_filter,
                ),
            )
            self._save_activation(connection, activation)

    def history(self, limit: int = 100) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT activation_id, phone_number, acquired_at, updated_at, phase,
                       cost, currency, country_code, provider_filter, code_received
                FROM activation_history
                ORDER BY acquired_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        entries = []
        for row in rows:
            international, national = phone_representations(row[1])
            entries.append(
                {
                    "activationId": row[0],
                    "phoneNumber": international,
                    "phoneNumberNational": national,
                    "acquiredAt": datetime.fromtimestamp(
                        row[2], timezone.utc
                    ).isoformat(),
                    "updatedAt": datetime.fromtimestamp(
                        row[3], timezone.utc
                    ).isoformat(),
                    "phase": row[4],
                    "cost": row[5],
                    "currency": row[6],
                    "countryCode": row[7],
                    "providerFilter": row[8],
                    "codeReceived": bool(row[9]),
                }
            )
        return entries

    def history_summary(self) -> dict[str, object]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT phase, cost, currency, code_received FROM activation_history"
            ).fetchall()
        gross_values: dict[str, Decimal] = {}
        for _, cost, currency, _ in rows:
            key = currency or "unknown"
            gross_values[key] = gross_values.get(key, Decimal("0")) + Decimal(cost)
        return {
            "attempts": len(rows),
            "codesReceived": sum(bool(row[3]) for row in rows),
            "unsuccessful": sum(row[0] in {"cancelled", "failed"} for row in rows),
            "grossPurchaseValues": {
                currency: str(value) for currency, value in gross_values.items()
            },
        }

    def clear(self) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM activation WHERE singleton = 1")


class StopSignal:
    def __init__(self) -> None:
        self.requested = False

    def set(self) -> None:
        self.requested = True

    def wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self.requested and time.monotonic() < deadline:
            time.sleep(min(0.25, deadline - time.monotonic()))
        return self.requested


class DiscordNotifier:
    def __init__(self, webhook_url: str, timeout: float, max_retries: int) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = new_session()

    def send(self, title: str, message: str, urgent: bool = False) -> bool:
        payload = {
            "content": f"**{title}**\n{message}",
            "allowed_mentions": {"parse": []},
        }
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as error:
                LOG.warning("Discord network error: %s", type(error).__name__)
                delay = 2**attempt
            else:
                if response.ok:
                    return True
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    delay = self.retry_delay(response, attempt)
                    LOG.warning("Discord HTTP %s; retrying in %.1fs", response.status_code, delay)
                else:
                    LOG.warning("Discord HTTP %s", response.status_code)
                    return False
            time.sleep(delay)
        LOG.warning("Discord notification failed after %s attempts", self.max_retries)
        return False

    @staticmethod
    def retry_delay(response: requests.Response, attempt: int) -> float:
        try:
            return max(float(response.headers.get("Retry-After", "")), 0.1)
        except ValueError:
            pass
        if response.status_code == 429:
            try:
                return max(float(response.json().get("retry_after")), 0.1)
            except (ValueError, requests.JSONDecodeError, AttributeError):
                pass
        return float(2**attempt)

    def close(self) -> None:
        self.session.close()


class NtfyNotifier:
    def __init__(self, url: str, timeout: float, max_retries: int) -> None:
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = new_session()

    def send(self, title: str, message: str, urgent: bool = False) -> bool:
        headers = {
            "Title": title,
            "Priority": "urgent" if urgent else "default",
            "Tags": "telephone_receiver" if urgent else "white_check_mark",
        }
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(
                    self.url,
                    data=message.encode(),
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as error:
                LOG.warning("ntfy network error: %s", type(error).__name__)
            else:
                if response.ok:
                    return True
                if response.status_code != 429 and not 500 <= response.status_code < 600:
                    LOG.warning("ntfy HTTP %s", response.status_code)
                    return False
                delay = DiscordNotifier.retry_delay(response, attempt)
                LOG.warning("ntfy HTTP %s; retrying in %.1fs", response.status_code, delay)
                time.sleep(delay)
                continue
            time.sleep(2**attempt)
        LOG.warning("ntfy notification failed after %s attempts", self.max_retries)
        return False

    def close(self) -> None:
        self.session.close()


def new_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=0, pool_maxsize=1))
    session.headers["User-Agent"] = "grizzlysms-stock-watcher/1.0"
    return session


class Bot:
    def __init__(
        self,
        config: Config,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.config = config
        self.activation_timeout_seconds = config.activation_timeout_seconds
        self.activation_timeout_lock = threading.Lock()
        self.stop = StopSignal()
        self.limiter = RateLimiter(config.rate)
        self.notifiers = [
            DiscordNotifier(
                config.discord_webhook_url,
                config.timeout,
                config.discord_max_retries,
            )
        ]
        if config.ntfy_url:
            self.notifiers.append(
                NtfyNotifier(config.ntfy_url, config.timeout, config.ntfy_max_retries)
            )
        self.store = StateStore(config.state_db_path)
        self.total_requests = 0
        self.no_numbers = 0
        self.progress_callback = progress_callback

    def send_notification(self, title: str, message: str, urgent: bool = False) -> bool:
        results = [notifier.send(title, message, urgent) for notifier in self.notifiers]
        return any(results)

    def record_request(self, no_number: bool = False) -> None:
        self.total_requests += 1
        if no_number:
            self.no_numbers += 1
        if self.progress_callback:
            self.progress_callback(self.total_requests, self.no_numbers)
        if (
            self.total_requests == 1
            or self.total_requests % self.config.status_every == 0
        ):
            LOG.info(
                "still polling requests=%s no_numbers=%s",
                self.total_requests,
                self.no_numbers,
            )

    def notify_purchase(self, activation_id: str, phone_number: str) -> bool:
        message = f"Number: {phone_number}\nActivation: {activation_id}"
        if self.send_notification("GRIZZLY NUMBER ACQUIRED", message, urgent=True):
            LOG.info("notification sent activation=%s", activation_id)
            return True
        else:
            LOG.warning("notification failed activation=%s", activation_id)
            return False

    def acquire(self, session: requests.Session) -> Activation | None:
        try:
            response = session.get(
                self.config.api_url,
                params=self.config.params,
                timeout=self.config.timeout,
            )
        except requests.RequestException as error:
            LOG.warning("Grizzly network error: %s", type(error).__name__)
            self.stop.wait(1)
            return None

        if response.status_code != 200:
            self.record_request()
            delay = 2.0
            self.limiter.pause(delay)
            LOG.warning("Grizzly HTTP %s: pause %.1fs", response.status_code, delay)
            return None

        body = response.text.strip()
        if body == "NO_NUMBERS":
            self.record_request(no_number=True)
            return None

        self.record_request()

        details = parse_number_v2(body)
        if not details:
            if body in {
                "BAD_KEY",
                "NO_KEY",
                "NO_BALANCE",
                "SERVICE_UNAVAILABLE_REGION",
            } or (
                "prohibited for sale" in body.lower()
            ):
                raise ValueError(f"Grizzly terminal error: {body}")
            self.limiter.pause(2)
            LOG.warning("Grizzly response: %s", body[:100])
            return None

        activation_id = details.activation_id
        phone_number = details.phone_number
        activation = Activation(activation_id, phone_number, time.time(), "acquired")
        self.store.record_acquisition(
            activation,
            details,
            self.config.provider_ids,
        )
        LOG.info(
            "number acquired activation=%s number=%s cost=%s currency=%s",
            activation_id,
            phone_number,
            details.cost,
            details.currency or "unknown",
        )
        return activation

    def change_status(self, session: requests.Session, activation: Activation, status: int) -> bool:
        try:
            response = session.get(
                self.config.api_url,
                params={
                    "api_key": self.config.api_key,
                    "action": "setStatus",
                    "id": activation.activation_id,
                    "status": str(status),
                },
                timeout=self.config.timeout,
            )
        except requests.RequestException as error:
            LOG.warning("Grizzly status update error: %s", type(error).__name__)
            return False
        if response.ok and response.text.strip().startswith("ACCESS_"):
            return True
        LOG.warning("Grizzly status update failed: %s", response.text.strip()[:100])
        return False

    def get_status(self, session: requests.Session, activation: Activation) -> str | None:
        try:
            response = session.get(
                self.config.api_url,
                params={
                    "api_key": self.config.api_key,
                    "action": "getStatus",
                    "id": activation.activation_id,
                },
                timeout=self.config.timeout,
            )
        except requests.RequestException as error:
            LOG.warning("Grizzly SMS status error: %s", type(error).__name__)
            return None
        if not response.ok:
            LOG.warning("Grizzly SMS status HTTP %s", response.status_code)
            return None
        return response.text.strip()

    def finish(self, activation: Activation, phase: str) -> None:
        sms_code = activation.sms_code if phase == "completed" else None
        self.store.save(
            Activation(
                activation.activation_id,
                activation.phone_number,
                activation.acquired_at,
                phase,
                sms_code,
            )
        )
        self.stop.set()

    def set_activation_timeout(self, seconds: int) -> None:
        with self.activation_timeout_lock:
            self.activation_timeout_seconds = seconds

    def request_cancel(self) -> bool:
        activation = self.store.load()
        if not activation or activation.phase not in ACTIVE_PHASES:
            return False
        self.store.save(
            Activation(
                activation.activation_id,
                activation.phone_number,
                activation.acquired_at,
                "cancellation_pending",
            )
        )
        return True

    def wait_for_code(self, session: requests.Session, activation: Activation) -> None:
        persisted = self.store.load()
        if (
            persisted
            and persisted.activation_id == activation.activation_id
            and persisted.phase == "cancellation_pending"
        ):
            activation = persisted
        if activation.phase == "acquired":
            activation = Activation(
                activation.activation_id,
                activation.phone_number,
                activation.acquired_at,
                "ready_pending",
            )
            self.store.save(activation)
        while not self.stop.requested:
            persisted = self.store.load()
            if (
                persisted
                and persisted.activation_id == activation.activation_id
                and persisted.phase == "cancellation_pending"
            ):
                activation = persisted

            if activation.phase == "cancellation_pending":
                if self.change_status(session, activation, 8):
                    self.finish(activation, "cancelled")
                    return
                status = self.get_status(session, activation)
                if status in {"STATUS_CANCEL", "NO_ACTIVATION"}:
                    LOG.info(
                        "cancellation already complete activation=%s",
                        activation.activation_id,
                    )
                    self.finish(activation, "cancelled")
                    return
                if status in {"BAD_KEY", "BAD_ACTION", "SERVICE_UNAVAILABLE_REGION"}:
                    raise ValueError(f"Grizzly terminal status error: {status}")
                self.stop.wait(self.config.sms_poll_seconds)
                continue

            if activation.phase == "code_notification_pending":
                if activation.sms_code is None:
                    LOG.error("pending code is missing activation=%s", activation.activation_id)
                    self.finish(activation, "failed")
                    return
                if self.send_notification(
                    "GRIZZLY SMS CODE RECEIVED",
                    f"Code: {activation.sms_code}\nActivation: {activation.activation_id}",
                    urgent=True,
                ):
                    activation = Activation(
                        activation.activation_id,
                        activation.phone_number,
                        activation.acquired_at,
                        "code_delivered",
                        activation.sms_code,
                    )
                    self.store.save(activation)
                else:
                    self.stop.wait(self.config.sms_poll_seconds)
                continue

            if activation.phase == "code_delivered":
                if self.change_status(session, activation, 6):
                    self.finish(activation, "completed")
                    return
                self.stop.wait(self.config.sms_poll_seconds)
                continue

            timed_out = False
            with self.activation_timeout_lock:
                if (
                    time.time() - activation.acquired_at
                    >= self.activation_timeout_seconds
                ):
                    activation = Activation(
                        activation.activation_id,
                        activation.phone_number,
                        activation.acquired_at,
                        "cancellation_pending",
                    )
                    self.store.save(activation)
                    timed_out = True
            if timed_out:
                self.send_notification(
                    "Grizzly SMS timed out",
                    f"Activation: {activation.activation_id}",
                    urgent=True,
                )
                continue

            if activation.phase == "ready_pending":
                if self.change_status(session, activation, 1):
                    activation = Activation(
                        activation.activation_id,
                        activation.phone_number,
                        activation.acquired_at,
                        "waiting_for_sms",
                    )
                    self.store.save(activation)
                else:
                    self.stop.wait(self.config.sms_poll_seconds)
                continue

            status = self.get_status(session, activation)
            if status is None:
                self.stop.wait(self.config.sms_poll_seconds)
                continue

            if status == "STATUS_WAIT_CODE":
                if activation.phase == "resend_required":
                    activation = Activation(
                        activation.activation_id,
                        activation.phone_number,
                        activation.acquired_at,
                        "waiting_for_sms",
                    )
                    self.store.save(activation)
                self.stop.wait(self.config.sms_poll_seconds)
                continue

            code = parse_code(status)
            if code:
                activation = Activation(
                    activation.activation_id,
                    activation.phone_number,
                    activation.acquired_at,
                    "code_notification_pending",
                    code,
                )
                self.store.save(activation)
                continue

            if status.startswith("STATUS_WAIT_RETRY") or status == "STATUS_WAIT_RESEND":
                if activation.phase != "resend_required":
                    activation = Activation(
                        activation.activation_id,
                        activation.phone_number,
                        activation.acquired_at,
                        "resend_required",
                    )
                    self.store.save(activation)
                    self.send_notification(
                        "Grizzly SMS resend required",
                        f"Activation: {activation.activation_id}\nStatus: {status}",
                        urgent=True,
                    )
                self.stop.wait(self.config.sms_poll_seconds)
                continue

            if status in {"STATUS_CANCEL", "NO_ACTIVATION", "BAD_STATUS"}:
                LOG.warning("activation ended status=%s", status)
                self.finish(activation, "failed")
                return

            if status in {"BAD_KEY", "BAD_ACTION", "SERVICE_UNAVAILABLE_REGION"}:
                raise ValueError(f"Grizzly terminal status error: {status}")

            LOG.warning("unexpected SMS status: %s", status[:100])
            self.stop.wait(self.config.sms_poll_seconds)

    def send_startup_notification(self) -> bool:
        cfg = self.config
        LOG.info(
            "startup service=%s country=%s maxPrice=%s providerIds=%s "
            "limit=%.1f/s",
            cfg.service,
            cfg.country,
            cfg.max_price,
            cfg.provider_ids or "none",
            cfg.rate,
        )
        result = self.send_notification(
            "Grizzly SMS startup test",
            f"Bot active: one-shot mode, limit {cfg.rate:g} req/s.",
        )
        LOG.info("Discord test: %s", "OK" if result else "FAILED")
        return result

    def run(self, acquire_if_idle: bool = True, notify_startup: bool = True) -> None:
        if notify_startup:
            self.send_startup_notification()

        activation = self.store.load()
        if activation and activation.phase in ACTIVE_PHASES:
            LOG.info("resuming activation=%s", activation.activation_id)
        elif activation:
            if not acquire_if_idle:
                return
            LOG.info("clearing terminal activation=%s", activation.activation_id)
            self.store.clear()
            activation = None

        if activation is None and not acquire_if_idle:
            return

        with new_session() as session:
            acquired_now = activation is None
            while activation is None and self.limiter.wait(self.stop):
                activation = self.acquire(session)
            if activation is None or self.stop.requested:
                return

            if acquired_now:
                self.store.save(activation)

            if activation.phase == "acquired" and not self.notify_purchase(
                activation.activation_id, activation.phone_number
            ):
                return
            self.wait_for_code(session, activation)

    def close(self) -> None:
        self.stop.set()
        for notifier in self.notifiers:
            notifier.close()


class ActivationController:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = StateStore(config.state_db_path)
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.bot: Bot | None = None
        self.events: deque[dict[str, str]] = deque(maxlen=50)
        self.last_error: str | None = None
        self.auto_retry_enabled = config.auto_retry_enabled
        self.auto_retry_interrupt = threading.Event()
        self.auto_retry_waiting = False
        self.auto_retry_stage: str | None = None
        self.auto_retry_signal = 0
        self.auto_retry_claimed_signal = 0
        self.auto_retry_claim_deadline = 0.0
        self.auto_retry_acknowledged_signal = 0
        self.auto_retry_attempt_active = False
        self.cancel_auto_retry_attempt = False
        self.shutting_down = False
        self.acquisition_requests = 0
        self.no_number_responses = 0

    def add_event(self, message: str, level: str = "info") -> None:
        with self.lock:
            self.events.appendleft(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "level": level,
                    "message": message,
                }
            )

    def start(self) -> None:
        notifier = Bot(self.config)
        try:
            delivered = notifier.send_startup_notification()
            self.add_event(
                "Notification test delivered" if delivered else "Notification test failed",
                "info" if delivered else "warning",
            )
        finally:
            notifier.close()
        activation = self.store.load()
        if activation and activation.phase in ACTIVE_PHASES:
            self.add_event("Resuming saved activation")
            self._start_worker(acquire_if_idle=False)

    def _start_worker(self, acquire_if_idle: bool) -> None:
        with self.lock:
            if self.shutting_down:
                raise RuntimeError("Controller is shutting down")
            if self.worker and self.worker.is_alive():
                raise RuntimeError("Activation work is already running")
            self.auto_retry_interrupt.clear()
            self.bot = self._new_bot()
            self.worker = threading.Thread(
                target=self._run_worker,
                args=(self.bot, acquire_if_idle),
                name="activation-worker",
                daemon=True,
            )
            self.worker.start()

    def _new_bot(self) -> Bot:
        self.acquisition_requests = 0
        self.no_number_responses = 0
        return Bot(self._effective_config(), self._record_acquisition_progress)

    def _record_acquisition_progress(
        self, requests_sent: int, no_number_responses: int
    ) -> None:
        with self.lock:
            self.acquisition_requests = requests_sent
            self.no_number_responses = no_number_responses
        if requests_sent == 1:
            self.add_event(
                "Grizzly availability check sent; no matching number returned yet",
                "warning" if no_number_responses else "info",
            )
            return
        self.add_event(
            "Grizzly availability checks: "
            f"{requests_sent} requests sent, "
            f"{no_number_responses} returned no matching number",
            "warning" if no_number_responses == requests_sent else "info",
        )

    def _effective_config(self) -> Config:
        timeout = (
            self.config.auto_retry_timeout_seconds
            if self.auto_retry_enabled
            else self.config.activation_timeout_seconds
        )
        return replace(self.config, activation_timeout_seconds=timeout)

    def _run_worker(self, watcher: Bot, acquire_if_idle: bool) -> None:
        current = watcher
        should_acquire = acquire_if_idle
        while True:
            failed = False
            try:
                current.run(acquire_if_idle=should_acquire, notify_startup=False)
            except Exception as error:
                failed = True
                LOG.exception("activation worker failed")
                with self.lock:
                    self.last_error = str(error)
                self.add_event("Activation worker failed", "error")
            finally:
                current.close()

            activation = self.store.load()
            with self.lock:
                cancel_automatic_attempt = self.cancel_auto_retry_attempt
                self.cancel_auto_retry_attempt = False
                self.auto_retry_attempt_active = False
            if (
                cancel_automatic_attempt
                and activation
                and activation.phase in ACTIVE_PHASES
            ):
                self.add_event(
                    "Auto-retry disabled; cancelling automatic purchase",
                    "warning",
                )
                cancelled = Activation(
                    activation.activation_id,
                    activation.phone_number,
                    activation.acquired_at,
                    "cancellation_pending",
                )
                self.store.save(cancelled)
                cleanup = Bot(self.config)
                with self.lock:
                    self.bot = cleanup
                try:
                    cleanup.run(acquire_if_idle=False, notify_startup=False)
                except Exception as error:
                    failed = True
                    LOG.exception("automatic activation cleanup failed")
                    with self.lock:
                        self.last_error = str(error)
                    self.add_event("Automatic activation cleanup failed", "error")
                finally:
                    cleanup.close()
                    with self.lock:
                        if self.bot is cleanup:
                            self.bot = None
                activation = self.store.load()
            if activation:
                self.add_event(f"Activation state: {activation.phase}")
            with self.lock:
                if self.bot is current:
                    self.bot = None
                should_retry = (
                    not failed
                    and self.auto_retry_enabled
                    and not self.shutting_down
                    and activation
                    and activation.phase != "completed"
                    and activation.phase not in ACTIVE_PHASES
                )
                if should_retry:
                    self.auto_retry_waiting = True
                    self.auto_retry_stage = "delay"
            if not should_retry:
                break

            self.add_event("Auto-retry: waiting before next attempt")
            self.auto_retry_interrupt.wait(
                self.config.auto_retry_delay_seconds
            )
            with self.lock:
                if not self.auto_retry_enabled or self.shutting_down:
                    self.auto_retry_waiting = False
                    self.auto_retry_stage = None
                    break
                self.auto_retry_interrupt.clear()
                self.auto_retry_stage = "announcing"
                self.auto_retry_signal += 1
                retry_signal = self.auto_retry_signal
                self.auto_retry_claimed_signal = 0
                self.auto_retry_claim_deadline = 0.0

            base_deadline = (
                time.monotonic() + self.config.auto_retry_sound_lead_seconds
            )
            stop_retry = False
            while True:
                with self.lock:
                    deadline = max(base_deadline, self.auto_retry_claim_deadline)
                    if time.monotonic() >= deadline:
                        self.auto_retry_stage = None
                        break
                self.auto_retry_interrupt.wait(
                    min(0.25, max(0, deadline - time.monotonic()))
                )
                with self.lock:
                    if not self.auto_retry_enabled or self.shutting_down:
                        stop_retry = True
                        self.auto_retry_stage = None
                        break
                    if self.auto_retry_acknowledged_signal == retry_signal:
                        self.auto_retry_stage = None
                        break
                    self.auto_retry_interrupt.clear()

            with self.lock:
                self.auto_retry_waiting = False
                self.auto_retry_stage = None
                if stop_retry or not self.auto_retry_enabled or self.shutting_down:
                    break
                current = self._new_bot()
                self.bot = current
                self.auto_retry_attempt_active = True
            self.add_event("Auto-retry: starting next attempt")
            should_acquire = True

    def start_purchase(self) -> tuple[bool, str]:
        with self.lock:
            if self.worker and self.worker.is_alive():
                return False, "Activation work is already running"
            activation = self.store.load()
            if activation and activation.phase in ACTIVE_PHASES:
                return False, "Cancel or finish the current activation first"
            self.last_error = None
            self.add_event("Number purchase requested")
            self._start_worker(acquire_if_idle=True)
            return True, "Looking for a number"

    def cancel_active_activation(self) -> tuple[bool, str]:
        with self.lock:
            activation = self.store.load()
            if not activation or activation.phase not in ACTIVE_PHASES:
                return False, "There is no active activation to cancel"
            cancelled = Activation(
                activation.activation_id,
                activation.phone_number,
                activation.acquired_at,
                "cancellation_pending",
            )
            self.store.save(cancelled)
            self.add_event("Cancellation requested", "warning")
            if self.bot:
                self.bot.request_cancel()
            if not self.worker or not self.worker.is_alive():
                self._start_worker(acquire_if_idle=False)
            return True, "Cancellation requested"

    def retry_pending_work(self) -> tuple[bool, str]:
        with self.lock:
            activation = self.store.load()
            if not activation or activation.phase not in ACTIVE_PHASES:
                return False, "There is no pending activation work"
            if self.worker and self.worker.is_alive():
                return False, "Activation work is already running"
            self.last_error = None
            self.add_event("Retry requested")
            self._start_worker(acquire_if_idle=False)
            return True, "Retry started"

    def set_auto_retry(self, enabled: bool) -> tuple[bool, str]:
        with self.lock:
            self.auto_retry_enabled = enabled
            if not self.auto_retry_enabled:
                self.auto_retry_interrupt.set()
                if self.auto_retry_attempt_active and self.bot:
                    self.cancel_auto_retry_attempt = True
                    self.bot.stop.set()
            else:
                self.auto_retry_interrupt.clear()
            if self.bot:
                self.bot.set_activation_timeout(
                    self.config.auto_retry_timeout_seconds
                    if self.auto_retry_enabled
                    else self.config.activation_timeout_seconds
                )
            self.add_event(
                "Auto-retry enabled" if self.auto_retry_enabled else "Auto-retry disabled"
            )
            state = "enabled" if self.auto_retry_enabled else "disabled"
            return True, f"Auto-retry {state}"

    def claim_auto_retry_sound(self, signal: int) -> tuple[bool, str]:
        with self.lock:
            if (
                self.auto_retry_stage != "announcing"
                or signal != self.auto_retry_signal
                or signal == self.auto_retry_claimed_signal
            ):
                return False, "Retry alert is no longer pending"
            self.auto_retry_claimed_signal = signal
            self.auto_retry_claim_deadline = time.monotonic() + 3
            self.auto_retry_interrupt.set()
            return True, "Retry alert claimed"

    def acknowledge_auto_retry_sound(self, signal: int) -> tuple[bool, str]:
        with self.lock:
            if (
                self.auto_retry_stage != "announcing"
                or signal != self.auto_retry_signal
                or signal != self.auto_retry_claimed_signal
            ):
                return False, "Retry alert is no longer pending"
            self.auto_retry_acknowledged_signal = signal
            self.auto_retry_interrupt.set()
            return True, "Retry alert acknowledged"

    def status(self) -> dict[str, object]:
        with self.lock:
            activation = self.store.load()
            worker_active = bool(self.worker and self.worker.is_alive())
            is_polling_for_number = worker_active and activation is None
            phase = (
                activation.phase
                if activation
                else "searching" if is_polling_for_number else "idle"
            )
            elapsed = max(0, int(time.time() - activation.acquired_at)) if activation else 0
            effective_timeout = (
                self.config.auto_retry_timeout_seconds
                if self.auto_retry_enabled
                else self.config.activation_timeout_seconds
            )
            remaining = (
                max(0, effective_timeout - elapsed)
                if activation and phase in ACTIVE_PHASES
                else 0
            )
            active = bool(activation and phase in ACTIVE_PHASES)
            retryable = active and not worker_active
            international, national = (
                phone_representations(activation.phone_number)
                if activation
                else (None, None)
            )
            phone_copy = (
                f"+{''.join(character for character in activation.phone_number if character.isdigit())}"
                if activation
                else None
            )
            return {
                "phase": phase,
                "phoneNumber": international,
                "phoneNumberCopy": phone_copy,
                "phoneNumberNational": national,
                "activationId": activation.activation_id if activation else None,
                "smsMessage": activation.sms_code if activation else None,
                "elapsedSeconds": elapsed,
                "timeoutRemainingSeconds": remaining,
                "workerActive": worker_active,
                "isPollingForNumber": is_polling_for_number,
                "acquisitionRequests": self.acquisition_requests,
                "noNumberResponses": self.no_number_responses,
                "service": self.config.service,
                "country": self.config.country,
                "maxPrice": self.config.max_price,
                "providerIds": self.config.provider_ids,
                "canPurchase": not active and not worker_active,
                "canCancel": active and phase != "cancellation_pending",
                "canRetry": retryable,
                "autoRetryEnabled": self.auto_retry_enabled,
                "autoRetryWaiting": self.auto_retry_waiting,
                "autoRetryStage": self.auto_retry_stage,
                "autoRetrySignal": self.auto_retry_signal,
                "autoRetryTimeoutSeconds": self.config.auto_retry_timeout_seconds,
                "autoRetryDelaySeconds": self.config.auto_retry_delay_seconds,
                "autoRetrySoundLeadSeconds": self.config.auto_retry_sound_lead_seconds,
                "lastError": self.last_error,
                "events": list(self.events),
                "history": self.store.history(),
                "historySummary": self.store.history_summary(),
            }

    def shutdown(self) -> None:
        with self.lock:
            self.shutting_down = True
            self.auto_retry_interrupt.set()
            if self.bot:
                self.bot.stop.set()
            worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=self.config.timeout + 5)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
    )
    try:
        config = Config.from_env()
    except (ValueError, OSError) as error:
        LOG.error("startup failed: %s", error)
        return 2

    if config.debug_logs:
        logging.getLogger().setLevel(logging.DEBUG)

    if config.web_ui:
        from web import run_web_ui

        return run_web_ui(config)

    bot = Bot(config)

    def shutdown(_signal: int, _frame: object) -> None:
        LOG.info("shutdown requested")
        bot.stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        bot.run()
    except ValueError as error:
        LOG.error("runtime failed: %s", error)
        return 2
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
