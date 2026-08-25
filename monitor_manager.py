#!/usr/bin/env python3
"""Wrapper around monitor streaming controller."""

from __future__ import annotations

import os
import socket

from settings import SettingsStore

try:
    from reandmon2.config import HTTP_PORT
except Exception:
    HTTP_PORT = 8000


class MonitorManager:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self.controller = None

    def start(
        self,
        serial: str | None = None,
        *,
        auto_launch_client: bool = True,
        stream_profile: str = "realtime",
    ) -> None:
        if self.controller is not None:
            return

        monitor_cfg = self.settings.get("monitor", {})
        host_ip = monitor_cfg.get("host_ip")
        if host_ip:
            os.environ["HOST_IP"] = str(host_ip)

        from reandmon2.stream_controller import StreamController

        controller = StreamController(
            adb_serial=serial,
            auto_launch_client=auto_launch_client,
            stream_profile=stream_profile,
            stream_format=str(monitor_cfg.get("stream_format", "1080p30")),
            realtime_queue_depth=monitor_cfg.get("realtime_queue_depth", 4),
            custom_profile=monitor_cfg.get("custom", {}),
            telemetry_overlay=bool(monitor_cfg.get("telemetry_overlay", False)),
            receiver_jitter_target_ms=monitor_cfg.get("receiver_jitter_target_ms"),
            encoder_fallback_callback=lambda _failed, fallback: self.settings.update_monitor(
                encoder=fallback
            ),
        )
        encoder = str(monitor_cfg.get("encoder", "auto"))
        if encoder and encoder != "auto":
            controller.set_encoder(encoder)
        controller.start_stream()
        self.controller = controller

    def stop(self) -> None:
        if self.controller is None:
            return
        try:
            self.controller.stop_stream()
        finally:
            self.controller = None

    def is_running(self) -> bool:
        return self.controller is not None

    def available_encoders(self) -> list[str]:
        if self.controller is not None:
            return self.controller.get_available_encoders()
        from reandmon2.stream_controller import StreamController

        tmp = StreamController()
        try:
            return tmp.get_available_encoders()
        finally:
            try:
                tmp.stop_stream()
            except Exception:
                pass

    def current_encoder_label(self) -> str:
        if self.controller is None:
            monitor_cfg = self.settings.get("monitor", {})
            return str(monitor_cfg.get("encoder", "auto"))
        return self.controller.encoder_override or "auto"

    def set_encoder(self, name: str | None) -> None:
        if self.controller is not None:
            if not self.controller.set_encoder(name):
                raise ValueError(f"Encoder '{name}' is not available")
        self.settings.update_monitor(encoder=(name or "auto"))

    def monitor_url(self) -> str | None:
        monitor_cfg = self.settings.get("monitor", {})
        host_ip = monitor_cfg.get("host_ip")
        if not host_ip:
            host_ip = self._resolve_host_ip()
        if not host_ip:
            return None
        if self.controller is not None:
            try:
                url = self.controller.client_url(host_ip=str(host_ip))
                if url:
                    return url
            except Exception:
                pass
        return f"http://{host_ip}:{HTTP_PORT}"

    def latest_receiver_telemetry(self) -> dict | None:
        if self.controller is None:
            return None
        try:
            return self.controller.latest_receiver_telemetry()
        except Exception:
            return None

    def telemetry_log_path(self) -> str | None:
        if self.controller is None:
            return None
        try:
            return self.controller.telemetry_log_path()
        except Exception:
            return None

    def _resolve_host_ip(self) -> str | None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            sock.close()
            return ip
        except Exception:
            return None
