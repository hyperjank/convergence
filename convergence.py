#!/usr/bin/env python3
"""Tray-first bootstrap with optional CLI and Textual frontends."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from cast_manager import ChromecastManager
from controller import ActionResult, ConvergenceController
from device_manager import DeviceManager
from monitor_manager import MonitorManager
from scrcpy_manager import ScrcpyManager
from settings import SettingsStore
from virtual_screen_manager import VirtualScreenManager


def build_controller() -> ConvergenceController:
    settings = SettingsStore()
    device_manager = DeviceManager()
    scrcpy_manager = ScrcpyManager(device_manager, settings)
    monitor_manager = MonitorManager(settings)
    cast_manager = ChromecastManager()
    virtual_screen_manager = VirtualScreenManager(settings)
    return ConvergenceController(
        settings=settings,
        device_manager=device_manager,
        scrcpy_manager=scrcpy_manager,
        monitor_manager=monitor_manager,
        cast_manager=cast_manager,
        virtual_screen_manager=virtual_screen_manager,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="convergence control surface")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tray", help="Launch the system-tray frontend (default)")
    sub.add_parser("status", help="Show the current convergence snapshot")
    sub.add_parser("devices", help="List discovered Android and Chromecast targets")
    sub.add_parser("start-monitor", help="Start the monitor stream for the selected target")
    sub.add_parser("stop-monitor", help="Stop the active monitor stream")
    virtual_screen = sub.add_parser("virtual-screen", help="Control the Hyprland virtual screen")
    virtual_screen.add_argument("action", choices=["on", "off", "toggle", "status"])
    sub.add_parser("send-window", help="Move the active window to the virtual screen")
    sub.add_parser("stop-all", help="Stop all scrcpy sessions")
    sub.add_parser("reset-adb", help="Restart the adb server")

    connect = sub.add_parser("connect-adb", help="Run adb connect against an IP:PORT endpoint")
    connect.add_argument("endpoint")

    launch_normal = sub.add_parser("launch-normal", help="Launch a normal scrcpy session")
    launch_normal.add_argument("--serial")

    launch_app = sub.add_parser("launch-app", help="Launch an Android app in a virtual display")
    launch_app.add_argument("package")
    launch_app.add_argument("--serial")

    packages = sub.add_parser("packages", help="List launchable packages for a device")
    packages.add_argument("--serial")

    select_app = sub.add_parser("select-app-device", help="Persist the selected app source device")
    select_app.add_argument("serial")

    select_target = sub.add_parser("select-monitor-target", help="Persist the selected monitor target")
    select_target.add_argument("target_type", choices=["android", "chromecast"])
    select_target.add_argument("target_value")

    resolution = sub.add_parser("set-resolution", help="Set the virtual display resolution")
    resolution.add_argument("value")

    encoder = sub.add_parser("set-encoder", help="Set the monitor encoder override")
    encoder.add_argument("value")

    cast_mode = sub.add_parser("set-cast-mode", help="Set the Chromecast stream profile")
    cast_mode.add_argument("value", choices=["realtime", "quality", "delay", "custom"])

    stream_format = sub.add_parser("set-stream-format", help="Set the monitor video format")
    stream_format.add_argument("value", choices=["1080p30", "720p30", "720p60"])

    queue_depth = sub.add_parser("set-queue-depth", help="Set realtime queue depth per queue")
    queue_depth.add_argument("value", type=int, choices=[1, 2, 4])

    custom_mode = sub.add_parser("set-custom-cast-mode", help="Set the custom Chromecast profile")
    custom_mode.add_argument("--webrtc-latency-ms", type=int, required=True)
    custom_mode.add_argument("--queue-time-ms", type=int, required=True)
    custom_mode.add_argument("--bitrate-multiplier", type=float, required=True)
    custom_mode.add_argument("--keyint", type=int, required=True)

    sub.add_parser("tui", help="Launch the optional Textual frontend")
    return parser


def render_action(result: ActionResult) -> int:
    stream = sys.stdout if result.ok else sys.stderr
    print(result.message, file=stream)
    return 0 if result.ok else 1


def run_tray(controller: ConvergenceController) -> int:
    try:
        from PyQt6.QtWidgets import QApplication
        from tray_ui import ConvergenceTray
    except ImportError as exc:
        print(f"Unable to start tray frontend: {exc}", file=sys.stderr)
        print("Install PyQt6 to use the tray frontend.", file=sys.stderr)
        return 1

    if not sys.stdout.isatty():
        log_file = Path("/tmp/convergence.log").open("a", buffering=1, encoding="utf-8")
        sys.stdout = log_file
        sys.stderr = log_file
        print("\n--- convergence start ---")

    app = QApplication([sys.argv[0]])
    app.setApplicationName("Convergence")
    app.setQuitOnLastWindowClosed(False)
    tray = ConvergenceTray(controller)
    app.aboutToQuit.connect(controller.shutdown)
    _ = tray
    return app.exec()


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    parser = build_parser()
    args = parser.parse_args()
    controller = build_controller()

    if args.command in (None, "tray"):
        return run_tray(controller)
    if args.command == "status":
        print(json.dumps(asdict(controller.snapshot()), indent=2, sort_keys=True))
        return 0
    if args.command == "devices":
        snapshot = controller.snapshot()
        print(json.dumps(
            {
                "android_devices": [asdict(device) for device in snapshot.android_devices],
                "chromecasts": [asdict(cast) for cast in snapshot.chromecasts],
            },
            indent=2,
            sort_keys=True,
        ))
        return 0
    if args.command == "start-monitor":
        return render_action(controller.start_monitor())
    if args.command == "stop-monitor":
        return render_action(controller.stop_monitor())
    if args.command == "virtual-screen":
        current = controller.virtual_screen_manager.status()
        if args.action == "status":
            print(json.dumps(asdict(current), indent=2, sort_keys=True))
            return 0
        enabled = args.action == "on" or (args.action == "toggle" and not current.enabled)
        return render_action(controller.set_virtual_screen(enabled))
    if args.command == "send-window":
        return render_action(controller.move_active_window_to_virtual_screen())
    if args.command == "stop-all":
        return render_action(controller.stop_all_scrcpy())
    if args.command == "reset-adb":
        return render_action(controller.adb_reset())
    if args.command == "connect-adb":
        return render_action(controller.adb_connect(args.endpoint))
    if args.command == "launch-normal":
        return render_action(controller.launch_normal(serial=args.serial))
    if args.command == "launch-app":
        return render_action(controller.launch_app(args.package, serial=args.serial))
    if args.command == "packages":
        result, packages = controller.list_launchable_packages(serial=args.serial)
        if not result.ok:
            return render_action(result)
        print("\n".join(packages))
        return 0
    if args.command == "select-app-device":
        return render_action(controller.select_app_device(args.serial))
    if args.command == "select-monitor-target":
        return render_action(controller.select_monitor_target(args.target_type, args.target_value))
    if args.command == "set-resolution":
        return render_action(controller.set_resolution(args.value))
    if args.command == "set-encoder":
        return render_action(controller.set_encoder(args.value))
    if args.command == "set-cast-mode":
        return render_action(controller.set_chromecast_mode(args.value))
    if args.command == "set-stream-format":
        return render_action(controller.set_stream_format(args.value))
    if args.command == "set-queue-depth":
        return render_action(controller.set_realtime_queue_depth(args.value))
    if args.command == "set-custom-cast-mode":
        return render_action(
            controller.set_custom_chromecast_mode(
                webrtc_latency_ms=args.webrtc_latency_ms,
                queue_time_ms=args.queue_time_ms,
                bitrate_multiplier=args.bitrate_multiplier,
                keyint=args.keyint,
            )
        )
    if args.command == "tui":
        try:
            from tui import ConvergenceTUI
        except ImportError as exc:
            print(f"Unable to start TUI: {exc}", file=sys.stderr)
            print("Install the 'textual' package to use the terminal UI.", file=sys.stderr)
            return 1
        app = ConvergenceTUI(controller)
        app.run()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
