#!/usr/bin/env python3
"""Chromecast support for convergence (DashCast mode)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CastDevice:
    name: str
    model_name: str


class ChromecastManager:
    def available(self) -> bool:
        try:
            import pychromecast  # noqa: F401
            return True
        except Exception:
            return False

    def discover(self, timeout: int = 4) -> list[CastDevice]:
        try:
            import pychromecast
        except Exception:
            return []

        try:
            chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
        except Exception:
            return []

        devices = []
        for cast in chromecasts:
            try:
                name = cast.name
                if not name:
                    continue
                devices.append(CastDevice(name=cast.name, model_name=cast.model_name or "Chromecast"))
            except Exception:
                continue

        self._stop_discovery(browser)
        devices.sort(key=lambda d: d.name.lower())
        return devices

    def cast_url(self, device_name: str, url: str) -> None:
        import pychromecast

        browser = None
        chromecasts = []
        try:
            chromecasts, browser = pychromecast.get_listed_chromecasts(friendly_names=[device_name])
            if not chromecasts:
                chromecasts, browser = pychromecast.get_chromecasts(timeout=6)
                chromecasts = [cast for cast in chromecasts if getattr(cast, "name", None) == device_name]
            if not chromecasts:
                raise RuntimeError(f"Chromecast not found: {device_name}")

            cast = chromecasts[0]
            cast.wait(timeout=10)
            from pychromecast.controllers.dashcast import DashCastController

            controller = DashCastController()
            cast.register_handler(controller)
            controller.load_url(url, force=True, reload_seconds=0)
        finally:
            self._stop_discovery(browser)

    def _stop_discovery(self, browser) -> None:
        if not browser:
            return
        try:
            import pychromecast

            pychromecast.discovery.stop_discovery(browser)
        except Exception:
            pass
