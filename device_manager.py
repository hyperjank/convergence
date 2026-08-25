#!/usr/bin/env python3
"""ADB device discovery and helper commands."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str
    model: str | None = None


class DeviceManager:
    def __init__(self) -> None:
        pass

    def adb_available(self) -> bool:
        try:
            subprocess.run(
                ["adb", "version"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return True
        except FileNotFoundError:
            return False

    def run_adb(
        self,
        args: Sequence[str],
        *,
        serial: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        cmd = ["adb"]
        if serial:
            cmd.extend(["-s", serial])
        cmd.extend(args)
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"

    def list_devices(self) -> list[DeviceInfo]:
        code, out, _ = self.run_adb(["devices", "-l"], timeout=4)
        if code != 0:
            return []

        devices: list[DeviceInfo] = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue

            serial = parts[0]
            state = parts[1]
            model = None
            for token in parts[2:]:
                if token.startswith("model:"):
                    model = token.split(":", 1)[1].replace("_", " ")
                    break
            devices.append(DeviceInfo(serial=serial, state=state, model=model))
        return devices

    def current_user(self, serial: str) -> str:
        code, out, _ = self.run_adb(["shell", "am", "get-current-user"], serial=serial, timeout=4)
        out = out.strip()
        if code == 0 and out.isdigit():
            return out
        return "0"

    def kill_server(self) -> bool:
        code, _, _ = self.run_adb(["kill-server"], timeout=5)
        return code == 0

    def start_server(self) -> bool:
        code, _, _ = self.run_adb(["start-server"], timeout=5)
        return code == 0

    def connect(self, endpoint: str) -> tuple[bool, str]:
        endpoint = endpoint.strip()
        if not endpoint:
            return False, "Empty endpoint"
        code, out, err = self.run_adb(["connect", endpoint], timeout=8)
        text = (out or err).strip()
        if code != 0:
            return False, text or f"adb connect failed ({code})"
        lowered = text.lower()
        ok = "connected to" in lowered or "already connected to" in lowered
        return ok, text or "connect finished"
