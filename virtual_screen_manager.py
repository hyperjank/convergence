#!/usr/bin/env python3
"""Hyprland headless-output lifecycle for Convergence."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from settings import SettingsStore


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class VirtualScreenStatus:
    available: bool
    enabled: bool
    output_name: str | None = None
    width: int | None = None
    height: int | None = None
    refresh_rate: float | None = None


class VirtualScreenManager:
    """Create, configure, and remove one Convergence-owned headless output."""

    def __init__(
        self,
        settings: SettingsStore,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self.settings = settings
        self._runner = runner

    def available(self) -> bool:
        return shutil.which("hyprctl") is not None

    def status(self) -> VirtualScreenStatus:
        if not self.available():
            return VirtualScreenStatus(False, False)
        try:
            monitors = self._monitors()
        except Exception:
            return VirtualScreenStatus(True, False)

        configured_name = self._config().get("output_name")
        if not configured_name:
            return VirtualScreenStatus(True, False)

        monitor = next(
            (item for item in monitors if item.get("name") == configured_name and not item.get("disabled")),
            None,
        )
        if monitor is None:
            return VirtualScreenStatus(True, False)
        return VirtualScreenStatus(
            available=True,
            enabled=True,
            output_name=str(monitor.get("name")),
            width=int(monitor.get("width", 0)) or None,
            height=int(monitor.get("height", 0)) or None,
            refresh_rate=float(monitor.get("refreshRate", 0.0)) or None,
        )

    def enable(self) -> VirtualScreenStatus:
        existing = self.status()
        if existing.enabled:
            return existing
        if not self.available():
            raise RuntimeError("hyprctl is not installed")

        before = self._monitors()
        before_names = {str(item.get("name")) for item in before}
        self._hyprctl("output", "create", "headless")

        after = self._monitors()
        created = [item for item in after if str(item.get("name")) not in before_names]
        if len(created) != 1:
            raise RuntimeError("Hyprland did not report exactly one new virtual output")

        output_name = str(created[0].get("name", ""))
        if not output_name:
            raise RuntimeError("Hyprland created an unnamed virtual output")

        cfg = self._config()
        width = self._positive_int(cfg.get("width"), 1920)
        height = self._positive_int(cfg.get("height"), 1080)
        refresh_rate = self._positive_int(cfg.get("refresh_rate"), 60)
        position = self._adjacent_position(before)
        code = (
            "hl.monitor({ "
            f"output = {json.dumps(output_name)}, "
            f"mode = {json.dumps(f'{width}x{height}@{refresh_rate}')}, "
            f"position = {json.dumps(position)}, scale = 1 "
            "})"
        )

        try:
            self._hyprctl("eval", code)
            cfg.update(
                output_name=output_name,
                width=width,
                height=height,
                refresh_rate=refresh_rate,
            )
            self.settings.set("virtual_screen", cfg)
            enabled = self.status()
            if not enabled.enabled:
                raise RuntimeError("Virtual output disappeared after configuration")
            return enabled
        except Exception:
            try:
                self._hyprctl("output", "remove", output_name)
            except Exception:
                pass
            raise

    def disable(self) -> None:
        current = self.status()
        cfg = self._config()
        if current.enabled and current.output_name:
            self._hyprctl("output", "remove", current.output_name)
            remaining = {str(item.get("name")) for item in self._monitors()}
            if current.output_name in remaining:
                raise RuntimeError(f"Hyprland did not remove {current.output_name}")
        cfg["output_name"] = None
        self.settings.set("virtual_screen", cfg)

    def move_active_window(self) -> str:
        current = self.status()
        if not current.enabled or not current.output_name:
            raise RuntimeError("Virtual screen is not enabled")
        monitor = next(
            item for item in self._monitors() if item.get("name") == current.output_name
        )
        workspace = monitor.get("activeWorkspace", {}).get("name")
        if not workspace:
            raise RuntimeError("Virtual screen has no active workspace")
        self._hyprctl("dispatch", "movetoworkspacesilent", str(workspace))
        return str(workspace)

    def _config(self) -> dict[str, Any]:
        value = self.settings.get("virtual_screen", {})
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _positive_int(value: Any, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    @staticmethod
    def _adjacent_position(monitors: list[dict[str, Any]]) -> str:
        active = [item for item in monitors if not item.get("disabled")]
        if not active:
            return "0x0"
        focused = next((item for item in active if item.get("focused")), active[0])
        scale = float(focused.get("scale", 1.0)) or 1.0
        logical_width = round(float(focused.get("width", 0)) / scale)
        x = round(float(focused.get("x", 0))) + logical_width
        y = round(float(focused.get("y", 0)))
        return f"{x}x{y}"

    def _monitors(self) -> list[dict[str, Any]]:
        raw = self._hyprctl("-j", "monitors", "all")
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise RuntimeError("Unexpected response from hyprctl monitors")
        return [item for item in parsed if isinstance(item, dict)]

    def _hyprctl(self, *args: str) -> str:
        command: Sequence[str] = ("hyprctl", *args)
        result = self._runner(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "hyprctl failed").strip()
            raise RuntimeError(detail)
        output = (result.stdout or "").strip()
        if output.lower().startswith("error") or "can't work" in output.lower():
            raise RuntimeError(output)
        return output
