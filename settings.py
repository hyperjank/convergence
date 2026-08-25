#!/usr/bin/env python3
"""Persistent settings for convergence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "app_source_device": None,
    "scrcpy": {
        "turn_screen_off": False,
        "stay_awake": True,
        "resolution": "1920x1080",
        "immersive_poke": True,
    },
    "monitor": {
        "encoder": "auto",
        "host_ip": None,
        "target_type": "android",
        "target_value": None,
        "chromecast_mode": "realtime",
        "stream_format": "1080p30",
        "realtime_queue_depth": 4,
        "telemetry_overlay": False,
        "receiver_jitter_target_ms": None,
        "custom": {
            "webrtc_latency_ms": 1200,
            "queue_time_ms": 12000,
            "bitrate_multiplier": 1.4,
            "keyint": 45,
        },
    },
    "virtual_screen": {
        "output_name": None,
        "width": 1920,
        "height": 1080,
        "refresh_rate": 60,
    },
}


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.home() / ".config" / "convergence" / "config.json")
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._merge({}, DEFAULTS)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._merge({}, DEFAULTS)
        if not isinstance(raw, dict):
            return self._merge({}, DEFAULTS)
        return self._merge(raw, DEFAULTS)

    def _merge(self, source: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, default_val in defaults.items():
            val = source.get(key)
            if isinstance(default_val, dict):
                out[key] = self._merge(val if isinstance(val, dict) else {}, default_val)
            else:
                out[key] = default_val if val is None else val
        for key, val in source.items():
            if key not in out:
                out[key] = val
        return out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def update_scrcpy(self, **kwargs: Any) -> None:
        scrcpy = dict(self.data.get("scrcpy", {}))
        scrcpy.update(kwargs)
        self.data["scrcpy"] = scrcpy
        self.save()

    def update_monitor(self, **kwargs: Any) -> None:
        monitor = dict(self.data.get("monitor", {}))
        monitor.update(kwargs)
        self.data["monitor"] = monitor
        self.save()

    def update_virtual_screen(self, **kwargs: Any) -> None:
        virtual_screen = dict(self.data.get("virtual_screen", {}))
        virtual_screen.update(kwargs)
        self.data["virtual_screen"] = virtual_screen
        self.save()
