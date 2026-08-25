from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from controller import ConvergenceController
from settings import SettingsStore

try:
    from PyQt6.QtWidgets import QApplication
    from tray_ui import ConvergenceTray
except ImportError:
    QApplication = None
    ConvergenceTray = None


class FakeDeviceManager:
    def list_devices(self):
        return []


class FakeScrcpyManager:
    def list_sessions(self):
        return []

    def stop_all(self):
        return None


class FakeMonitorManager:
    def __init__(self):
        self.running = False

    def is_running(self):
        return self.running

    def stop(self):
        self.running = False

    def set_encoder(self, _name):
        return None

    def available_encoders(self):
        return ["x264enc"]

    def latest_receiver_telemetry(self):
        return None

    def telemetry_log_path(self):
        return None


class FakeCastManager:
    def available(self):
        return False

    def discover(self, timeout=3):
        return []


class FakeVirtualScreenStatus:
    available = True
    enabled = False
    output_name = None
    width = None
    height = None


class FakeVirtualScreenManager:
    def __init__(self):
        self.enabled = False
        self.disable_calls = 0

    def status(self):
        status = FakeVirtualScreenStatus()
        status.enabled = self.enabled
        status.output_name = "HEADLESS-1" if self.enabled else None
        status.width = 1280 if self.enabled else None
        status.height = 720 if self.enabled else None
        return status

    def disable(self):
        self.disable_calls += 1
        self.enabled = False


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class TraySmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["convergence-test"])

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        settings = SettingsStore(Path(self.tempdir.name) / "config.json")
        self.controller = ConvergenceController(
            settings=settings,
            device_manager=FakeDeviceManager(),
            scrcpy_manager=FakeScrcpyManager(),
            monitor_manager=FakeMonitorManager(),
            cast_manager=FakeCastManager(),
            virtual_screen_manager=FakeVirtualScreenManager(),
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_tray_builds_and_uses_controller_settings(self):
        with patch.object(ConvergenceTray, "refresh_targets", autospec=True):
            tray = ConvergenceTray(self.controller)

        labels = [action.text() for action in tray.menu.actions()]
        self.assertIn("Cast Target", labels)
        self.assertIn("Start Casting", labels)
        self.assertIn("Receiver: waiting for stream", labels)
        self.assertIn("Virtual Display: Off", labels)
        self.assertIn("Stream Settings", labels)
        self.assertIn("Android Mirroring", labels)
        android_labels = [action.text() for action in tray.android_menu.actions()]
        self.assertIn("Source Device", android_labels)
        self.assertIn("Mirror Entire Device", android_labels)
        stream_labels = [action.text() for action in tray.stream_settings_menu.actions()]
        self.assertIn("Format", stream_labels)
        self.assertIn("Encoder", stream_labels)
        self.assertIn("Realtime Queue Depth", stream_labels)
        self.assertIn("Exit", labels)

        tray.on_set_resolution("1280x720")
        self.assertEqual(
            self.controller.settings.get("scrcpy", {}).get("resolution"),
            "1280x720",
        )

        tray.on_toggle_telemetry_overlay(True)
        self.assertTrue(
            self.controller.settings.get("monitor", {}).get("telemetry_overlay")
        )

        tray.on_set_receiver_jitter_target(100)
        self.assertEqual(
            self.controller.settings.get("monitor", {}).get("receiver_jitter_target_ms"),
            100,
        )

        tray.on_set_stream_format("720p30")
        self.assertEqual(
            self.controller.settings.get("monitor", {}).get("stream_format"),
            "720p30",
        )
        self.assertEqual(
            self.controller.settings.get("virtual_screen", {}).get("width"),
            1280,
        )
        self.assertEqual(
            self.controller.settings.get("virtual_screen", {}).get("height"),
            720,
        )

        tray.on_set_stream_format("720p60")
        self.assertEqual(
            self.controller.settings.get("monitor", {}).get("stream_format"),
            "720p60",
        )

        tray.on_set_realtime_queue_depth(2)
        self.assertEqual(
            self.controller.settings.get("monitor", {}).get("realtime_queue_depth"),
            2,
        )

        self.controller.virtual_screen_manager.enabled = True
        self.controller.shutdown()
        self.assertFalse(self.controller.virtual_screen_manager.enabled)
        self.assertEqual(self.controller.virtual_screen_manager.disable_calls, 1)
        self.controller.shutdown()
        self.assertEqual(self.controller.virtual_screen_manager.disable_calls, 1)

        tray.timer.stop()
        tray.tray.hide()


if __name__ == "__main__":
    unittest.main()
