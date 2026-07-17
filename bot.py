from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter


LOG = logging.getLogger("grizzlysms")
API_URL = "https://api.grizzlysms.com/stubs/handler_api.php"


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def env_int(name: str, minimum: int = 1) -> int:
    value = int(env_required(name))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
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


@dataclass(frozen=True)
class Config:
    api_key: str
    service: str
    country: str
    max_price: str
    provider_ids: str | None
    workers: int
    rate: float
    timeout: float
    status_every: int
    ntfy_url: str
    api_url: str = API_URL

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            api_key=env_required("GRIZZLY_API_KEY"),
            service=env_required("SERVICE"),
            country=env_required("COUNTRY"),
            max_price=env_required("MAX_PRICE"),
            provider_ids=os.getenv("PROVIDER_IDS", "").strip() or None,
            workers=env_int("THREADS"),
            rate=env_float("MAX_REQUESTS_PER_SECOND"),
            timeout=env_float("REQUEST_TIMEOUT_SECONDS", 1),
            status_every=env_int_optional("STATUS_EVERY_REQUESTS", 100),
            ntfy_url=env_required("NTFY_URL"),
            api_url=os.getenv("GRIZZLY_API_URL", API_URL),
        )

    @property
    def params(self) -> dict[str, str]:
        params = {
            "api_key": self.api_key,
            "action": "getNumber",
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
        self.lock = threading.Lock()

    def wait(self, stop: threading.Event) -> bool:
        while not stop.is_set():
            with self.lock:
                delay = self.next_request - time.monotonic()
                if delay <= 0:
                    self.next_request = time.monotonic() + self.interval
                    return True
            stop.wait(min(delay, 0.25))
        return False

    def pause(self, seconds: float) -> None:
        with self.lock:
            self.next_request = max(self.next_request, time.monotonic() + seconds)


def parse_number(body: str) -> tuple[str, str] | None:
    parts = body.split(":", 2)
    if len(parts) == 3 and parts[0] == "ACCESS_NUMBER" and all(parts[1:]):
        return parts[1], parts[2]
    return None


def new_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=0, pool_maxsize=1))
    session.headers["User-Agent"] = "grizzlysms-stock-watcher/1.0"
    return session


class Bot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.stop = threading.Event()
        self.limiter = RateLimiter(config.rate)
        self.ntfy = new_session()
        self.ntfy_lock = threading.Lock()
        self.seen_lock = threading.Lock()
        self.seen_activations: set[str] = set()
        self.status_lock = threading.Lock()
        self.total_requests = 0
        self.no_numbers = 0

    def send_notification(self, title: str, message: str, urgent: bool = False) -> bool:
        try:
            with self.ntfy_lock:
                response = self.ntfy.post(
                    self.config.ntfy_url,
                    data=message.encode(),
                    headers={
                        "Title": title,
                        "Priority": "urgent" if urgent else "default",
                        "Tags": "telephone_receiver" if urgent else "white_check_mark",
                    },
                    timeout=self.config.timeout,
                )
            return response.ok
        except requests.RequestException as error:
            LOG.warning("ntfy error: %s", type(error).__name__)
            return False

    def mark_seen(self, activation_id: str) -> bool:
        with self.seen_lock:
            if activation_id in self.seen_activations:
                return False
            self.seen_activations.add(activation_id)
            return True

    def record_request(self, no_number: bool = False) -> None:
        with self.status_lock:
            self.total_requests += 1
            if no_number:
                self.no_numbers += 1
            if self.total_requests % self.config.status_every != 0:
                return
            with self.seen_lock:
                acquired = len(self.seen_activations)
            LOG.info(
                "still polling requests=%s no_numbers=%s acquired=%s",
                self.total_requests,
                self.no_numbers,
                acquired,
            )

    def notify_purchase(self, activation_id: str, phone_number: str) -> None:
        message = f"Number: {phone_number}\nActivation: {activation_id}"
        if self.send_notification("GRIZZLY NUMBER ACQUIRED", message, urgent=True):
            LOG.info("notification sent activation=%s", activation_id)
        else:
            LOG.warning("notification failed activation=%s", activation_id)

    def poll_worker(self, worker_id: int) -> None:
        session = new_session()
        try:
            while self.limiter.wait(self.stop):
                self.poll_once(session, worker_id)
        finally:
            session.close()

    def poll_once(self, session: requests.Session, worker_id: int) -> None:
        try:
            response = session.get(
                self.config.api_url,
                params=self.config.params,
                timeout=self.config.timeout,
            )
        except requests.RequestException as error:
            LOG.warning("Grizzly network error: %s", type(error).__name__)
            self.stop.wait(1)
            return

        if response.status_code != 200:
            self.record_request()
            delay = 2.0
            self.limiter.pause(delay)
            LOG.warning("Grizzly HTTP %s: pause %.1fs", response.status_code, delay)
            return

        body = response.text.strip()
        if body == "NO_NUMBERS":
            self.record_request(no_number=True)
            return

        self.record_request()

        number = parse_number(body)
        if not number:
            self.limiter.pause(2)
            LOG.warning("Grizzly response: %s", body[:100])
            return

        activation_id, phone_number = number
        if not self.mark_seen(activation_id):
            return

        LOG.info(
            "number acquired worker=%s activation=%s number=%s",
            worker_id,
            activation_id,
            phone_number,
        )
        self.notify_purchase(activation_id, phone_number)

    def run(self) -> None:
        cfg = self.config
        LOG.info(
            "startup service=%s country=%s maxPrice=%s providerIds=%s "
            "workers=%s limit=%.1f/s",
            cfg.service,
            cfg.country,
            cfg.max_price,
            cfg.provider_ids or "none",
            cfg.workers,
            cfg.rate,
        )
        result = self.send_notification(
            "Grizzly SMS startup test",
            f"Bot active: {cfg.workers} workers, limit {cfg.rate:g} req/s.",
        )
        LOG.info("ntfy test: %s", "OK" if result else "FAILED")

        threads = [
            threading.Thread(target=self.poll_worker, args=(worker,), name=f"poll-{worker}")
            for worker in range(1, cfg.workers + 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def close(self) -> None:
        self.stop.set()
        self.ntfy.close()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
    )
    try:
        bot = Bot(Config.from_env())
    except (ValueError, OSError) as error:
        LOG.error("startup failed: %s", error)
        return 2

    def shutdown(_signal: int, _frame: object) -> None:
        LOG.info("shutdown requested")
        bot.stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        bot.run()
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
