# Grizzly SMS Bot

A one-shot Grizzly SMS activation bot. It acquires one phone number, sends it to
Discord and optionally ntfy, waits for the verification SMS, sends the code once,
completes the activation, and exits. It never automatically starts another purchase.

The activation ID is persisted in SQLite. If the process stops while waiting for
the SMS, the next manual start resumes the same activation instead of buying a
second number.

## Requirements

- [Docker](https://www.docker.com/) with Docker Compose, or Python 3.13+
- A [Grizzly SMS](https://grizzlysms.com/) API key and account balance
- A private Discord channel with an incoming webhook

Treat the Grizzly API key, Discord webhook URL, and ntfy topic URL as secrets.
Anyone subscribed to either notification destination can read the purchased number
and verification code.

## Discord Webhook

In Discord, create a private channel, then open **Edit Channel > Integrations >
Webhooks** and create a webhook. Copy its URL into `DISCORD_WEBHOOK_URL`. The bot
disables all Discord mentions in its payloads, so SMS content cannot trigger
`@everyone`, roles, or users.

## Optional ntfy Redundancy

Set `NTFY_URL` to an ntfy topic URL to send every startup, number, timeout, and
code notification to both Discord and ntfy. The bot attempts both providers even
when the first succeeds, and treats notification delivery as successful when at
least one provider accepts it.

Notification delivery is at-least-once. If a provider accepts a request but its
response is interrupted, a retry or restart can create a duplicate message. The
bot persists pending number and code notifications and resumes them on the next
run instead of buying another number.

## Local Web Dashboard

The recommended mode is the authenticated local dashboard. With `WEB_UI=true`,
the bot starts idle and does not purchase a number until you click **Get another
number**. Open [http://localhost:8080](http://localhost:8080), sign in with
`UI_PASSWORD`, and use **Cancel this number** when a service rejects an acquired
number. Cancellation is persisted and retried until Grizzly confirms it.

The dashboard binds to host loopback only in Docker. Do not change the Compose
port mapping to a public interface. It provides copy buttons for international
and country-code-free number formats, displays the latest received verification
code, and supports persistent light and dark themes. Codes also continue to use
Discord and optional ntfy. Never delete the SQLite volume to skip an activation
because that can leave the paid activation active at Grizzly.

When a purchase is in progress but no number has been acquired, the dashboard
shows **Searching** rather than **Idle**. Its inventory-search panel shows live
request and no-match counts plus the service, country, maximum price, and allowed
provider IDs sent to Grizzly. An empty provider filter is displayed as **Any
provider**.

### Activation History

New purchases are recorded permanently in the dashboard with their phone number,
purchase time, Grizzly activation ID, purchase price and currency, allowed provider
filter, final outcome, and whether a code arrived. Summary cards show total attempts,
codes received, unsuccessful activations, and gross purchase value per currency.

Number acquisition uses Grizzly's `getNumberV2` response for price metadata while
SMS polling and status changes continue to use the v1 lifecycle. Grizzly does not
return the selected provider ID in its documented purchase response, so the history
shows the configured **Allowed providers** rather than claiming which provider was
used. Tracking starts with purchases made after this update; older activations cannot
be reconstructed with reliable price/provider metadata.

### Auto-Retry

When enabled, the bot automatically cancels an activation if no SMS arrives within
the configured timeout (3 minutes by default), waits a short delay, plays a retry
sound in the browser, and acquires a new number. The loop continues until either:

- A code is successfully received
- You disable the toggle in the dashboard
- A terminal Grizzly error occurs (e.g., `BAD_KEY`)

The retry sound plays only while the dashboard tab is open. Enable it via the
`AUTO_RETRY_ENABLED` environment variable or toggle it at runtime from the
automatic replacement panel in the current activation card.

## Quick Start

```bash
cp .env.example .env
```

Edit `.env`:

```env
GRIZZLY_API_KEY=your_grizzly_api_key
SERVICE=wx
COUNTRY=62
MAX_PRICE=2
PROVIDER_IDS=311,415

DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_MAX_RETRIES=5
NTFY_URL=https://ntfy.sh/your-private-topic
NTFY_MAX_RETRIES=5

WEB_UI=true
UI_PASSWORD=replace_with_a_long_local_password
WEB_UI_PORT=8080

AUTO_RETRY_ENABLED=false
AUTO_RETRY_TIMEOUT_SECONDS=180
AUTO_RETRY_DELAY_SECONDS=5
AUTO_RETRY_SOUND_LEAD_SECONDS=5

MAX_REQUESTS_PER_SECOND=2
REQUEST_TIMEOUT_SECONDS=10
STATUS_EVERY_REQUESTS=100
SMS_POLL_SECONDS=5
ACTIVATION_TIMEOUT_SECONDS=900
LOG_LEVEL=INFO
```

Start the dashboard:

```bash
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080). The container deliberately
has no restart policy. An unfinished activation is resumed after a manual restart,
while a fresh purchase always requires an explicit dashboard confirmation.

Watch a running container from another terminal:

```bash
docker compose logs -f --tail=100
```

Stop a run:

```bash
docker compose down
```

## Lifecycle

1. Poll Grizzly until one number is available.
2. Persist the activation ID and number before notifying the configured providers.
3. Send the number and activation ID to Discord and, when configured, ntfy.
4. Mark the activation ready and poll Grizzly for the SMS code.
5. Send the code to the configured providers without logging it locally.
6. Complete the activation and exit.

The bot cancels an activation after `ACTIVATION_TIMEOUT_SECONDS`. A state database
inside the named Docker volume preserves unfinished activations across an
interruption. `STATE_DB_PATH` defaults to `/data/grizzlysms.db` in Docker and can
be overridden in `.env`.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `GRIZZLY_API_KEY` | yes | Grizzly API key. |
| `SERVICE` | yes | Grizzly service code, such as `wx` for Apple. |
| `COUNTRY` | yes | Grizzly country code; `any` is also supported by Grizzly. |
| `MAX_PRICE` | yes | Maximum activation price. |
| `PROVIDER_IDS` | no | Comma-separated provider IDs; omit when empty. |
| `DISCORD_WEBHOOK_URL` | yes | Private Discord incoming-webhook URL. |
| `DISCORD_MAX_RETRIES` | no | Notification attempts for network, 429, and 5xx errors. Defaults to `5`. |
| `NTFY_URL` | no | ntfy topic URL for redundant delivery alongside Discord. |
| `NTFY_MAX_RETRIES` | no | ntfy notification attempts for network, 429, and 5xx errors. Defaults to `5`. |
| `MAX_REQUESTS_PER_SECOND` | yes | Maximum number-acquisition request rate. |
| `REQUEST_TIMEOUT_SECONDS` | yes | HTTP request timeout. |
| `STATUS_EVERY_REQUESTS` | no | Acquisition progress frequency after the first request. Progress appears in Docker logs and the dashboard activity feed. Defaults to `100`. |
| `SMS_POLL_SECONDS` | no | Delay between SMS-status polls. Defaults to `5`. |
| `ACTIVATION_TIMEOUT_SECONDS` | no | Maximum wait for an SMS. Defaults to `900`. |
| `STATE_DB_PATH` | no | SQLite state path. Docker defaults to `/data/grizzlysms.db`. |
| `LOG_LEVEL` | no | Python logging level, such as `INFO` or `DEBUG`. |
| `DEBUG_LOGS` | no | Enable additional application diagnostics. Defaults to `false`. |
| `WEB_REQUEST_LOGS` | no | Log every dashboard HTTP request. Defaults to `false`. |
| `GRIZZLY_API_URL` | no | Override the Grizzly endpoint for testing. |
| `WEB_UI` | no | Enable the local dashboard and explicit-purchase mode. Defaults to `false`. |
| `UI_PASSWORD` | with Web UI | Password required to access the dashboard. |
| `WEB_UI_PORT` | no | Host loopback port for the dashboard. Defaults to `8080`. |
| `AUTO_RETRY_ENABLED` | no | Enable automatic retry after timeout. Defaults to `false`. |
| `AUTO_RETRY_TIMEOUT_SECONDS` | no | Timeout before auto-retry cancels. Defaults to `180`. |
| `AUTO_RETRY_DELAY_SECONDS` | no | Delay between cancellation and next attempt. Defaults to `5`. |
| `AUTO_RETRY_SOUND_LEAD_SECONDS` | no | Maximum wait for the browser retry alert before the next purchase. Defaults to `5`. |

Set `WEB_UI=false` to retain headless behavior: the process immediately looks for
one number, follows its lifecycle, and exits after completion or failure.

The dashboard polls its status endpoint every two seconds. Request logging is off
by default to keep this routine traffic out of Docker logs. Set
`WEB_REQUEST_LOGS=true` when debugging HTTP behavior, and `DEBUG_LOGS=true` for
additional application diagnostics.

## Running Without Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a
source .env
set +a
STATE_DB_PATH=./grizzlysms.db python bot.py
```

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
```

## Scope

Use this tool only where you are authorized to receive the relevant SMS messages
and in accordance with both Grizzly's terms and the target service's terms.
