#!/usr/bin/env python3
"""
UniFi Protect → Govee WiFi Light Alarm Service

Listens for webhooks from UniFi Protect and activates up to two Govee WiFi
lights when a person/animal/vehicle is detected. Automatically restores lights
to their previous state after a configurable timeout.

Port: 8585 (configurable via WEBHOOK_PORT env var)

Supports both the Govee cloud HTTP API and the local LAN UDP API.
LAN API is preferred when device IPs are configured (faster, no rate limits).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

# ─── Version / constants ──────────────────────────────────────────────────────
VERSION         = "1.0.0"
GOVEE_API_BASE  = "https://developer-api.govee.com/v1"
REQUEST_TIMEOUT = 15       # HTTP request timeout (seconds)
MAX_LOG_ENTRIES = 50       # in-memory activity log size
CLOUD_MIN_GAP   = 1.15     # minimum seconds between cloud API calls (rate limit)
LAN_UDP_PORT    = 4003     # Govee LAN control UDP port
LAN_TIMEOUT     = 2.0      # UDP response wait timeout (seconds)

# Alarm FSM states
IDLE      = "idle"
ALARMED   = "alarmed"
RESTORING = "restoring"

# ─── Effects ─────────────────────────────────────────────────────────────────
#
# type "static" — set colour once and hold
# type "blink"  — toggle on/off at configured interval
# type "cycle"  — rotate through a list of colours
#
# NOTE: Cloud API rate limits mean cloud-only setups see a minimum of ~2.3 s
# per animation step for two devices (1.15 s × 2 calls). Configure
# GOVEE_DEVICE1_IP / GOVEE_DEVICE2_IP to use the local LAN API for fast
# strobing effects.

EFFECTS: Dict[str, dict] = {
    # ── Static ────────────────────────────────────────────────────────────────
    "white": {
        "label": "Solid White",
        "type": "static",
        "color": (255, 255, 255),
        "brightness": 100,
    },
    "red": {
        "label": "Solid Red",
        "type": "static",
        "color": (255, 0, 0),
        "brightness": 100,
    },
    "blue": {
        "label": "Solid Blue",
        "type": "static",
        "color": (0, 0, 255),
        "brightness": 100,
    },
    "green": {
        "label": "Solid Green",
        "type": "static",
        "color": (0, 255, 0),
        "brightness": 100,
    },
    "amber": {
        "label": "Solid Amber",
        "type": "static",
        "color": (255, 191, 0),
        "brightness": 100,
    },
    "purple": {
        "label": "Solid Purple",
        "type": "static",
        "color": (148, 0, 211),
        "brightness": 100,
    },
    "cyan": {
        "label": "Solid Cyan",
        "type": "static",
        "color": (0, 255, 255),
        "brightness": 100,
    },
    "warm-white": {
        "label": "Warm White",
        "type": "static",
        "color": (255, 200, 80),
        "brightness": 80,
    },
    # ── Blink / strobe ────────────────────────────────────────────────────────
    "red-strobe": {
        "label": "Red Strobe",
        "type": "blink",
        "color": (255, 0, 0),
        "on_seconds": 0.4,
        "off_seconds": 0.4,
        "brightness": 100,
    },
    "blue-strobe": {
        "label": "Blue Strobe",
        "type": "blink",
        "color": (0, 0, 255),
        "on_seconds": 0.4,
        "off_seconds": 0.4,
        "brightness": 100,
    },
    "white-strobe": {
        "label": "White Strobe",
        "type": "blink",
        "color": (255, 255, 255),
        "on_seconds": 0.3,
        "off_seconds": 0.3,
        "brightness": 100,
    },
    "amber-flash": {
        "label": "Amber Flash",
        "type": "blink",
        "color": (255, 191, 0),
        "on_seconds": 0.8,
        "off_seconds": 0.4,
        "brightness": 100,
    },
    "slow-red-blink": {
        "label": "Slow Red Blink",
        "type": "blink",
        "color": (255, 0, 0),
        "on_seconds": 1.5,
        "off_seconds": 1.5,
        "brightness": 100,
    },
    # ── Colour cycle ──────────────────────────────────────────────────────────
    "red-blue-strobe": {
        "label": "Red/Blue Strobe",
        "type": "cycle",
        "colors": [(255, 0, 0), (0, 0, 255)],
        "interval": 0.4,
        "brightness": 100,
    },
    "police": {
        "label": "Police Flash",
        "type": "cycle",
        "colors": [
            (255, 0, 0), (255, 0, 0),
            (0, 0, 255), (0, 0, 255),
        ],
        "interval": 0.25,
        "brightness": 100,
    },
    "alarm-red-white": {
        "label": "Red/White Alarm",
        "type": "cycle",
        "colors": [(255, 0, 0), (255, 255, 255)],
        "interval": 0.6,
        "brightness": 100,
    },
    "rgb-cycle": {
        "label": "RGB Cycle",
        "type": "cycle",
        "colors": [(255, 0, 0), (0, 255, 0), (0, 0, 255)],
        "interval": 1.5,
        "brightness": 100,
    },
}


# ─── Exceptions ───────────────────────────────────────────────────────────────
class APIError(Exception):
    pass


# ─── Configuration ────────────────────────────────────────────────────────────
class Config:
    """All settings sourced from environment variables."""

    def __init__(self) -> None:
        self.api_key        = os.environ.get("GOVEE_API_KEY", "")

        self.device1_id     = os.environ.get("GOVEE_DEVICE1_ID", "")
        self.device1_model  = os.environ.get("GOVEE_DEVICE1_MODEL", "")
        self.device1_ip     = os.environ.get("GOVEE_DEVICE1_IP", "")   # optional LAN
        self.device1_label  = os.environ.get("GOVEE_DEVICE1_LABEL", "Light 1")

        self.device2_id     = os.environ.get("GOVEE_DEVICE2_ID", "")
        self.device2_model  = os.environ.get("GOVEE_DEVICE2_MODEL", "")
        self.device2_ip     = os.environ.get("GOVEE_DEVICE2_IP", "")   # optional LAN
        self.device2_label  = os.environ.get("GOVEE_DEVICE2_LABEL", "Light 2")

        self.webhook_port   = int(os.environ.get("WEBHOOK_PORT", "8585"))
        self.alarm_timeout  = int(os.environ.get("ALARM_TIMEOUT", "30"))
        self.default_effect = os.environ.get("DEFAULT_EFFECT", "white")
        self.log_level      = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.log_file       = os.environ.get("LOG_FILE", "/app/logs/govee_alarm.log")

    def validate(self) -> None:
        errors: List[str] = []
        if not self.api_key:
            errors.append("GOVEE_API_KEY is required")
        if not self.device1_id:
            errors.append("GOVEE_DEVICE1_ID is required")
        if not self.device1_model:
            errors.append("GOVEE_DEVICE1_MODEL is required")
        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))
        if self.alarm_timeout < 1:
            self.alarm_timeout = 1
        if self.default_effect not in EFFECTS:
            self.default_effect = "white"

    @property
    def devices(self) -> List[dict]:
        devs = [dict(
            id=self.device1_id, model=self.device1_model,
            ip=self.device1_ip, label=self.device1_label,
        )]
        if self.device2_id and self.device2_model:
            devs.append(dict(
                id=self.device2_id, model=self.device2_model,
                ip=self.device2_ip, label=self.device2_label,
            ))
        return devs


# ─── Device state snapshot ───────────────────────────────────────────────────
class DeviceState:
    """Snapshot of a Govee device's state, used for restoration."""
    __slots__ = ("power_on", "brightness", "r", "g", "b")

    def __init__(self, power_on: bool = True, brightness: int = 100,
                 r: int = 255, g: int = 255, b: int = 255) -> None:
        self.power_on   = power_on
        self.brightness = brightness
        self.r, self.g, self.b = r, g, b

    def __repr__(self) -> str:
        return (f"DeviceState(power={'on' if self.power_on else 'off'}, "
                f"brightness={self.brightness}, rgb=({self.r},{self.g},{self.b}))")


# ─── Govee Cloud HTTP API ─────────────────────────────────────────────────────
class GoveeCloudClient:
    """
    Wraps the Govee developer HTTP API.
    Enforces a global rate-limit of 1 call per CLOUD_MIN_GAP seconds because
    the free tier allows ~100 req/minute across the account.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._last    = 0.0
        self._lock    = threading.Lock()

    def _throttle(self) -> None:
        with self._lock:
            wait = CLOUD_MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _req(self, method: str, path: str,
             body: Optional[dict] = None,
             params: Optional[Dict[str, str]] = None) -> dict:
        self._throttle()
        url = GOVEE_API_BASE + path
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        data = json.dumps(body).encode() if body else None
        req = Request(url, data=data, method=method)
        req.add_header("Govee-API-Key", self._api_key)
        if body:
            req.add_header("Content-Type", "application/json")
        log.debug("Cloud %s %s  body=%s", method, path, body)
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode()
            result = json.loads(raw)
            log.debug("Cloud response: %s", json.dumps(result, separators=(",", ":")))
            code = result.get("code", result.get("status"))
            if code not in (200, "ok"):
                raise APIError(
                    f"Govee error {code}: {result.get('message', 'unknown')}"
                )
            return result
        except HTTPError as exc:
            raise APIError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}")
        except URLError as exc:
            raise APIError(f"Request failed: {exc.reason}")
        except json.JSONDecodeError as exc:
            raise APIError(f"Bad JSON: {exc}")

    def get_state(self, device_id: str, model: str) -> DeviceState:
        result = self._req("GET", "/devices/state",
                           params={"device": device_id, "model": model})
        props: Dict[str, Any] = {}
        for item in result.get("data", {}).get("properties", []):
            props.update(item)
        color = props.get("color", {"r": 255, "g": 255, "b": 255})
        return DeviceState(
            power_on   = props.get("powerState", "on") == "on",
            brightness = int(props.get("brightness", 100)),
            r = int(color.get("r", 255)),
            g = int(color.get("g", 255)),
            b = int(color.get("b", 255)),
        )

    def _cmd(self, device_id: str, model: str, name: str, value: Any) -> None:
        self._req("PUT", "/devices/control", body={
            "device": device_id,
            "model":  model,
            "cmd":    {"name": name, "value": value},
        })

    def power(self, device_id: str, model: str, on: bool) -> None:
        self._cmd(device_id, model, "turn", "on" if on else "off")

    def brightness(self, device_id: str, model: str, value: int) -> None:
        self._cmd(device_id, model, "brightness", max(1, min(100, value)))

    def color(self, device_id: str, model: str, r: int, g: int, b: int) -> None:
        self._cmd(device_id, model, "color", {"r": r, "g": g, "b": b})


# ─── Govee LAN UDP API ────────────────────────────────────────────────────────
class GoveeLANClient:
    """
    Controls Govee devices on the local network via UDP.
    No rate limits; suitable for fast animations.
    Requires 'LAN Control' to be enabled in the Govee app.
    """

    @staticmethod
    def _send(ip: str, cmd: str, data: dict) -> None:
        msg = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(msg, (ip, LAN_UDP_PORT))
        log.debug("LAN %s %s  data=%s", ip, cmd, data)

    @staticmethod
    def get_state(ip: str) -> Optional[DeviceState]:
        msg = json.dumps({"msg": {"cmd": "devStatus", "data": {}}}).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(LAN_TIMEOUT)
            try:
                s.sendto(msg, (ip, LAN_UDP_PORT))
                raw, _ = s.recvfrom(4096)
                d = json.loads(raw).get("msg", {}).get("data", {})
                color = d.get("color", {"r": 255, "g": 255, "b": 255})
                return DeviceState(
                    power_on   = d.get("onOff", 1) == 1,
                    brightness = int(d.get("brightness", 100)),
                    r = int(color.get("r", 255)),
                    g = int(color.get("g", 255)),
                    b = int(color.get("b", 255)),
                )
            except (socket.timeout, json.JSONDecodeError, OSError):
                return None

    @staticmethod
    def power(ip: str, on: bool) -> None:
        GoveeLANClient._send(ip, "turn", {"value": 1 if on else 0})

    @staticmethod
    def brightness(ip: str, value: int) -> None:
        GoveeLANClient._send(ip, "brightness", {"value": max(1, min(100, value))})

    @staticmethod
    def color(ip: str, r: int, g: int, b: int) -> None:
        GoveeLANClient._send(ip, "colorwc", {
            "color": {"r": r, "g": g, "b": b},
            "colorTemInKelvin": 0,
        })


_LAN = GoveeLANClient()   # stateless singleton


# ─── Per-device unified controller ───────────────────────────────────────────
class GoveeDevice:
    """
    Unified controller for a single Govee device.
    Uses LAN API when an IP is configured, cloud API otherwise.
    """

    def __init__(self, device_id: str, model: str, label: str,
                 cloud: GoveeCloudClient, lan_ip: str = "") -> None:
        self.id      = device_id
        self.model   = model
        self.label   = label
        self._cloud  = cloud
        self._lan_ip = lan_ip
        self.use_lan = bool(lan_ip)

    @property
    def api_mode(self) -> str:
        return "LAN" if self.use_lan else "Cloud"

    def get_state(self) -> Optional[DeviceState]:
        try:
            if self.use_lan:
                s = _LAN.get_state(self._lan_ip)
                if s is not None:
                    return s
                log.warning("LAN state query failed for %s — falling back to cloud", self.label)
            return self._cloud.get_state(self.id, self.model)
        except APIError as exc:
            log.warning("State query failed for %s: %s", self.label, exc)
            return None

    def power(self, on: bool) -> None:
        log.info("%s → power %s", self.label, "on" if on else "off")
        if self.use_lan:
            _LAN.power(self._lan_ip, on)
        else:
            self._cloud.power(self.id, self.model, on)

    def brightness(self, value: int) -> None:
        log.debug("%s → brightness %d%%", self.label, value)
        if self.use_lan:
            _LAN.brightness(self._lan_ip, value)
        else:
            self._cloud.brightness(self.id, self.model, value)

    def color(self, r: int, g: int, b: int) -> None:
        log.debug("%s → color (%d,%d,%d)", self.label, r, g, b)
        if self.use_lan:
            _LAN.color(self._lan_ip, r, g, b)
        else:
            self._cloud.color(self.id, self.model, r, g, b)

    def apply_color(self, r: int, g: int, b: int, br: int = 100) -> None:
        """Turn on, set brightness, set colour. LAN calls need a small gap."""
        self.power(True)
        gap = 0.05 if self.use_lan else 0.0  # cloud already throttles globally
        if gap:
            time.sleep(gap)
        self.brightness(br)
        if gap:
            time.sleep(gap)
        self.color(r, g, b)

    def restore(self, state: DeviceState) -> None:
        log.info("Restoring %s → %s", self.label, state)
        if not state.power_on:
            self.power(False)
            return
        self.power(True)
        gap = 0.05 if self.use_lan else 0.0
        if gap:
            time.sleep(gap)
        self.brightness(state.brightness)
        if gap:
            time.sleep(gap)
        self.color(state.r, state.g, state.b)


# ─── Alarm state machine ──────────────────────────────────────────────────────
class AlarmStateMachine:
    """
    Manages alarm state for all configured Govee devices.

    FSM:
        IDLE ──(trigger)──► ALARMED ──(timeout)──► RESTORING ──(done)──► IDLE
                                ▲                                            │
                                └──────────(retrigger after restore)─────────┘
    """

    def __init__(self, config: Config, devices: List[GoveeDevice]) -> None:
        self.config  = config
        self.devices = devices

        self._lock          = threading.RLock()
        self._state         = IDLE
        self._current_effect = ""
        self._saved_states: Dict[str, DeviceState] = {}  # device.id → state

        self._timer: Optional[threading.Timer] = None
        self._anim_stop = threading.Event()

        # Activity log: list of (iso_timestamp, level, message) newest-first
        self.activity_log: List[Tuple[str, str, str]] = []
        self.triggered_at:  Optional[str] = None
        self.restored_at:   Optional[str] = None
        self.trigger_count: int = 0

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, level: str, msg: str, *args: Any) -> None:
        text = msg % args if args else msg
        log.log(getattr(logging, level.upper(), logging.INFO), text)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self.activity_log.insert(0, (ts, level.lower(), text))
            if len(self.activity_log) > MAX_LOG_ENTRIES:
                self.activity_log.pop()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_effect(self) -> str:
        return self._current_effect

    def trigger(self, effect_name: str) -> None:
        """Handle an incoming alarm trigger (safe to call from any thread)."""
        if effect_name not in EFFECTS:
            self._log("warning", "Unknown effect '%s' — using default '%s'",
                      effect_name, self.config.default_effect)
            effect_name = self.config.default_effect

        with self._lock:
            if self._state == IDLE:
                self._state = ALARMED
                self._current_effect = effect_name
                self.trigger_count += 1
                self.triggered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.restored_at  = None
                self._log("info", "Alarm triggered — %s", EFFECTS[effect_name]["label"])
                threading.Thread(target=self._activate, args=(effect_name,), daemon=True).start()

            elif self._state == ALARMED:
                if effect_name == self._current_effect:
                    self._log("info", "Alarm active (%s) — resetting timer",
                              EFFECTS[effect_name]["label"])
                    self._reset_timer()
                else:
                    self._log("info", "Override: %s → %s",
                              EFFECTS[self._current_effect]["label"],
                              EFFECTS[effect_name]["label"])
                    self._current_effect = effect_name
                    self._anim_stop.set()
                    threading.Thread(
                        target=self._apply_and_reset_timer,
                        args=(effect_name,), daemon=True,
                    ).start()

            elif self._state == RESTORING:
                self._log("info", "Queuing retrigger (%s) — currently restoring",
                          EFFECTS[effect_name]["label"])
                threading.Thread(
                    target=self._wait_and_retrigger,
                    args=(effect_name,), daemon=True,
                ).start()

    def status(self) -> dict:
        with self._lock:
            return {
                "state":                self._state,
                "current_effect":       self._current_effect,
                "current_effect_label": EFFECTS.get(self._current_effect, {}).get("label", ""),
                "alarm_timeout":        self.config.alarm_timeout,
                "triggered_at":         self.triggered_at,
                "restored_at":          self.restored_at,
                "trigger_count":        self.trigger_count,
                "log":                  list(self.activity_log[:20]),
                "devices": [
                    {"id": d.id, "label": d.label, "mode": d.api_mode}
                    for d in self.devices
                ],
            }

    # ── Internal activation ──────────────────────────────────────────────────

    def _activate(self, effect_name: str) -> None:
        """Save device states, apply effect, start restore timer."""
        for dev in self.devices:
            state = dev.get_state()
            if state is not None:
                self._saved_states[dev.id] = state
                self._log("debug", "Saved state for %s: %s", dev.label, state)
            else:
                self._log("warning", "Could not read state for %s — will turn off on restore",
                          dev.label)
        try:
            self._apply_effect(effect_name)
        except APIError as exc:
            self._log("error", "Failed to activate alarm: %s", exc)
            with self._lock:
                self._state = IDLE
            return
        self._reset_timer()

    def _apply_and_reset_timer(self, effect_name: str) -> None:
        try:
            self._apply_effect(effect_name)
        except APIError as exc:
            self._log("error", "Failed to apply override effect: %s", exc)
        self._reset_timer()

    def _apply_effect(self, effect_name: str) -> None:
        """Apply effect to all devices; spawn animation thread if needed."""
        effect = EFFECTS[effect_name]
        etype  = effect["type"]
        self._anim_stop.clear()

        if etype == "static":
            r, g, b = effect["color"]
            br      = effect.get("brightness", 100)
            self._for_all_devices(lambda dev: dev.apply_color(r, g, b, br))

        elif etype == "blink":
            threading.Thread(target=self._run_blink, args=(effect_name,), daemon=True).start()

        elif etype == "cycle":
            threading.Thread(target=self._run_cycle, args=(effect_name,), daemon=True).start()

    def _for_all_devices(self, fn) -> None:
        """Execute fn(device) for all devices.
        LAN devices are called in parallel; cloud devices are serial (shared throttle)."""
        lan_devs   = [d for d in self.devices if d.use_lan]
        cloud_devs = [d for d in self.devices if not d.use_lan]

        threads = [threading.Thread(target=fn, args=(d,), daemon=True) for d in lan_devs]
        for t in threads:
            t.start()
        for d in cloud_devs:
            fn(d)   # serial — cloud throttle serialises these anyway
        for t in threads:
            t.join()

    # ── Animation threads ────────────────────────────────────────────────────

    def _run_blink(self, effect_name: str) -> None:
        """Toggle device(s) on/off until alarm ends or effect changes."""
        effect  = EFFECTS[effect_name]
        r, g, b = effect["color"]
        br      = effect.get("brightness", 100)
        on_s    = float(effect.get("on_seconds",  0.4))
        off_s   = float(effect.get("off_seconds", 0.4))

        # Cloud API cannot physically strobe faster than ~1.15 s per command.
        # Warn once if requested interval is impractically short.
        all_cloud = all(not d.use_lan for d in self.devices)
        if all_cloud and (on_s < CLOUD_MIN_GAP or off_s < CLOUD_MIN_GAP):
            self._log("warning",
                      "Blink intervals (%.2f s / %.2f s) are shorter than cloud API "
                      "rate limit (%.2f s). Configure LAN IPs for fast strobing.",
                      on_s, off_s, CLOUD_MIN_GAP)

        # Set brightness once; then only toggle power in loop (fewer API calls).
        try:
            self._for_all_devices(lambda dev: dev.apply_color(r, g, b, br))
        except APIError as exc:
            self._log("error", "Blink init failed: %s", exc)
            return

        self._anim_stop.wait(on_s)

        while not self._anim_stop.is_set():
            if self._state != ALARMED or self._current_effect != effect_name:
                break
            # OFF phase
            try:
                self._for_all_devices(lambda dev: dev.power(False))
            except APIError as exc:
                self._log("warning", "Blink off error: %s", exc)
            self._anim_stop.wait(off_s)

            if self._anim_stop.is_set():
                break
            if self._state != ALARMED or self._current_effect != effect_name:
                break
            # ON phase
            try:
                self._for_all_devices(lambda dev: dev.color(r, g, b))
            except APIError as exc:
                self._log("warning", "Blink on error: %s", exc)
            self._anim_stop.wait(on_s)

    def _run_cycle(self, effect_name: str) -> None:
        """Rotate through colour list until alarm ends or effect changes."""
        effect   = EFFECTS[effect_name]
        colors   = effect["colors"]
        br       = effect.get("brightness", 100)
        interval = float(effect.get("interval", 1.0))

        all_cloud = all(not d.use_lan for d in self.devices)
        if all_cloud and interval < CLOUD_MIN_GAP:
            self._log("warning",
                      "Cycle interval (%.2f s) is shorter than cloud API rate limit "
                      "(%.2f s). Configure LAN IPs for fast animations.",
                      interval, CLOUD_MIN_GAP)

        # Initial setup: power on and set brightness once.
        r0, g0, b0 = colors[0]
        try:
            self._for_all_devices(lambda dev: dev.apply_color(r0, g0, b0, br))
        except APIError as exc:
            self._log("error", "Cycle init failed: %s", exc)
            return

        self._anim_stop.wait(interval)
        idx = 1

        while not self._anim_stop.is_set():
            if self._state != ALARMED or self._current_effect != effect_name:
                break
            r, g, b = colors[idx % len(colors)]
            try:
                self._for_all_devices(lambda dev: dev.color(r, g, b))
            except APIError as exc:
                self._log("warning", "Cycle frame error: %s", exc)
            idx += 1
            self._anim_stop.wait(interval)

    # ── Timer / restore ──────────────────────────────────────────────────────

    def _reset_timer(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.config.alarm_timeout, self._begin_restore)
            self._timer.daemon = True
            self._timer.start()

    def _begin_restore(self) -> None:
        with self._lock:
            if self._state != ALARMED:
                return
            self._state = RESTORING
            self._anim_stop.set()
        self._log("info", "Alarm timeout — restoring lights")
        threading.Thread(target=self._restore, daemon=True).start()

    def _restore(self) -> None:
        time.sleep(1.0)   # let animation threads exit
        try:
            for dev in self.devices:
                saved = self._saved_states.get(dev.id)
                if saved is not None:
                    dev.restore(saved)
                else:
                    self._log("warning", "No saved state for %s — turning off", dev.label)
                    try:
                        dev.power(False)
                    except APIError as exc:
                        self._log("error", "Could not turn off %s: %s", dev.label, exc)
        except APIError as exc:
            self._log("error", "Restore failed: %s", exc)

        with self._lock:
            self._state          = IDLE
            self._current_effect = ""
            self._saved_states.clear()
            self.restored_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._log("info", "Lights restored — back to idle")

    def _wait_and_retrigger(self, effect_name: str) -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if self._state == IDLE:
                self.trigger(effect_name)
                return
        self._log("warning", "Retrigger timed out waiting for IDLE")


# ─── HTTP request handler ────────────────────────────────────────────────────
class WebHandler(BaseHTTPRequestHandler):
    """
    HTTP endpoints:

    GET  /             Web UI
    GET  /health       JSON status
    GET  /webhook      Connectivity probe (UniFi Protect tests this)
    POST /webhook      Alarm trigger  (?effect=<name>)
    POST /test         Test trigger from UI (?effect=<name>)
    GET  /logs         Last N lines of log file as plain text
    POST /loglevel     Change log verbosity  body: {"level":"DEBUG"}
    """

    alarm_sm: AlarmStateMachine = None   # set by main()
    config:   Config            = None

    def log_message(self, fmt: str, *args) -> None:  # silence default access log
        log.debug("HTTP %s %s", self.address_string(), fmt % args)

    # ── Routing ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._serve_ui()
        elif path == "/health":
            self._serve_health()
        elif path == "/webhook":
            self._send(200, "text/plain", b"OK")   # UniFi connectivity probe
        elif path == "/logs":
            self._serve_logs()
        else:
            self._send(404, "text/plain", b"Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path in ("/webhook", "/test"):
            self._handle_trigger(parsed)
        elif path == "/loglevel":
            self._handle_loglevel()
        else:
            self._send(404, "text/plain", b"Not Found")

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _handle_trigger(self, parsed) -> None:
        qs     = parse_qs(parsed.query)
        effect = qs.get("effect", [self.config.default_effect])[0]

        # Read body (may be absent)
        length  = int(self.headers.get("Content-Length", 0))
        payload = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                pass

        # Extract trigger keys from UniFi Protect webhook format (optional)
        triggers = []
        try:
            triggers = [t.get("key", "") for t in payload["alarm"]["triggers"]]
        except (KeyError, TypeError):
            pass

        if triggers:
            log.info("Webhook trigger keys=%s effect=%s", triggers, effect)
        else:
            log.info("Webhook trigger effect=%s", effect)

        # Non-blocking: alarm runs in its own thread
        threading.Thread(
            target=self.alarm_sm.trigger,
            args=(effect,),
            daemon=True,
        ).start()

        resp = json.dumps({
            "triggered": True,
            "effect": effect,
            "triggers": triggers,
        }).encode()
        self._send(200, "application/json", resp)

    def _handle_loglevel(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            body  = json.loads(self.rfile.read(length))
            level = body.get("level", "").upper()
            if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                raise ValueError(f"invalid level: {level}")
            logging.getLogger().setLevel(level)
            log.setLevel(level)
            log.info("Log level changed to %s", level)
            resp = json.dumps({"level": level}).encode()
            self._send(200, "application/json", resp)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send(400, "text/plain", str(exc).encode())

    def _serve_health(self) -> None:
        status = self.alarm_sm.status()
        status["status"] = "ok"
        status["version"] = VERSION
        self._send(200, "application/json", json.dumps(status, indent=2).encode())

    def _serve_ui(self) -> None:
        status = self.alarm_sm.status()
        html   = _render_ui(status, self.config)
        self._send(200, "text/html; charset=utf-8", html.encode())

    def _serve_logs(self) -> None:
        qs    = parse_qs(urlparse(self.path).query)
        lines = int(qs.get("lines", ["200"])[0])
        try:
            with open(self.config.log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            tail = "".join(all_lines[-lines:])
            self._send(200, "text/plain; charset=utf-8", tail.encode())
        except FileNotFoundError:
            self._send(200, "text/plain; charset=utf-8", b"(log file not yet created)")
        except OSError as exc:
            self._send(500, "text/plain", str(exc).encode())

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ─── Web UI ───────────────────────────────────────────────────────────────────
def _render_ui(status: dict, config: Config) -> str:
    state         = status["state"]
    effect        = status.get("current_effect", "")
    effect_label  = status.get("current_effect_label", "")
    alarm_timeout = status.get("alarm_timeout", config.alarm_timeout)
    triggered_at  = status.get("triggered_at") or "—"
    restored_at   = status.get("restored_at")  or "—"
    trigger_count = status.get("trigger_count", 0)
    log_entries   = status.get("log", [])
    devices       = status.get("devices", [])

    current_log_level = logging.getLevelName(logging.getLogger().level)

    state_badge_class = {
        IDLE:      "badge-idle",
        ALARMED:   "badge-alarmed",
        RESTORING: "badge-restoring",
    }.get(state, "badge-idle")

    # Effect options for dropdowns
    effect_opts = "\n".join(
        f'<option value="{k}"{" selected" if k == config.default_effect else ""}>'
        f'{v["label"]} ({v["type"]})</option>'
        for k, v in EFFECTS.items()
    )

    # Effects reference table rows
    effects_rows = ""
    for k, v in EFFECTS.items():
        etype = v["type"]
        if etype == "static":
            r, g, b = v["color"]
            swatch = f'<span class="swatch" style="background:rgb({r},{g},{b})"></span>'
            detail = f'RGB({r},{g},{b})'
        elif etype == "blink":
            r, g, b = v["color"]
            swatch = f'<span class="swatch" style="background:rgb({r},{g},{b})"></span>'
            detail = f'on={v.get("on_seconds",0.4):.2f}s off={v.get("off_seconds",0.4):.2f}s'
        else:
            cols = v.get("colors", [])
            swatches = " ".join(
                f'<span class="swatch" style="background:rgb{c}"></span>' for c in cols[:4]
            )
            swatch = swatches
            detail = f'{len(cols)} colours, interval={v.get("interval",1):.2f}s'
        effects_rows += (
            f'<tr><td><code>{k}</code></td><td>{v["label"]}</td>'
            f'<td>{swatch}</td><td>{etype}</td><td>{detail}</td></tr>\n'
        )

    # Webhook URL table rows
    webhook_rows = "\n".join(
        f'<tr><td><code>{k}</code></td>'
        f'<td><code class="url">http://<host>:{config.webhook_port}/webhook?effect={k}</code></td></tr>'
        for k in EFFECTS
    )

    # Activity log rows
    def level_cls(lvl: str) -> str:
        return {"error": "log-error", "warning": "log-warning", "debug": "log-debug"}.get(lvl, "")

    log_rows = ""
    for ts, lvl, msg in log_entries:
        cls = level_cls(lvl)
        log_rows += (
            f'<tr class="{cls}">'
            f'<td class="ts" data-utc="{ts}">{ts}</td>'
            f'<td class="lv">{lvl.upper()}</td>'
            f'<td>{_html_escape(msg)}</td></tr>\n'
        )

    # Device cards
    device_cards = ""
    for d in devices:
        device_cards += (
            f'<div class="dev-card">'
            f'<span class="dev-label">{_html_escape(d["label"])}</span>'
            f'<span class="dev-id"><code>{d["id"][:12]}…</code></span>'
            f'<span class="dev-mode mode-{d["mode"].lower()}">{d["mode"]}</span>'
            f'</div>'
        )

    # Auto-refresh meta if alarmed
    auto_refresh = '<meta http-equiv="refresh" content="5">' if state == ALARMED else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{auto_refresh}
<title>Govee Alarm — UniFi Protect</title>
<style>
  :root {{
    --bg: #0f172a; --card: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #f97316;
    --idle: #22c55e; --alarmed: #ef4444; --restoring: #f59e0b;
    --red: #ef4444; --yellow: #f59e0b; --blue: #60a5fa; --green: #22c55e;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: system-ui, sans-serif;
          font-size: 14px; line-height: 1.6; }}
  header {{ background: var(--card); border-bottom: 1px solid var(--border);
             padding: 1rem 1.5rem; display: flex; align-items: center; gap: 1rem; }}
  header h1 {{ font-size: 1.2rem; color: var(--accent); }}
  header .sub {{ color: var(--muted); font-size: 0.85rem; }}
  main {{ max-width: 1200px; margin: 0 auto; padding: 1.5rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px;
           padding: 1.25rem; }}
  .card h2 {{ font-size: 0.9rem; font-weight: 600; text-transform: uppercase;
              letter-spacing: 0.05em; color: var(--muted); margin-bottom: 1rem; }}
  .kv {{ display: flex; justify-content: space-between; padding: 0.3rem 0;
         border-bottom: 1px solid var(--border); }}
  .kv:last-child {{ border-bottom: none; }}
  .kv .k {{ color: var(--muted); }}
  .kv .v {{ font-weight: 500; }}
  .badge {{ display: inline-block; padding: 0.2rem 0.7rem; border-radius: 999px;
             font-size: 0.78rem; font-weight: 700; text-transform: uppercase; }}
  .badge-idle     {{ background: #14532d; color: var(--idle); }}
  .badge-alarmed  {{ background: #7f1d1d; color: var(--alarmed); animation: pulse 1s infinite; }}
  .badge-restoring {{ background: #78350f; color: var(--restoring); }}
  @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.6 }} }}
  .dev-card {{ background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
               padding: 0.75rem; margin-bottom: 0.5rem; display: flex; gap: 0.75rem;
               align-items: center; flex-wrap: wrap; }}
  .dev-label {{ font-weight: 600; }}
  .dev-id {{ color: var(--muted); font-size: 0.8rem; }}
  .dev-mode {{ font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 4px; }}
  .mode-lan   {{ background: #1e3a5f; color: var(--blue); }}
  .mode-cloud {{ background: #1a2e1a; color: var(--green); }}
  .swatch {{ display: inline-block; width: 14px; height: 14px; border-radius: 3px;
             border: 1px solid #475569; vertical-align: middle; margin-right: 2px; }}
  form {{ display: flex; flex-direction: column; gap: 0.75rem; }}
  select, input {{ background: var(--bg); color: var(--text); border: 1px solid var(--border);
                   border-radius: 6px; padding: 0.45rem 0.75rem; font-size: 0.9rem; }}
  button {{ background: var(--accent); color: #fff; border: none; border-radius: 6px;
            padding: 0.55rem 1.25rem; font-size: 0.9rem; font-weight: 600; cursor: pointer; }}
  button:hover {{ opacity: 0.85; }}
  button.secondary {{ background: var(--card); border: 1px solid var(--border); color: var(--text); }}
  .msg {{ padding: 0.5rem 0.75rem; border-radius: 6px; font-size: 0.85rem; margin-top: 0.5rem; }}
  .msg.ok  {{ background: #14532d; color: var(--idle); }}
  .msg.err {{ background: #7f1d1d; color: var(--alarmed); }}
  .full {{ grid-column: 1 / -1; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ text-align: left; color: var(--muted); font-size: 0.78rem; text-transform: uppercase;
        padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--border); }}
  td {{ padding: 0.4rem 0.5rem; border-bottom: 1px solid #1e293b; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  .url {{ font-size: 0.78rem; color: var(--blue); word-break: break-all; }}
  .ts {{ color: var(--muted); font-size: 0.78rem; white-space: nowrap; }}
  .lv {{ font-size: 0.75rem; font-weight: 700; text-transform: uppercase; white-space: nowrap; }}
  .log-error td   {{ background: #3b0a0a; }}
  .log-warning td {{ background: #2a1a04; }}
  .log-debug td   {{ color: var(--muted); }}
  code {{ font-family: 'Courier New', monospace; }}
  .section-title {{ font-size: 1rem; font-weight: 600; margin: 1.5rem 0 0.75rem; color: var(--accent); }}
</style>
</head>
<body>
<header>
  <div>
    <h1>Govee Alarm ⚡ UniFi Protect</h1>
    <div class="sub">v{VERSION} &nbsp;·&nbsp; port {config.webhook_port} &nbsp;·&nbsp;
      timeout {alarm_timeout}s &nbsp;·&nbsp; {len(config.devices)} device(s)</div>
  </div>
</header>
<main>

<div class="grid">

  <!-- Status -->
  <div class="card">
    <h2>System Status</h2>
    <div class="kv"><span class="k">Alarm state</span>
      <span class="v"><span class="badge {state_badge_class}">{state}</span></span></div>
    <div class="kv"><span class="k">Active effect</span>
      <span class="v">{effect_label or '—'}</span></div>
    <div class="kv"><span class="k">Timeout</span>
      <span class="v">{alarm_timeout} s</span></div>
    <div class="kv"><span class="k">Triggered at</span>
      <span class="v ts" data-utc="{triggered_at}">{triggered_at}</span></div>
    <div class="kv"><span class="k">Restored at</span>
      <span class="v ts" data-utc="{restored_at}">{restored_at}</span></div>
    <div class="kv"><span class="k">Total triggers</span>
      <span class="v">{trigger_count}</span></div>
    <div class="kv"><span class="k">Log verbosity</span>
      <span class="v" id="cur-level">{current_log_level}</span></div>
  </div>

  <!-- Devices -->
  <div class="card">
    <h2>Devices</h2>
    {device_cards}
    <div style="color:var(--muted);font-size:0.8rem;margin-top:0.5rem">
      LAN = local UDP (fast) &nbsp;·&nbsp; Cloud = Govee HTTP API
    </div>
  </div>

  <!-- Test alarm -->
  <div class="card">
    <h2>Test Alarm</h2>
    <form id="test-form">
      <label style="color:var(--muted);font-size:0.82rem">Effect</label>
      <select name="effect">{effect_opts}</select>
      <button type="submit">Trigger Now</button>
    </form>
    <div id="test-msg" style="display:none" class="msg"></div>
  </div>

  <!-- Log verbosity -->
  <div class="card">
    <h2>Log Verbosity</h2>
    <form id="level-form">
      <label style="color:var(--muted);font-size:0.82rem">Level</label>
      <select name="level">
        <option value="DEBUG"{"   selected" if current_log_level=="DEBUG"   else ""}>DEBUG</option>
        <option value="INFO"{"    selected" if current_log_level=="INFO"    else ""}>INFO</option>
        <option value="WARNING"{"  selected" if current_log_level=="WARNING"  else ""}>WARNING</option>
        <option value="ERROR"{"   selected" if current_log_level=="ERROR"   else ""}>ERROR</option>
      </select>
      <button type="submit">Apply</button>
    </form>
    <div id="level-msg" style="display:none" class="msg"></div>
    <div style="color:var(--muted);font-size:0.8rem;margin-top:0.75rem">
      Also set <code>LOG_LEVEL</code> env var for persistent verbosity.
    </div>
  </div>

</div><!-- /grid -->

<p class="section-title">Webhook URLs</p>
<div class="card">
  <p style="color:var(--muted);font-size:0.85rem;margin-bottom:0.75rem">
    In UniFi Protect → Alarm Manager, set method to POST and content-type application/json.
    The <code>&lt;host&gt;</code> placeholder is replaced with your browser's hostname below.
  </p>
  <table>
    <thead><tr><th>Effect</th><th>Webhook URL</th></tr></thead>
    <tbody>{webhook_rows}</tbody>
  </table>
</div>

<p class="section-title">Available Effects</p>
<div class="card">
  <table>
    <thead><tr><th>Key</th><th>Label</th><th>Colour(s)</th><th>Type</th><th>Detail</th></tr></thead>
    <tbody>{effects_rows}</tbody>
  </table>
</div>

<p class="section-title">Activity Log
  <a href="/logs?lines=500" target="_blank"
     style="font-size:0.8rem;color:var(--blue);margin-left:1rem;font-weight:400">
    View full log file ↗</a>
</p>
<div class="card">
  <table>
    <thead><tr><th>Time</th><th>Level</th><th>Message</th></tr></thead>
    <tbody id="log-tbody">{log_rows}</tbody>
  </table>
</div>

</main>
<script>
  // Localise UTC timestamps to browser timezone
  document.querySelectorAll('.ts[data-utc]').forEach(el => {{
    const v = el.dataset.utc;
    if (!v || v === '—') return;
    try {{
      el.textContent = new Date(v).toLocaleString();
    }} catch(_) {{}}
  }});

  // Replace <host> in webhook URLs
  document.querySelectorAll('.url').forEach(el => {{
    el.textContent = el.textContent.replace('<host>', location.hostname);
  }});

  // Test form submission
  document.getElementById('test-form').addEventListener('submit', async e => {{
    e.preventDefault();
    const effect = e.target.effect.value;
    const msgEl  = document.getElementById('test-msg');
    try {{
      const r = await fetch('/test?effect=' + encodeURIComponent(effect), {{method:'POST'}});
      const d = await r.json();
      msgEl.className = 'msg ok';
      msgEl.textContent = 'Triggered: ' + d.effect;
    }} catch(err) {{
      msgEl.className = 'msg err';
      msgEl.textContent = 'Error: ' + err;
    }}
    msgEl.style.display = 'block';
    setTimeout(() => {{ msgEl.style.display='none'; location.reload(); }}, 2000);
  }});

  // Log level form
  document.getElementById('level-form').addEventListener('submit', async e => {{
    e.preventDefault();
    const level  = e.target.level.value;
    const msgEl  = document.getElementById('level-msg');
    try {{
      const r = await fetch('/loglevel', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{level}}),
      }});
      const d = await r.json();
      msgEl.className = 'msg ok';
      msgEl.textContent = 'Log level set to ' + d.level;
      document.getElementById('cur-level').textContent = d.level;
    }} catch(err) {{
      msgEl.className = 'msg err';
      msgEl.textContent = 'Error: ' + err;
    }}
    msgEl.style.display = 'block';
    setTimeout(() => {{ msgEl.style.display='none'; }}, 2500);
  }});
</script>
</body>
</html>"""


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


# ─── Logging setup ────────────────────────────────────────────────────────────
def setup_logging(config: Config) -> None:
    level = getattr(logging, config.log_level, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers: List[logging.Handler] = []

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    handlers.append(ch)

    # Rotating file
    if config.log_file:
        try:
            log_dir = os.path.dirname(config.log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                config.log_file,
                maxBytes=10 * 1024 * 1024,   # 10 MB
                backupCount=5,
                encoding="utf-8",
            )
            fh.setLevel(level)
            fh.setFormatter(fmt)
            handlers.append(fh)
        except OSError as exc:
            print(f"WARNING: Cannot open log file {config.log_file!r}: {exc}",
                  file=sys.stderr)

    logging.basicConfig(level=level, handlers=handlers, force=True)


# ─── Module-level logger (set after setup_logging is called) ──────────────────
log = logging.getLogger("govee")


# ─── Entry point ─────────────────────────────────────────────────────────────
def main() -> None:
    global log

    config = Config()
    setup_logging(config)
    log = logging.getLogger("govee")

    log.info("=" * 60)
    log.info("UniFi Protect → Govee Alarm Service  v%s", VERSION)
    log.info("=" * 60)

    try:
        config.validate()
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)

    log.info("Device 1     : %s (%s)%s",
             config.device1_id, config.device1_model,
             f"  [LAN {config.device1_ip}]" if config.device1_ip else "  [Cloud]")
    if config.device2_id:
        log.info("Device 2     : %s (%s)%s",
                 config.device2_id, config.device2_model,
                 f"  [LAN {config.device2_ip}]" if config.device2_ip else "  [Cloud]")
    log.info("Port         : %d", config.webhook_port)
    log.info("Alarm timeout: %d s", config.alarm_timeout)
    log.info("Default effect: %s", config.default_effect)
    log.info("Log level    : %s", config.log_level)
    log.info("Log file     : %s", config.log_file or "(disabled)")

    # Build device controllers
    cloud = GoveeCloudClient(config.api_key)
    devices = [
        GoveeDevice(d["id"], d["model"], d["label"], cloud, d["ip"])
        for d in config.devices
    ]

    alarm_sm = AlarmStateMachine(config, devices)

    # Wire up HTTP handler
    WebHandler.alarm_sm = alarm_sm
    WebHandler.config   = config

    server = HTTPServer(("0.0.0.0", config.webhook_port), WebHandler)
    server.timeout = 1.0

    def _shutdown(sig: int, _frame) -> None:
        log.info("Signal %d received — shutting down", sig)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Listening on 0.0.0.0:%d", config.webhook_port)
    log.info("Web UI : http://localhost:%d/", config.webhook_port)
    log.info("Health : http://localhost:%d/health", config.webhook_port)
    log.info("-" * 60)

    server.serve_forever()
    log.info("Server stopped")


if __name__ == "__main__":
    main()
