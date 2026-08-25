#!/usr/bin/env python3
"""Frontend-neutral application controller for convergence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from cast_manager import CastDevice, ChromecastManager
from device_manager import DeviceInfo, DeviceManager
from monitor_manager import MonitorManager
from scrcpy_manager import ScrcpyManager
from settings import SettingsStore
from virtual_screen_manager import VirtualScreenManager


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: int
    serial: str
    mode: str
    package: str | None
    display_id: int | None
    started_at: float
    mismatch_count: int
    last_top_pkg: str | None
    has_seen_target: bool
    auto_close_state: str


@dataclass(frozen=True)
class AppSnapshot:
    app_source_serial: str | None
    monitor_target_type: str
    monitor_target_value: str | None
    monitor_running: bool
    monitor_url: str | None
    virtual_screen_enabled: bool
    virtual_screen_output: str | None
    sessions: list[SessionSnapshot]
    android_devices: list[DeviceInfo]
    chromecasts: list[CastDevice]


class ConvergenceController:
    def __init__(
        self,
        settings: SettingsStore,
        device_manager: DeviceManager,
        scrcpy_manager: ScrcpyManager,
        monitor_manager: MonitorManager,
        cast_manager: ChromecastManager,
        virtual_screen_manager: VirtualScreenManager,
    ) -> None:
        self.settings = settings
        self.device_manager = device_manager
        self.scrcpy_manager = scrcpy_manager
        self.monitor_manager = monitor_manager
        self.cast_manager = cast_manager
        self.virtual_screen_manager = virtual_screen_manager
        self._shutdown_done = False

    def discover_targets(self) -> tuple[list[DeviceInfo], list[CastDevice]]:
        android_devices = [d for d in self.device_manager.list_devices() if d.state == "device"]
        chromecasts: list[CastDevice] = []
        if self.cast_manager.available():
            chromecasts = self.cast_manager.discover(timeout=3)
        return android_devices, chromecasts

    def selected_app_serial(self, devices: list[DeviceInfo] | None = None) -> str | None:
        serial = self.settings.get("app_source_device")
        if serial:
            return str(serial)
        current_devices = devices if devices is not None else self.discover_targets()[0]
        if current_devices:
            serial = current_devices[0].serial
            self.settings.set("app_source_device", serial)
            return serial
        return None

    def selected_monitor_target(self) -> tuple[str, str | None]:
        monitor_cfg = self.settings.get("monitor", {})
        target_type = str(monitor_cfg.get("target_type", "android"))
        target_value = monitor_cfg.get("target_value")
        return target_type, (str(target_value) if target_value else None)

    def snapshot(self) -> AppSnapshot:
        android_devices, chromecasts = self.discover_targets()
        sessions = [
            SessionSnapshot(
                session_id=session.session_id,
                serial=session.serial,
                mode=session.mode,
                package=session.package,
                display_id=session.display_id,
                started_at=session.started_at,
                mismatch_count=session.mismatch_count,
                last_top_pkg=session.last_top_pkg,
                has_seen_target=session.has_seen_target,
                auto_close_state=session.auto_close_state,
            )
            for session in self.scrcpy_manager.list_sessions()
        ]
        target_type, target_value = self.selected_monitor_target()
        virtual_screen = self.virtual_screen_manager.status()
        return AppSnapshot(
            app_source_serial=self.selected_app_serial(android_devices),
            monitor_target_type=target_type,
            monitor_target_value=target_value,
            monitor_running=self.monitor_manager.is_running(),
            monitor_url=self.monitor_manager.monitor_url(),
            virtual_screen_enabled=virtual_screen.enabled,
            virtual_screen_output=virtual_screen.output_name,
            sessions=sessions,
            android_devices=android_devices,
            chromecasts=chromecasts,
        )

    def select_app_device(self, serial: str) -> ActionResult:
        serial = serial.strip()
        if not serial:
            return ActionResult(False, "No device serial provided")
        self.settings.set("app_source_device", serial)
        return ActionResult(True, f"App source device set to {serial}")

    def select_monitor_target(self, target_type: str, target_value: str) -> ActionResult:
        kind = target_type.strip().lower()
        value = target_value.strip()
        if kind not in {"android", "chromecast"}:
            return ActionResult(False, f"Unsupported monitor target type: {target_type}")
        if not value:
            return ActionResult(False, "No monitor target value provided")
        self.settings.update_monitor(target_type=kind, target_value=value)
        return ActionResult(True, f"Monitor target set to {kind}:{value}")

    def list_launchable_packages(self, serial: str | None = None) -> tuple[ActionResult, list[str]]:
        chosen = serial or self.selected_app_serial()
        if not chosen:
            return ActionResult(False, "No connected Android app source device selected"), []
        packages = self.scrcpy_manager.list_launchable_packages(chosen)
        if not packages:
            return ActionResult(False, "No launchable packages found"), []
        return ActionResult(True, f"Found {len(packages)} launchable packages"), packages

    def launch_normal(self, serial: str | None = None) -> ActionResult:
        chosen = serial or self.selected_app_serial()
        if not chosen:
            return ActionResult(False, "No connected Android app source device selected")
        try:
            session_id = self.scrcpy_manager.launch_normal(chosen)
        except FileNotFoundError:
            return ActionResult(False, "scrcpy is not installed or not in PATH")
        except Exception as exc:
            return ActionResult(False, f"Failed to launch scrcpy: {exc}")
        return ActionResult(True, f"Started scrcpy session {session_id} for {chosen}")

    def launch_app(self, package: str, serial: str | None = None) -> ActionResult:
        chosen = serial or self.selected_app_serial()
        if not chosen:
            return ActionResult(False, "No connected Android app source device selected")
        pkg = package.strip()
        if not pkg:
            return ActionResult(False, "No app package provided")
        try:
            session_id = self.scrcpy_manager.launch_app_virtual(chosen, pkg)
        except FileNotFoundError:
            return ActionResult(False, "scrcpy is not installed or not in PATH")
        except Exception as exc:
            return ActionResult(False, f"Failed to launch app stream: {exc}")
        return ActionResult(True, f"Started app session {session_id} for {pkg} on {chosen}")

    def start_monitor(self) -> ActionResult:
        target_type, target_value = self.selected_monitor_target()
        if not target_value:
            return ActionResult(False, "No monitor target selected")

        if self.monitor_manager.is_running():
            self.monitor_manager.stop()

        try:
            if target_type == "android":
                self.monitor_manager.start(serial=target_value, auto_launch_client=True)
                url = self.monitor_manager.monitor_url()
                return ActionResult(True, f"Monitor stream started for android:{target_value} ({url or 'url pending'})")
            if target_type == "chromecast":
                cast_mode = str(self.settings.get("monitor", {}).get("chromecast_mode", "realtime"))
                mode = cast_mode if cast_mode in ("realtime", "quality", "delay", "custom") else "realtime"
                self.monitor_manager.start(serial=None, auto_launch_client=False, stream_profile=mode)
                url = self.monitor_manager.monitor_url()
                if not url:
                    self.monitor_manager.stop()
                    return ActionResult(False, "Unable to resolve monitor URL for Chromecast")
                if mode in ("quality", "delay", "custom"):
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}mode={mode}"
                self.cast_manager.cast_url(target_value, url)
                return ActionResult(True, f"Monitor stream started for chromecast:{target_value} ({url})")
            return ActionResult(False, f"Unsupported monitor target type: {target_type}")
        except Exception as exc:
            self.monitor_manager.stop()
            return ActionResult(False, f"Failed to start monitor stream: {exc}")

    def stop_monitor(self) -> ActionResult:
        try:
            self.monitor_manager.stop()
        except Exception as exc:
            return ActionResult(False, f"Failed to stop monitor stream: {exc}")
        return ActionResult(True, "Monitor stream stopped")

    def set_virtual_screen(self, enabled: bool) -> ActionResult:
        try:
            if enabled:
                status = self.virtual_screen_manager.enable()
                resolution = f"{status.width}x{status.height}" if status.width and status.height else "1080p"
                return ActionResult(
                    True,
                    f"Virtual screen enabled as {status.output_name} ({resolution})",
                )

            stopped_stream = self.monitor_manager.is_running()
            if stopped_stream:
                self.monitor_manager.stop()
            self.virtual_screen_manager.disable()
            suffix = "; monitor stream stopped" if stopped_stream else ""
            return ActionResult(True, f"Virtual screen disabled{suffix}")
        except Exception as exc:
            return ActionResult(False, f"Failed to {'enable' if enabled else 'disable'} virtual screen: {exc}")

    def move_active_window_to_virtual_screen(self) -> ActionResult:
        try:
            workspace = self.virtual_screen_manager.move_active_window()
        except Exception as exc:
            return ActionResult(False, f"Failed to move window: {exc}")
        return ActionResult(True, f"Moved active window to virtual workspace {workspace}")

    def stop_all_scrcpy(self) -> ActionResult:
        self.scrcpy_manager.stop_all()
        return ActionResult(True, "Stopped all scrcpy sessions")

    def stop_session(self, session_id: int) -> ActionResult:
        existing = {session.session_id for session in self.scrcpy_manager.list_sessions()}
        if session_id not in existing:
            return ActionResult(False, f"No session with id {session_id}")
        self.scrcpy_manager.stop_session(session_id)
        return ActionResult(True, f"Stopped session {session_id}")

    def adb_connect(self, endpoint: str) -> ActionResult:
        connected, message = self.device_manager.connect(endpoint)
        return ActionResult(connected, message or "adb connect failed")

    def adb_reset(self) -> ActionResult:
        ok = self.device_manager.kill_server()
        started = self.device_manager.start_server()
        if ok and started:
            return ActionResult(True, "adb server restarted")
        if not ok:
            return ActionResult(False, "adb kill-server failed")
        return ActionResult(False, "adb start-server failed")

    def set_turn_screen_off(self, enabled: bool) -> ActionResult:
        self.settings.update_scrcpy(turn_screen_off=bool(enabled))
        return ActionResult(True, f"Turn screen off set to {bool(enabled)}")

    def set_stay_awake(self, enabled: bool) -> ActionResult:
        self.settings.update_scrcpy(stay_awake=bool(enabled))
        return ActionResult(True, f"Stay awake set to {bool(enabled)}")

    def set_immersive_poke(self, enabled: bool) -> ActionResult:
        self.settings.update_scrcpy(immersive_poke=bool(enabled))
        return ActionResult(True, f"Immersive poke set to {bool(enabled)}")

    def set_resolution(self, resolution: str) -> ActionResult:
        value = resolution.strip().lower()
        match = re.fullmatch(r"([1-9]\d{2,4})x([1-9]\d{2,4})", value)
        if not match:
            return ActionResult(False, "Resolution must look like 1920x1080")
        width, height = int(match.group(1)), int(match.group(2))
        if width < 320 or height < 320 or width > 9999 or height > 9999:
            return ActionResult(False, "Resolution must be between 320x320 and 9999x9999")
        self.settings.update_scrcpy(resolution=value)
        return ActionResult(True, f"Resolution set to {value}")

    def set_encoder(self, encoder: str) -> ActionResult:
        value = encoder.strip() if encoder.strip() else "auto"
        try:
            self.monitor_manager.set_encoder(None if value == "auto" else value)
        except Exception as exc:
            return ActionResult(False, f"Failed to set encoder: {exc}")
        return ActionResult(True, f"Encoder set to {value}")

    def set_chromecast_mode(self, mode: str) -> ActionResult:
        value = mode.strip().lower()
        if value not in {"realtime", "quality", "delay", "custom"}:
            return ActionResult(False, f"Unsupported Chromecast mode: {mode}")
        self.settings.update_monitor(chromecast_mode=value)
        return ActionResult(True, f"Chromecast mode set to {value}")

    def set_stream_format(self, stream_format: str) -> ActionResult:
        value = stream_format.strip().lower()
        if value not in {"1080p30", "720p30", "720p60"}:
            return ActionResult(False, f"Unsupported stream format: {stream_format}")
        self.settings.update_monitor(stream_format=value)
        width, height = (1280, 720) if value.startswith("720p") else (1920, 1080)
        self.settings.update_virtual_screen(width=width, height=height)
        return ActionResult(
            True,
            f"Stream and next virtual display set to {value} ({width}x{height})",
        )

    def set_realtime_queue_depth(self, depth: int) -> ActionResult:
        value = int(depth)
        if value not in {1, 2, 4}:
            return ActionResult(False, "Realtime queue depth must be 1, 2, or 4 frames")
        self.settings.update_monitor(realtime_queue_depth=value)
        return ActionResult(True, f"Realtime queue depth set to {value} frames for the next stream")

    def set_telemetry_overlay(self, enabled: bool) -> ActionResult:
        self.settings.update_monitor(telemetry_overlay=bool(enabled))
        suffix = "enabled" if enabled else "disabled"
        return ActionResult(True, f"Receiver telemetry overlay {suffix} for the next stream")

    def set_receiver_jitter_target(self, target_ms: int | None) -> ActionResult:
        if target_ms is not None and not 0 <= int(target_ms) <= 4000:
            return ActionResult(False, "Receiver jitter target must be between 0 and 4000 ms")
        normalized = int(target_ms) if target_ms is not None else None
        self.settings.update_monitor(receiver_jitter_target_ms=normalized)
        label = "adaptive" if normalized is None else f"{normalized} ms"
        return ActionResult(True, f"Receiver jitter target set to {label} for the next stream")

    def set_custom_chromecast_mode(
        self,
        *,
        webrtc_latency_ms: int,
        queue_time_ms: int,
        bitrate_multiplier: float,
        keyint: int,
    ) -> ActionResult:
        self.settings.update_monitor(
            custom={
                "webrtc_latency_ms": int(webrtc_latency_ms),
                "queue_time_ms": int(queue_time_ms),
                "bitrate_multiplier": float(bitrate_multiplier),
                "keyint": int(keyint),
            }
        )
        return ActionResult(True, "Custom Chromecast mode updated")

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.monitor_manager.stop()
        finally:
            try:
                self.scrcpy_manager.stop_all()
            finally:
                status = self.virtual_screen_manager.status()
                if status.enabled:
                    self.virtual_screen_manager.disable()
