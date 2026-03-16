# unifiprotect-govee

Listens for webhooks from **UniFi Protect** and activates up to two **Govee WiFi lights** when a person, animal, or vehicle is detected. Automatically restores lights to their previous state after a configurable timeout.

```
UniFi Protect  →  POST /webhook?effect=<name>  →  Govee Light 1
                                                →  Govee Light 2
```

## Features

- **Two independent Govee devices** — both activate simultaneously on alarm
- **17 built-in effects** — solid colours, strobe/blink, and colour-cycle animations
- **Cloud + LAN API** — use the Govee cloud API out of the box, or configure local IPs for fast strobing (no rate limits)
- **Auto-restore** — saves power/brightness/colour before alarm; restores after timeout
- **Web UI** at `:8585/` — status, test triggers, webhook URL reference, activity log
- **File + console logging** with runtime verbosity control
- **Zero Python dependencies** — stdlib only; runs in a tiny Docker container

---

## Quick Start

### 1. Get a Govee API Key

Sign in at <https://developer.govee.com> → Apply for API key.

### 2. Find your Device IDs

```bash
curl -H "Govee-API-Key: YOUR_KEY" https://developer-api.govee.com/v1/devices
```

Note the `device` (e.g. `AB:CD:EF:01:02:03:04:05`) and `model` (e.g. `H6160`) for each light.

### 3. Configure

```bash
cp .env.example .env
# edit .env
```

Minimum required settings:

```dotenv
GOVEE_API_KEY=your-api-key-here
GOVEE_DEVICE1_ID=AB:CD:EF:01:02:03:04:05
GOVEE_DEVICE1_MODEL=H6160
```

### 4. Run

```bash
docker compose up -d
```

Web UI → **http://localhost:8585/**

---

## Configuration

All settings are environment variables (see `.env.example`).

| Variable | Default | Description |
|----------|---------|-------------|
| `GOVEE_API_KEY` | — | **Required.** Govee developer API key |
| `GOVEE_DEVICE1_ID` | — | **Required.** First device MAC-style ID |
| `GOVEE_DEVICE1_MODEL` | — | **Required.** First device model string |
| `GOVEE_DEVICE1_LABEL` | `Light 1` | Display name in UI |
| `GOVEE_DEVICE1_IP` | _(empty)_ | Local IP for LAN mode (see below) |
| `GOVEE_DEVICE2_ID` | _(empty)_ | Second device ID (optional) |
| `GOVEE_DEVICE2_MODEL` | _(empty)_ | Second device model (optional) |
| `GOVEE_DEVICE2_LABEL` | `Light 2` | Display name in UI |
| `GOVEE_DEVICE2_IP` | _(empty)_ | Local IP for LAN mode (optional) |
| `WEBHOOK_PORT` | `8585` | HTTP listener port |
| `ALARM_TIMEOUT` | `30` | Seconds before auto-restore after a real alarm |
| `TEST_DURATION` | `5` | Seconds a single-device test holds before restoring |
| `DEFAULT_EFFECT` | `white` | Effect used when no `?effect=` param given |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `LOG_FILE` | `/app/logs/govee_alarm.log` | Log file path (mounted to `./logs/`) |

---

## LAN Mode (recommended for fast animations)

The Govee cloud API is rate-limited to ~100 requests/minute. This makes fast strobing impractical (minimum ~1.15 s per API call). For true strobing effects:

1. Open the **Govee Home** app → select device → Settings → **LAN Control** → enable
2. Find the device's local IP (your router's DHCP table or the Govee app)
3. Set `GOVEE_DEVICE1_IP=192.168.1.xxx` in `.env`

With LAN mode, animation intervals down to ~50 ms work reliably.

---

## UniFi Protect Setup

In Protect → **Alarm Manager**, create alarms with webhook URLs:

| Detection | Webhook URL | Suggested effect |
|-----------|-------------|-----------------|
| Person | `http://<host>:8585/webhook?effect=white` | Solid white |
| Vehicle | `http://<host>:8585/webhook?effect=red-strobe` | Red strobe |
| Animal | `http://<host>:8585/webhook?effect=amber-flash` | Amber flash |
| Intruder | `http://<host>:8585/webhook?effect=police` | Police flash |

Set method to **POST**, content-type **application/json**.

The web UI at `:8585/` shows the full list of URLs with your hostname substituted in.

---

## Effects Reference

### Static (set once, hold)

| Key | Colour |
|-----|--------|
| `white` | White |
| `red` | Red |
| `blue` | Blue |
| `green` | Green |
| `amber` | Amber |
| `purple` | Purple |
| `cyan` | Cyan |
| `warm-white` | Warm white (80% brightness) |

### Blink / Strobe

| Key | Colour | On | Off |
|-----|--------|----|-----|
| `red-strobe` | Red | 0.4 s | 0.4 s |
| `blue-strobe` | Blue | 0.4 s | 0.4 s |
| `white-strobe` | White | 0.3 s | 0.3 s |
| `amber-flash` | Amber | 0.8 s | 0.4 s |
| `slow-red-blink` | Red | 1.5 s | 1.5 s |

### Colour Cycle

| Key | Colours | Interval |
|-----|---------|----------|
| `red-blue-strobe` | Red → Blue | 0.4 s |
| `police` | Red ×2 → Blue ×2 | 0.25 s |
| `alarm-red-white` | Red → White | 0.6 s |
| `rgb-cycle` | Red → Green → Blue | 1.5 s |

> **Cloud API note:** Blink/cycle intervals shorter than 1.15 s are not achievable via the cloud API. Configure `GOVEE_DEVICE_IP` for fast effects.

---

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | JSON status (polled every 5 s by the UI) |
| `GET` | `/webhook` | Connectivity probe (returns 200 OK) |
| `POST` | `/webhook?effect=<name>` | Trigger alarm on all devices |
| `POST` | `/test?effect=<name>` | Test all devices (full alarm sequence) |
| `POST` | `/test?effect=<name>&device=0` | Test one device (by index) |
| `POST` | `/test-device?device=0&effect=<name>` | Test one device directly |
| `GET` | `/govee-devices` | List devices registered to your API key |
| `GET` | `/logs?lines=200` | Tail of log file as plain text |
| `POST` | `/loglevel` | Change verbosity at runtime |

### Change log level at runtime

```bash
curl -X POST http://localhost:8585/loglevel \
  -H "Content-Type: application/json" \
  -d '{"level":"DEBUG"}'
```

### Simulate a person detection

```bash
curl -X POST "http://localhost:8585/webhook?effect=white" \
  -H "Content-Type: application/json" \
  -d '{"alarm":{"triggers":[{"key":"person","device":"camera1"}]}}'
```

---

## Logs

- **Console** — always on, respects `LOG_LEVEL`
- **File** — `./logs/govee_alarm.log` (10 MB rotating, 5 backups)
- **Web UI** — last 50 entries with timestamps and severity badges
- **Runtime** — change level via UI dropdown or `POST /loglevel`

---

## Development

Run directly without Docker:

```bash
export GOVEE_API_KEY=your-key
export GOVEE_DEVICE1_ID=AB:CD:EF:01:02:03:04:05
export GOVEE_DEVICE1_MODEL=H6160
export LOG_FILE=''   # disable file logging
python3 govee_alarm.py
```

---

## Troubleshooting

### "Device Not Found" errors

The Govee developer API uses a different device ID format than the MAC address printed on the device. The correct IDs are returned by the API itself:

1. Click **"List all Govee devices"** on the web UI, or call `GET /govee-devices`
2. Note the `device` and `model` fields for each light
3. Update `.env` with the correct values and restart: `docker compose down && docker compose up -d`

### Tests show no result / activity log doesn't update

The activity log in the UI polls `/health` every 5 seconds automatically. After clicking a test button, wait up to 5 seconds for the result to appear. If the API call fails the error appears in the activity log.

### API rate limits and slow animations

The Govee cloud API allows ~100 requests/minute. Each API command (power, brightness, colour) counts separately, and the service enforces a minimum 1.15 s gap between calls. For fast strobe/cycle effects, enable LAN mode by setting `GOVEE_DEVICE1_IP`.

## Architecture

Single-file Python application (`govee_alarm.py`, stdlib only):

```
GoveeCloudClient   HTTP REST to developer-api.govee.com  (rate-limited)
GoveeLANClient     UDP to device local IP                (no rate limit)
GoveeDevice        Wraps one device; chooses API by IP config
AlarmStateMachine  IDLE → ALARMED → RESTORING FSM
WebHandler         HTTP server (webhooks, UI, health, logs)
```

State machine:

```
IDLE ──(trigger)──► ALARMED ──(timeout)──► RESTORING ──(done)──► IDLE
                        ▲
                        └── (retrigger: same effect resets timer,
                                        new effect overrides immediately)
```
