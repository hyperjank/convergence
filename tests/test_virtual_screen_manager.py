from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from settings import SettingsStore
from virtual_screen_manager import VirtualScreenManager


class FakeHyprctl:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.monitors = [
            {
                "name": "eDP-1",
                "width": 3840,
                "height": 2400,
                "refreshRate": 60.0,
                "x": 559,
                "y": 1080,
                "scale": 2,
                "focused": True,
                "disabled": False,
                "activeWorkspace": {"name": "1"},
            }
        ]

    def __call__(self, command, **_kwargs):
        args = list(command)
        self.commands.append(args)
        stdout = "ok"
        if args[1:] == ["-j", "monitors", "all"]:
            stdout = json.dumps(self.monitors)
        elif args[1:] == ["output", "create", "headless"]:
            self.monitors.append(
                {
                    "name": "HEADLESS-1",
                    "width": 1920,
                    "height": 1080,
                    "refreshRate": 60.0,
                    "x": 2479,
                    "y": 1080,
                    "scale": 1,
                    "focused": False,
                    "disabled": False,
                    "activeWorkspace": {"name": "10"},
                }
            )
        elif args[1:3] == ["output", "remove"]:
            name = args[3]
            self.monitors = [item for item in self.monitors if item["name"] != name]
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


class VirtualScreenManagerTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = SettingsStore(Path(self.tempdir.name) / "config.json")
        self.hyprctl = FakeHyprctl()
        self.manager = VirtualScreenManager(self.settings, runner=self.hyprctl)
        self.which = patch("virtual_screen_manager.shutil.which", return_value="/usr/bin/hyprctl")
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.tempdir.cleanup()

    def test_enable_configures_adjacent_scale_one_output(self):
        status = self.manager.enable()

        self.assertTrue(status.enabled)
        self.assertEqual(status.output_name, "HEADLESS-1")
        eval_command = next(args for args in self.hyprctl.commands if args[1] == "eval")
        self.assertIn('position = "2479x1080"', eval_command[2])
        self.assertIn("scale = 1", eval_command[2])
        self.assertEqual(
            self.settings.get("virtual_screen", {}).get("output_name"),
            "HEADLESS-1",
        )

    def test_enable_uses_configured_native_resolution(self):
        self.settings.update_virtual_screen(width=1280, height=720, refresh_rate=60)

        self.manager.enable()

        eval_command = next(args for args in self.hyprctl.commands if args[1] == "eval")
        self.assertIn('mode = "1280x720@60"', eval_command[2])

    def test_disable_only_removes_recorded_output(self):
        self.manager.enable()
        self.manager.disable()

        self.assertFalse(self.manager.status().enabled)
        self.assertEqual(self.settings.get("virtual_screen", {}).get("output_name"), None)
        self.assertIn(
            ["hyprctl", "output", "remove", "HEADLESS-1"],
            self.hyprctl.commands,
        )

    def test_move_active_window_targets_virtual_workspace(self):
        self.manager.enable()
        workspace = self.manager.move_active_window()

        self.assertEqual(workspace, "10")
        self.assertIn(
            ["hyprctl", "dispatch", "movetoworkspacesilent", "10"],
            self.hyprctl.commands,
        )


if __name__ == "__main__":
    unittest.main()
