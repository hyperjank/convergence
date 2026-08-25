#!/usr/bin/env python3
"""PyQt tray UI for convergence."""

from __future__ import annotations

import threading
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QActionGroup, QIcon
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QVBoxLayout,
)

from cast_manager import CastDevice
from controller import ActionResult, ConvergenceController
from device_manager import DeviceInfo
try:
    from reandmon2.config import ENCODER_PIPELINES
except Exception:
    ENCODER_PIPELINES = [("x264enc", "")]


ENCODER_LABELS = {
    "auto": "Auto",
    "vah264lpenc": "Intel low-power (vah264lpenc)",
    "vah264enc": "Intel VA (vah264enc)",
    "x264enc": "Software (x264enc)",
}


class DiscoverySignals(QObject):
    finished = pyqtSignal(object, object)


class AppPickerDialog(QDialog):
    def __init__(self, packages: list[str]) -> None:
        super().__init__()
        self.setWindowTitle("Launch Android App")
        self.resize(620, 760)

        self._all_packages = packages

        layout = QVBoxLayout(self)

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search package…")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list_widget = QListWidget(self)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate(self._all_packages)
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def _populate(self, packages: list[str]) -> None:
        self.list_widget.clear()
        for pkg in packages:
            item = QListWidgetItem(pkg)
            item.setData(0x0100, pkg)  # Qt.UserRole
            self.list_widget.addItem(item)

    def _filter(self, text: str) -> None:
        query = text.strip().lower()
        if not query:
            filtered = self._all_packages
        else:
            filtered = [pkg for pkg in self._all_packages if query in pkg.lower()]
        self._populate(filtered)
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def selected_package(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        if not item:
            return None
        pkg = item.data(0x0100)
        if isinstance(pkg, str) and pkg.strip():
            return pkg.strip()
        return None


class ConvergenceTray:
    def __init__(self, controller: ConvergenceController) -> None:
        self.controller = controller
        self.settings = controller.settings
        self.device_manager = controller.device_manager
        self.scrcpy_manager = controller.scrcpy_manager
        self.monitor_manager = controller.monitor_manager
        self.cast_manager = controller.cast_manager
        self.virtual_screen_manager = controller.virtual_screen_manager
        self._last_android_devices: list[DeviceInfo] = []
        self._last_chromecasts: list[CastDevice] = []
        self._refresh_in_progress = False
        self._discovery_signals = DiscoverySignals()
        self._discovery_signals.finished.connect(self._on_discovery_finished)

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon.fromTheme("video-display"))
        self.tray.setToolTip("convergence")

        self.menu = QMenu()

        self.status_action = self.menu.addAction("Initializing…")
        self.status_action.setEnabled(False)
        self.telemetry_action = self.menu.addAction("Receiver: waiting for stream")
        self.telemetry_action.setEnabled(False)
        self.menu.addSeparator()

        self.monitor_target_menu = self.menu.addMenu("Cast Target")
        self._monitor_target_group = QActionGroup(self.monitor_target_menu)
        self._monitor_target_group.setExclusive(True)

        self.virtual_screen_action = self.menu.addAction("Virtual Display: Off")
        self.virtual_screen_action.setCheckable(True)
        self.virtual_screen_action.triggered.connect(self.on_toggle_virtual_screen)

        self.start_monitor_action = self.menu.addAction("Start Casting")
        self.start_monitor_action.triggered.connect(self.on_start_monitor)
        self.stop_monitor_action = self.menu.addAction("Stop Casting")
        self.stop_monitor_action.triggered.connect(self.on_stop_monitor)

        self.menu.addSeparator()

        self.stream_settings_menu = self.menu.addMenu("Stream Settings")

        self.stream_format_menu = self.stream_settings_menu.addMenu("Format")
        self._stream_format_group = QActionGroup(self.stream_format_menu)
        self._stream_format_group.setExclusive(True)
        self.stream_1080p30_action = self.stream_format_menu.addAction("1080p30 (baseline)")
        self.stream_1080p30_action.setCheckable(True)
        self.stream_1080p30_action.triggered.connect(
            lambda checked: checked and self.on_set_stream_format("1080p30")
        )
        self._stream_format_group.addAction(self.stream_1080p30_action)
        self.stream_720p30_action = self.stream_format_menu.addAction("720p30 (stability)")
        self.stream_720p30_action.setCheckable(True)
        self.stream_720p30_action.triggered.connect(
            lambda checked: checked and self.on_set_stream_format("720p30")
        )
        self._stream_format_group.addAction(self.stream_720p30_action)
        self.stream_720p60_action = self.stream_format_menu.addAction("720p60 (interactive)")
        self.stream_720p60_action.setCheckable(True)
        self.stream_720p60_action.triggered.connect(
            lambda checked: checked and self.on_set_stream_format("720p60")
        )
        self._stream_format_group.addAction(self.stream_720p60_action)

        self.encoder_menu = self.stream_settings_menu.addMenu("Encoder")
        self._encoder_group = QActionGroup(self.encoder_menu)
        self._encoder_group.setExclusive(True)
        self.encoder_auto_action = self.encoder_menu.addAction("Auto")
        self.encoder_auto_action.setCheckable(True)
        self.encoder_auto_action.setData("auto")
        self.encoder_auto_action.triggered.connect(
            lambda checked: checked and self.on_set_encoder("auto")
        )
        self._encoder_group.addAction(self.encoder_auto_action)
        try:
            available_encoders = set(self.monitor_manager.available_encoders())
        except Exception:
            available_encoders = {"x264enc"}
        for enc_name, _ in ENCODER_PIPELINES:
            if enc_name not in available_encoders:
                continue
            act = self.encoder_menu.addAction(ENCODER_LABELS.get(enc_name, enc_name))
            act.setCheckable(True)
            act.setData(enc_name)
            act.triggered.connect(
                lambda checked, e=enc_name: checked and self.on_set_encoder(e)
            )
            self._encoder_group.addAction(act)

        self.queue_depth_menu = self.stream_settings_menu.addMenu("Realtime Queue Depth")
        self._queue_depth_group = QActionGroup(self.queue_depth_menu)
        self._queue_depth_group.setExclusive(True)
        self.queue_depth_4_action = self.queue_depth_menu.addAction("4 frames (baseline)")
        self.queue_depth_2_action = self.queue_depth_menu.addAction("2 frames (low latency)")
        self.queue_depth_1_action = self.queue_depth_menu.addAction("1 frame (aggressive)")
        for action, depth in (
            (self.queue_depth_4_action, 4),
            (self.queue_depth_2_action, 2),
            (self.queue_depth_1_action, 1),
        ):
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked, value=depth: checked and self.on_set_realtime_queue_depth(value)
            )
            self._queue_depth_group.addAction(action)

        self.telemetry_overlay_action = self.stream_settings_menu.addAction("Show Receiver Telemetry")
        self.telemetry_overlay_action.setCheckable(True)
        self.telemetry_overlay_action.triggered.connect(self.on_toggle_telemetry_overlay)

        self.advanced_stream_menu = self.stream_settings_menu.addMenu("Advanced")
        self.chromecast_mode_menu = self.advanced_stream_menu.addMenu("Chromecast Mode")
        self._cast_mode_group = QActionGroup(self.chromecast_mode_menu)
        self._cast_mode_group.setExclusive(True)
        self.cast_mode_realtime_action = self.chromecast_mode_menu.addAction("Realtime")
        self.cast_mode_realtime_action.setCheckable(True)
        self.cast_mode_realtime_action.triggered.connect(
            lambda checked: checked and self.on_set_chromecast_mode("realtime")
        )
        self._cast_mode_group.addAction(self.cast_mode_realtime_action)
        self.cast_mode_quality_action = self.chromecast_mode_menu.addAction("Quality")
        self.cast_mode_quality_action.setCheckable(True)
        self.cast_mode_quality_action.triggered.connect(
            lambda checked: checked and self.on_set_chromecast_mode("quality")
        )
        self._cast_mode_group.addAction(self.cast_mode_quality_action)
        self.cast_mode_delay_action = self.chromecast_mode_menu.addAction("Delay")
        self.cast_mode_delay_action.setCheckable(True)
        self.cast_mode_delay_action.triggered.connect(
            lambda checked: checked and self.on_set_chromecast_mode("delay")
        )
        self._cast_mode_group.addAction(self.cast_mode_delay_action)
        self.cast_mode_custom_action = self.chromecast_mode_menu.addAction("Custom")
        self.cast_mode_custom_action.setCheckable(True)
        self.cast_mode_custom_action.triggered.connect(
            lambda checked: checked and self.on_set_chromecast_mode("custom")
        )
        self._cast_mode_group.addAction(self.cast_mode_custom_action)
        self.chromecast_mode_config_action = self.chromecast_mode_menu.addAction("Configure Custom…")
        self.chromecast_mode_config_action.triggered.connect(self.on_configure_custom_mode)

        self.receiver_jitter_menu = self.advanced_stream_menu.addMenu("Receiver Jitter Buffer")
        self._receiver_jitter_group = QActionGroup(self.receiver_jitter_menu)
        self._receiver_jitter_group.setExclusive(True)
        self.receiver_jitter_adaptive_action = self.receiver_jitter_menu.addAction("Adaptive (baseline)")
        self.receiver_jitter_adaptive_action.setCheckable(True)
        self.receiver_jitter_adaptive_action.triggered.connect(
            lambda checked: checked and self.on_set_receiver_jitter_target(None)
        )
        self._receiver_jitter_group.addAction(self.receiver_jitter_adaptive_action)
        self.receiver_jitter_low_action = self.receiver_jitter_menu.addAction("Low latency (100 ms)")
        self.receiver_jitter_low_action.setCheckable(True)
        self.receiver_jitter_low_action.triggered.connect(
            lambda checked: checked and self.on_set_receiver_jitter_target(100)
        )
        self._receiver_jitter_group.addAction(self.receiver_jitter_low_action)

        self.android_menu = self.menu.addMenu("Android Mirroring")
        self.app_device_menu = self.android_menu.addMenu("Source Device")
        self._app_device_group = QActionGroup(self.app_device_menu)
        self._app_device_group.setExclusive(True)

        self.launch_normal_action = self.android_menu.addAction("Mirror Entire Device")
        self.launch_normal_action.triggered.connect(self.on_launch_normal)
        self.launch_app_action = self.android_menu.addAction("Launch App on Virtual Display")
        self.launch_app_action.triggered.connect(self.on_launch_app)

        self.scrcpy_settings_menu = self.android_menu.addMenu("scrcpy Settings")
        self.turn_screen_off_action = self.scrcpy_settings_menu.addAction("Turn Screen Off")
        self.turn_screen_off_action.setCheckable(True)
        self.turn_screen_off_action.triggered.connect(self.on_toggle_turn_screen_off)

        self.stay_awake_action = self.scrcpy_settings_menu.addAction("Keep Device Awake")
        self.stay_awake_action.setCheckable(True)
        self.stay_awake_action.triggered.connect(self.on_toggle_stay_awake)

        self.immersive_poke_action = self.scrcpy_settings_menu.addAction("Immersive Mode Poke")
        self.immersive_poke_action.setCheckable(True)
        self.immersive_poke_action.triggered.connect(self.on_toggle_immersive_poke)

        self.resolution_menu = self.scrcpy_settings_menu.addMenu("Virtual Display Resolution")
        self._res_group = QActionGroup(self.resolution_menu)
        self._res_group.setExclusive(True)
        for res in ["1280x720", "1920x1080", "1080x1920", "1440x3040", "2160x1080"]:
            act = self.resolution_menu.addAction(res)
            act.setCheckable(True)
            act.triggered.connect(lambda checked, r=res: self.on_set_resolution(r))
            self._res_group.addAction(act)
        self.custom_res_action = self.resolution_menu.addAction("Custom…")
        self.custom_res_action.triggered.connect(self.on_custom_resolution)

        self.android_menu.addSeparator()
        self.stop_all_scrcpy_action = self.android_menu.addAction("Stop All Android Sessions")
        self.stop_all_scrcpy_action.triggered.connect(self.on_stop_all_scrcpy)

        self.tools_menu = self.menu.addMenu("Tools")
        self.tools_refresh_action = self.tools_menu.addAction("Refresh Devices")
        self.tools_refresh_action.triggered.connect(self.on_tools_refresh)
        self.tools_connect_action = self.tools_menu.addAction("Connect Android via ADB…")
        self.tools_connect_action.triggered.connect(self.on_tools_connect_adb)
        self.tools_reset_action = self.tools_menu.addAction("Restart ADB")
        self.tools_reset_action.triggered.connect(self.on_tools_reset_adb)
        self.tools_menu.addSeparator()
        self.tools_scan_cast_action = self.tools_menu.addAction("Scan Chromecasts")
        self.tools_scan_cast_action.triggered.connect(self.on_tools_scan_chromecast)

        self.menu.addSeparator()

        self.exit_action = self.menu.addAction("Exit")
        self.exit_action.triggered.connect(self.on_exit)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

        self._sync_settings_to_ui()
        self.refresh_targets()

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(1000)

    def _selected_app_serial(self) -> Optional[str]:
        return self.controller.selected_app_serial(self._last_android_devices)

    def _selected_monitor_target(self) -> tuple[str, Optional[str]]:
        return self.controller.selected_monitor_target()

    def _sync_settings_to_ui(self) -> None:
        scrcpy_cfg = self.settings.get("scrcpy", {})
        turn_screen_off = bool(scrcpy_cfg.get("turn_screen_off", False))
        stay_awake = bool(scrcpy_cfg.get("stay_awake", True))
        immersive_poke = bool(scrcpy_cfg.get("immersive_poke", True))
        resolution = str(scrcpy_cfg.get("resolution", "1920x1080"))
        cast_mode = str(self.settings.get("monitor", {}).get("chromecast_mode", "realtime"))
        stream_format = str(self.settings.get("monitor", {}).get("stream_format", "1080p30"))
        queue_depth = int(self.settings.get("monitor", {}).get("realtime_queue_depth", 4))
        encoder = str(self.settings.get("monitor", {}).get("encoder", "auto"))
        telemetry_overlay = bool(self.settings.get("monitor", {}).get("telemetry_overlay", False))
        receiver_jitter_target = self.settings.get("monitor", {}).get("receiver_jitter_target_ms")
        self.telemetry_overlay_action.blockSignals(True)
        self.telemetry_overlay_action.setChecked(telemetry_overlay)
        self.telemetry_overlay_action.blockSignals(False)
        self.receiver_jitter_adaptive_action.setChecked(receiver_jitter_target is None)
        self.receiver_jitter_low_action.setChecked(receiver_jitter_target == 100)

        self.turn_screen_off_action.setChecked(turn_screen_off)
        self.stay_awake_action.setChecked(stay_awake)
        self.immersive_poke_action.setChecked(immersive_poke)
        self.cast_mode_realtime_action.setChecked(cast_mode == "realtime")
        self.cast_mode_quality_action.setChecked(cast_mode == "quality")
        self.cast_mode_delay_action.setChecked(cast_mode == "delay")
        self.cast_mode_custom_action.setChecked(cast_mode == "custom")
        self.stream_1080p30_action.setChecked(stream_format == "1080p30")
        self.stream_720p30_action.setChecked(stream_format == "720p30")
        self.stream_720p60_action.setChecked(stream_format == "720p60")
        self.queue_depth_4_action.setChecked(queue_depth == 4)
        self.queue_depth_2_action.setChecked(queue_depth == 2)
        self.queue_depth_1_action.setChecked(queue_depth == 1)
        self.encoder_auto_action.setChecked(encoder == "auto")
        if encoder != "auto":
            for act in self._encoder_group.actions():
                if act.data() == encoder:
                    act.setChecked(True)
                    break

        matched = False
        for act in self._res_group.actions():
            if act.text() == resolution:
                act.setChecked(True)
                matched = True
        if not matched:
            for act in self._res_group.actions():
                act.setChecked(False)

    def refresh_app_devices(self) -> None:
        self.refresh_targets()

    def _render_app_devices(self, devices: list[DeviceInfo]) -> None:
        for act in list(self._app_device_group.actions()):
            self.app_device_menu.removeAction(act)
            self._app_device_group.removeAction(act)

        selected = self._selected_app_serial()

        for dev in devices:
            label = dev.serial
            if dev.model:
                label = f"{dev.model} ({dev.serial})"
            act = self.app_device_menu.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(lambda checked, s=dev.serial: self.on_select_app_device(s))
            self._app_device_group.addAction(act)
            if selected == dev.serial:
                act.setChecked(True)

        if devices and not any(a.isChecked() for a in self._app_device_group.actions()):
            self.on_select_app_device(devices[0].serial)
            for act in self._app_device_group.actions():
                if devices[0].serial in act.text():
                    act.setChecked(True)
                    break

        self.refresh_status()

    def refresh_monitor_targets(self) -> None:
        self.refresh_targets()

    def _render_monitor_targets(self, android_devices: list[DeviceInfo], chromecasts: list[CastDevice]) -> None:
        for act in list(self._monitor_target_group.actions()):
            self.monitor_target_menu.removeAction(act)
            self._monitor_target_group.removeAction(act)

        selected_type, selected_value = self._selected_monitor_target()

        for dev in android_devices:
            label = f"Android: {dev.serial}"
            if dev.model:
                label = f"Android: {dev.model} ({dev.serial})"
            act = self.monitor_target_menu.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(
                lambda checked, serial=dev.serial: self.on_select_monitor_target("android", serial)
            )
            self._monitor_target_group.addAction(act)
            if selected_type == "android" and selected_value == dev.serial:
                act.setChecked(True)

        for cast in chromecasts:
            label = f"Chromecast: {cast.name} ({cast.model_name})"
            act = self.monitor_target_menu.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(
                lambda checked, name=cast.name: self.on_select_monitor_target("chromecast", name)
            )
            self._monitor_target_group.addAction(act)
            if selected_type == "chromecast" and selected_value == cast.name:
                act.setChecked(True)

        if not any(a.isChecked() for a in self._monitor_target_group.actions()):
            if android_devices:
                self.on_select_monitor_target("android", android_devices[0].serial)
            elif chromecasts:
                self.on_select_monitor_target("chromecast", chromecasts[0].name)

            selected_type, selected_value = self._selected_monitor_target()
            for act in self._monitor_target_group.actions():
                text = act.text()
                if selected_type == "android" and selected_value and selected_value in text:
                    act.setChecked(True)
                if selected_type == "chromecast" and selected_value and f"Chromecast: {selected_value}" in text:
                    act.setChecked(True)

        self.refresh_status()

    def refresh_targets(self) -> None:
        if self._refresh_in_progress:
            return
        self._refresh_in_progress = True
        self.status_action.setText("Refreshing devices…")

        def _worker() -> None:
            try:
                android_devices, chromecasts = self.controller.discover_targets()
            except Exception:
                android_devices = []
                chromecasts = []
            self._discovery_signals.finished.emit(android_devices, chromecasts)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_discovery_finished(self, android_devices: object, chromecasts: object) -> None:
        self._refresh_in_progress = False
        self._last_android_devices = list(android_devices) if isinstance(android_devices, list) else []
        self._last_chromecasts = list(chromecasts) if isinstance(chromecasts, list) else []
        self._render_app_devices(self._last_android_devices)
        self._render_monitor_targets(self._last_android_devices, self._last_chromecasts)

    def on_select_app_device(self, serial: str) -> None:
        self._handle_result(self.controller.select_app_device(serial))
        self.refresh_status()

    def on_select_monitor_target(self, target_type: str, target_value: str) -> None:
        self._handle_result(self.controller.select_monitor_target(target_type, target_value))
        self.refresh_status()

    def on_start_monitor(self) -> None:
        self._handle_result(self.controller.start_monitor())
        self.refresh_status()

    def on_stop_monitor(self) -> None:
        self._handle_result(self.controller.stop_monitor())
        self.refresh_status()

    def on_toggle_virtual_screen(self, checked: bool) -> None:
        if not self._handle_result(self.controller.set_virtual_screen(checked)):
            self.virtual_screen_action.blockSignals(True)
            self.virtual_screen_action.setChecked(not checked)
            self.virtual_screen_action.blockSignals(False)
        self.refresh_status()

    def on_launch_normal(self) -> None:
        self._handle_result(self.controller.launch_normal())
        self.refresh_status()

    def on_launch_app(self) -> None:
        result, packages = self.controller.list_launchable_packages()
        if not self._handle_result(result):
            return

        dlg = AppPickerDialog(packages)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        package = dlg.selected_package()
        if not package:
            self._show_error("Unable to resolve selected app package")
            return

        self._handle_result(self.controller.launch_app(package))
        self.refresh_status()

    def on_toggle_turn_screen_off(self, checked: bool) -> None:
        self._handle_result(self.controller.set_turn_screen_off(checked))

    def on_toggle_stay_awake(self, checked: bool) -> None:
        self._handle_result(self.controller.set_stay_awake(checked))

    def on_toggle_immersive_poke(self, checked: bool) -> None:
        self._handle_result(self.controller.set_immersive_poke(checked))

    def on_set_resolution(self, resolution: str) -> None:
        self._handle_result(self.controller.set_resolution(resolution))

    def on_custom_resolution(self) -> None:
        current = str(self.settings.get("scrcpy", {}).get("resolution", "1920x1080"))
        text, ok = QInputDialog.getText(None, "Custom Resolution", "Resolution WxH:", text=current)
        if not ok:
            return
        if not self._handle_result(self.controller.set_resolution(text)):
            return
        self._sync_settings_to_ui()

    def on_stop_all_scrcpy(self) -> None:
        self._handle_result(self.controller.stop_all_scrcpy())
        self.refresh_status()

    def on_tools_reset_adb(self) -> None:
        self._handle_result(self.controller.adb_reset())
        self.on_tools_refresh()

    def on_tools_connect_adb(self) -> None:
        endpoint, ok = QInputDialog.getText(
            None,
            "ADB Connect",
            "IP:PORT",
            text="",
        )
        if not ok:
            return
        endpoint = endpoint.strip()
        if not endpoint:
            return
        result = self.controller.adb_connect(endpoint)
        self.on_tools_refresh()
        if result.ok:
            QMessageBox.information(None, "convergence", result.message)
        else:
            self._show_error(result.message)

    def on_tools_refresh(self) -> None:
        self.refresh_targets()
        self.refresh_status()

    def on_tools_scan_chromecast(self) -> None:
        if not self.cast_manager.available():
            self._show_error("pychromecast is not installed")
            return
        self.refresh_targets()
        QMessageBox.information(None, "convergence", "Chromecast scan started")

    def on_set_chromecast_mode(self, mode: str) -> None:
        self._handle_result(self.controller.set_chromecast_mode(mode))
        self._sync_settings_to_ui()
        self.refresh_status()

    def on_set_stream_format(self, stream_format: str) -> None:
        self._handle_result(self.controller.set_stream_format(stream_format))
        self._sync_settings_to_ui()
        self.refresh_status()

    def on_set_realtime_queue_depth(self, depth: int) -> None:
        self._handle_result(self.controller.set_realtime_queue_depth(depth))
        self._sync_settings_to_ui()
        self.refresh_status()

    def on_toggle_telemetry_overlay(self, checked: bool) -> None:
        self._handle_result(self.controller.set_telemetry_overlay(checked))
        self.refresh_status()

    def on_set_receiver_jitter_target(self, target_ms: int | None) -> None:
        self._handle_result(self.controller.set_receiver_jitter_target(target_ms))
        self._sync_settings_to_ui()
        self.refresh_status()

    def on_configure_custom_mode(self) -> None:
        monitor_cfg = self.settings.get("monitor", {})
        custom = monitor_cfg.get("custom", {}) if isinstance(monitor_cfg.get("custom"), dict) else {}

        lat, ok = QInputDialog.getInt(
            None, "Custom Mode", "WebRTC latency (ms):", int(custom.get("webrtc_latency_ms", 1200)), 100, 5000, 50
        )
        if not ok:
            return
        queue, ok = QInputDialog.getInt(
            None, "Custom Mode", "Queue time (ms):", int(custom.get("queue_time_ms", 12000)), 500, 30000, 100
        )
        if not ok:
            return
        bitrate, ok = QInputDialog.getDouble(
            None, "Custom Mode", "Bitrate multiplier:", float(custom.get("bitrate_multiplier", 1.6)), 0.5, 6.0, 2
        )
        if not ok:
            return
        keyint, ok = QInputDialog.getInt(
            None, "Custom Mode", "Keyframe interval:", int(custom.get("keyint", 45)), 10, 300, 1
        )
        if not ok:
            return

        result = self.controller.set_custom_chromecast_mode(
            webrtc_latency_ms=lat,
            queue_time_ms=queue,
            bitrate_multiplier=bitrate,
            keyint=keyint,
        )
        if self._handle_result(result):
            QMessageBox.information(None, "convergence", result.message)

    def on_set_encoder(self, encoder: str) -> None:
        self._handle_result(self.controller.set_encoder(encoder))
        self._sync_settings_to_ui()
        self.refresh_status()

    def on_exit(self) -> None:
        self.controller.shutdown()
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.quit()

    def refresh_status(self) -> None:
        running = self.monitor_manager.is_running()
        _target_type, target_value = self._selected_monitor_target()
        target = target_value or "no target"
        monitor_cfg = self.settings.get("monitor", {})
        stream_format = str(monitor_cfg.get("stream_format", "1080p30"))
        encoder = str(monitor_cfg.get("encoder", "auto"))
        encoder_label = ENCODER_LABELS.get(encoder, encoder).split(" (")[0]
        virtual_screen = self.virtual_screen_manager.status()
        state = "Casting" if running else "Ready"
        status_text = f"{state} → {target} · {stream_format} · {encoder_label}"
        self.status_action.setText(status_text)
        telemetry = self.monitor_manager.latest_receiver_telemetry()
        if running and telemetry:
            def metric(name: str, suffix: str = "", digits: int = 1) -> str:
                value = telemetry.get(name)
                if not isinstance(value, (int, float)):
                    return "—"
                return f"{value:.{digits}f}{suffix}"

            self.telemetry_action.setText(
                f"Live · {metric('decodeFps', '', 1)} → {metric('renderFps', '', 1)} fps · "
                f"{metric('rttMs', ' ms')} · {metric('packetLossPercent', '%', 2)} loss"
            )
            self.telemetry_action.setToolTip(
                f"Decode/render: {metric('decodeFps', '', 1)} / {metric('renderFps', '', 1)} fps\n"
                f"Dropped frames: +{metric('framesDroppedDelta', '', 0)}\n"
                f"Packet loss: {metric('packetLossPercent', '%', 2)}\n"
                f"RTT: {metric('rttMs', ' ms')}\n"
                f"Target jitter buffer: {metric('jitterBufferTargetMs', ' ms')}\n"
                f"Minimum jitter buffer: {metric('jitterBufferMinimumMs', ' ms')}\n"
                f"Control API: {'supported' if telemetry.get('jitterBufferTargetSupported') == 1 else 'unavailable'}\n"
                f"Requested/read back: {metric('jitterBufferTargetRequestedMs', ' ms')} / "
                f"{metric('jitterBufferTargetReadbackMs', ' ms')}\n"
                f"Network jitter: {metric('jitterMs', ' ms')}\n"
                f"Receive rate: {metric('bitrateMbps', ' Mbit/s', 2)}\n"
                f"Log: {self.monitor_manager.telemetry_log_path() or 'unavailable'}"
            )
        elif running:
            self.telemetry_action.setText("Receiver: waiting for telemetry")
            self.telemetry_action.setToolTip(
                f"Log: {self.monitor_manager.telemetry_log_path() or 'pending'}"
            )
        else:
            self.telemetry_action.setText("Receiver: idle")
            self.telemetry_action.setToolTip("")
        self.virtual_screen_action.blockSignals(True)
        self.virtual_screen_action.setChecked(virtual_screen.enabled)
        width = getattr(virtual_screen, "width", None)
        height = getattr(virtual_screen, "height", None)
        if virtual_screen.enabled and width and height:
            self.virtual_screen_action.setText(f"Virtual Display: On · {width}×{height}")
        elif virtual_screen.enabled:
            self.virtual_screen_action.setText("Virtual Display: On")
        else:
            self.virtual_screen_action.setText("Virtual Display: Off")
        self.virtual_screen_action.blockSignals(False)
        self.virtual_screen_action.setEnabled(virtual_screen.available)
        self.start_monitor_action.setText(f"Start Cast → {target}" if target_value else "Start Casting")
        self.start_monitor_action.setEnabled(not running and target_value is not None)
        self.stop_monitor_action.setEnabled(running)
        tray_tooltip = status_text
        if running and telemetry:
            tray_tooltip += f"\n{self.telemetry_action.text()}"
        self.tray.setToolTip(tray_tooltip)

    def _show_error(self, text: str) -> None:
        QMessageBox.critical(None, "convergence", text)

    def _handle_result(self, result: ActionResult) -> bool:
        if not result.ok:
            self._show_error(result.message)
        return result.ok
