# One-Shot SMS Activation Plan

## Goal

Acquire one Grizzly SMS number, notify through Discord, wait for the SMS code,
deliver that code once, finalize the activation, and exit. The bot must not
silently purchase another number.

## Lifecycle

1. On startup, load any persisted activation state.
2. If an activation is waiting for a code, resume polling it instead of buying
   another number.
3. Otherwise, poll Grizzly for one number and persist its activation ID before
   notification delivery.
4. Notify Discord with the acquired phone number and activation ID.
5. Mark the activation ready when required by the Grizzly-compatible API, then
   poll its status until a code, terminal status, or configured timeout.
6. On a code, notify Discord once, complete the activation, record the terminal
   state, and exit.
7. On timeout or a terminal API result, cancel or record the activation as
   failed, notify Discord where possible, and exit.

## Reliability And Safety

- Persist the active activation in a SQLite database mounted outside the
  container so restarts resume the same activation.
- Send at most one acquisition per run and never acquire while a persisted
  activation is active or terminal.
- Treat the Discord webhook as a secret; use a private channel and suppress
  mentions in payloads.
- Retry transient Discord delivery errors, including rate limits, without
  duplicate success notifications.
- Do not log SMS codes.
- Validate API responses and stop on terminal configuration and balance errors.

## Configuration

- `DISCORD_WEBHOOK_URL` is required for notifications.
- SMS polling interval and activation timeout are configurable.
- The state database path is configurable and backed by a Docker volume.

## Verification

- Unit-test parsing, persisted-state recovery, lifecycle outcomes, and Discord
  retry behavior with mocked HTTP responses.
- Run syntax checks and the test suite before each commit.
