#!/usr/bin/env python3
"""Manage concurrent scrcpy sessions."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Literal

from device_manager import DeviceManager
from settings import SettingsStore


DISPLAY_ID_PATTERNS = [
    re.compile(r"\(id\s*=\s*(\d+)\)", re.IGNORECASE),
    re.compile(r"\bid\s*=\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bdisplay(?:\s+id)?\s*[#:=-]\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bnew\s+display\s*[#:=-]?\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bdisplay\s+#(\d+)\b", re.IGNORECASE),
]


SessionMode = Literal["normal", "app"]


@dataclass
class ScrcpySession:
    session_id: int
    serial: str
    mode: SessionMode
    process: subprocess.Popen
    package: str | None = None
    display_id: int | None = None
    started_at: float = 0.0
    mismatch_count: int = 0
    last_top_pkg: str | None = None
    has_seen_target: bool = False
    auto_close_state: str = "pending"


class ScrcpyManager:
    def __init__(self, device_manager: DeviceManager, settings: SettingsStore) -> None:
        self.device_manager = device_manager
        self.settings = settings
        self._next_id = 1
        self._sessions: dict[int, ScrcpySession] = {}
        self._lock = threading.Lock()
        self._immersive_prev: dict[str, str] = {}

    def list_sessions(self) -> list[ScrcpySession]:
        self._reap()
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.session_id)

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                session.process.terminate()
            except Exception:
                pass
        self._reap(force=True)

    def stop_session(self, session_id: int) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return
        try:
            session.process.terminate()
        except Exception:
            pass
        self._reap(force=True)

    def launch_normal(
        self,
        serial: str,
        *,
        turn_screen_off: bool | None = None,
        stay_awake: bool | None = None,
    ) -> int:
        scrcpy_cfg = self.settings.get("scrcpy", {})
        if turn_screen_off is None:
            turn_screen_off = bool(scrcpy_cfg.get("turn_screen_off", False))
        if stay_awake is None:
            stay_awake = bool(scrcpy_cfg.get("stay_awake", True))

        cmd = ["scrcpy", "--serial", serial]
        if turn_screen_off:
            cmd.append("--turn-screen-off")
        if stay_awake:
            cmd.append("--stay-awake")

        proc = subprocess.Popen(cmd)
        return self._register(proc, serial, "normal")

    def launch_app_virtual(
        self,
        serial: str,
        package: str,
        *,
        resolution: str | None = None,
        turn_screen_off: bool | None = None,
        stay_awake: bool | None = None,
    ) -> int:
        scrcpy_cfg = self.settings.get("scrcpy", {})
        if resolution is None:
            resolution = str(scrcpy_cfg.get("resolution", "1920x1080"))
        if turn_screen_off is None:
            turn_screen_off = bool(scrcpy_cfg.get("turn_screen_off", False))
        if stay_awake is None:
            stay_awake = bool(scrcpy_cfg.get("stay_awake", True))

        # Some devices ignore app-start while the screen is off; wake first.
        self.wake_device_screen(serial)

        cmd = [
            "scrcpy",
            "--serial",
            serial,
            f"--new-display={resolution}",
            "--start-app",
            package,
            "--window-title",
            f"{package} [{serial}]",
        ]
        if turn_screen_off:
            cmd.append("--turn-screen-off")
        if stay_awake:
            cmd.append("--stay-awake")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        session_id = self._register(proc, serial, "app", package=package)
        self._update_immersive_policy(serial)
        self._start_output_parser(session_id)
        self._start_app_watchdog(session_id)
        return session_id

    def list_launchable_packages(self, serial: str, include_system: bool = False) -> list[str]:
        user = self.device_manager.current_user(serial)
        code, out, _ = self.device_manager.run_adb(
            [
                "shell",
                "cmd",
                "package",
                "query-activities",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "--user",
                user,
            ],
            serial=serial,
            timeout=8,
        )
        if code != 0:
            return []

        launchable: set[str] = set()
        for line in out.splitlines():
            line = line.strip()
            if not line or line.endswith(":") or "/" not in line:
                continue
            launchable.add(line.split("/", 1)[0].strip())

        if include_system:
            return sorted(x for x in launchable if x)

        code, out, _ = self.device_manager.run_adb(
            ["shell", "cmd", "package", "list", "packages", "-3", "--user", user],
            serial=serial,
            timeout=8,
        )
        if code != 0:
            return sorted(x for x in launchable if x)

        user_pkgs = {
            line.strip().replace("package:", "")
            for line in out.splitlines()
            if line.strip().startswith("package:")
        }
        return sorted(x for x in launchable.intersection(user_pkgs) if x)

    def wake_device_screen(self, serial: str) -> None:
        # Best-effort wake + dismiss keyguard. Failures are intentionally ignored.
        self.device_manager.run_adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial=serial, timeout=1.5)
        self.device_manager.run_adb(["shell", "wm", "dismiss-keyguard"], serial=serial, timeout=1.5)

    def _register(
        self,
        proc: subprocess.Popen,
        serial: str,
        mode: SessionMode,
        *,
        package: str | None = None,
    ) -> int:
        session_id = self._next_id
        self._next_id += 1
        with self._lock:
            self._sessions[session_id] = ScrcpySession(
                session_id=session_id,
                serial=serial,
                mode=mode,
                process=proc,
                package=package,
                started_at=time.time(),
            )
        return session_id

    def _reap(self, force: bool = False) -> None:
        with self._lock:
            snapshot = list(self._sessions.items())
        for sid, session in snapshot:
            if force:
                try:
                    session.process.wait(timeout=0.2)
                except Exception:
                    pass
            if session.process.poll() is not None:
                with self._lock:
                    self._sessions.pop(sid, None)
                if session.mode == "app":
                    self._update_immersive_policy(session.serial)

    def _start_output_parser(self, session_id: int) -> None:
        def _worker() -> None:
            with self._lock:
                session = self._sessions.get(session_id)
            if not session or not session.process.stdout:
                return
            try:
                for raw_line in session.process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    display_id = self._extract_display_id(line)
                    if display_id is not None:
                        with self._lock:
                            live = self._sessions.get(session_id)
                            if live:
                                if live.display_id != display_id:
                                    print(
                                        f"ScrcpyManager: session {session_id} display id={display_id} "
                                        f"pkg={live.package}"
                                    )
                                live.display_id = display_id
            except Exception:
                return

        threading.Thread(target=_worker, daemon=True).start()

    def _start_app_watchdog(self, session_id: int) -> None:
        def _worker() -> None:
            while True:
                time.sleep(0.6)
                self._reap()
                with self._lock:
                    session = self._sessions.get(session_id)
                if not session:
                    return
                if session.process.poll() is not None:
                    return
                if session.mode != "app" or not session.package:
                    continue

                if session.display_id is None:
                    # Wait a bit for scrcpy logs to expose the virtual display id.
                    if time.time() - session.started_at < 12:
                        continue
                    # No display id discovered; skip auto-close to avoid false positives.
                    self._set_auto_close_state(
                        session_id,
                        "disabled",
                        "no virtual display id discovered",
                    )
                    continue

                top_pkg = self._window_focus_package_for_display(session.serial, session.display_id)
                if top_pkg is None:
                    top_pkg = self._top_package_for_display(session.serial, session.display_id)
                if top_pkg is None:
                    continue

                if top_pkg != session.last_top_pkg:
                    with self._lock:
                        live = self._sessions.get(session_id)
                        if live:
                            live.last_top_pkg = top_pkg
                    print(
                        f"ScrcpyManager: session {session_id} top pkg={top_pkg} "
                        f"target={session.package} display={session.display_id}"
                    )

                if top_pkg == session.package:
                    with self._lock:
                        live = self._sessions.get(session_id)
                        if live:
                            live.mismatch_count = 0
                            live.has_seen_target = True
                            live.auto_close_state = "active"
                    continue

                # Do not auto-close until we have positively observed the target app once.
                # Some devices report launcher during initial display setup even when app launch is in-flight.
                if not session.has_seen_target:
                    if time.time() - session.started_at < 15:
                        continue
                    # After grace period, if target app was never seen, disable auto-close for safety.
                    # Keep session running rather than killing valid launches due to ambiguous activity data.
                    self._set_auto_close_state(
                        session_id,
                        "disabled",
                        "target app was never observed on virtual display",
                    )
                    continue

                with self._lock:
                    live = self._sessions.get(session_id)
                    if live:
                        live.mismatch_count += 1
                        mismatch_count = live.mismatch_count
                    else:
                        mismatch_count = 0

                # Require consecutive mismatches to avoid killing during transitions.
                if mismatch_count >= 2:
                    print(
                        f"ScrcpyManager: session {session_id} closing (top pkg changed) "
                        f"{session.package} -> {top_pkg}"
                    )
                    try:
                        session.process.terminate()
                    except Exception:
                        pass
                    return

        threading.Thread(target=_worker, daemon=True).start()

    def _set_auto_close_state(self, session_id: int, state: str, reason: str) -> None:
        with self._lock:
            live = self._sessions.get(session_id)
            if not live or live.auto_close_state == state:
                return
            live.auto_close_state = state
            package = live.package
        print(f"ScrcpyManager: session {session_id} auto-close {state}: {reason} pkg={package}")

    def _extract_display_id(self, line: str) -> int | None:
        for pattern in DISPLAY_ID_PATTERNS:
            match = pattern.search(line)
            if match:
                try:
                    return int(match.group(1))
                except Exception:
                    continue
        return None

    def _top_package_for_display(self, serial: str, display_id: int) -> str | None:
        code, out, _ = self.device_manager.run_adb(
            ["shell", "dumpsys", "activity", "displays"],
            serial=serial,
            timeout=4,
        )
        if code != 0 or not out:
            return None

        block = self._extract_display_block(out, display_id)
        if not block:
            return None

        patterns = [
            re.compile(r"topResumedActivity=.*?\s([a-zA-Z0-9_.]+)/"),
            re.compile(r"mResumedActivity:.*?\s([a-zA-Z0-9_.]+)/"),
            re.compile(r"topActivity=.*?\s([a-zA-Z0-9_.]+)/"),
            re.compile(r"realActivity=([a-zA-Z0-9_.]+)/"),
            re.compile(rf"displayId={display_id}.*?\s([a-zA-Z0-9_.]+)/"),
            re.compile(rf"\s([a-zA-Z0-9_.]+)/.*?displayId={display_id}"),
        ]
        for pattern in patterns:
            match = pattern.search(block)
            if match:
                return match.group(1)
        return None

    def _extract_display_block(self, text: str, display_id: int) -> str | None:
        starts = []
        for marker in (
            f"Display #{display_id}",
            f"Display {display_id}",
            f"displayId={display_id}",
        ):
            idx = text.find(marker)
            if idx >= 0:
                starts.append(idx)
        if not starts:
            return None
        start = min(starts)

        next_indices = []
        for next_marker in ("Display #", "Display ", "displayId="):
            idx = text.find(next_marker, start + 1)
            if idx > start:
                next_indices.append(idx)
        next_idx = min(next_indices) if next_indices else -1
        if next_idx < 0:
            return text[start:]
        return text[start:next_idx]

    def _window_focus_package_for_display(self, serial: str, display_id: int) -> str | None:
        code, out, _ = self.device_manager.run_adb(
            ["shell", "dumpsys", "window", "displays"],
            serial=serial,
            timeout=4,
        )
        if code != 0 or not out:
            return None

        block = self._extract_display_block(out, display_id)
        if not block:
            return None

        patterns = [
            re.compile(r"mCurrentFocus=.*?\s([a-zA-Z0-9_.]+)/"),
            re.compile(r"mFocusedApp=.*?\s([a-zA-Z0-9_.]+)/"),
            re.compile(r"Window\{[^\n]*\s([a-zA-Z0-9_.]+)/"),
        ]
        for pattern in patterns:
            match = pattern.search(block)
            if match:
                return match.group(1)
        return None

    def _update_immersive_policy(self, serial: str) -> None:
        cfg = self.settings.get("scrcpy", {})
        if not bool(cfg.get("immersive_poke", True)):
            return

        with self._lock:
            app_pkgs = sorted(
                {
                    s.package
                    for s in self._sessions.values()
                    if s.mode == "app" and s.serial == serial and s.process.poll() is None and s.package
                }
            )

        if app_pkgs:
            if serial not in self._immersive_prev:
                prev = self._settings_get(serial, "global", "policy_control")
                self._immersive_prev[serial] = prev if prev is not None else "null"
            policy = f"immersive.full={','.join(app_pkgs)}"
            self._settings_put(serial, "global", "policy_control", policy)
            return

        prev = self._immersive_prev.pop(serial, None)
        if prev is None:
            return
        restore = prev if prev and prev.strip() else "null"
        self._settings_put(serial, "global", "policy_control", restore)

    def _settings_get(self, serial: str, scope: str, key: str) -> str | None:
        code, out, _ = self.device_manager.run_adb(
            ["shell", "settings", "get", scope, key],
            serial=serial,
            timeout=2,
        )
        if code != 0:
            return None
        value = (out or "").strip()
        return value or None

    def _settings_put(self, serial: str, scope: str, key: str, value: str) -> bool:
        code, _, _ = self.device_manager.run_adb(
            ["shell", "settings", "put", scope, key, value],
            serial=serial,
            timeout=2,
        )
        return code == 0
