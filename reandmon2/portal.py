#!/usr/bin/env python3
"""
Thin wrapper around org.freedesktop.portal.ScreenCast

"""

import uuid
import dbus
from dbus.mainloop.glib import DBusGMainLoop

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

DBusGMainLoop(set_as_default=True)
bus = dbus.SessionBus()
portal_obj = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
sc = dbus.Interface(portal_obj, "org.freedesktop.portal.ScreenCast")

# org.freedesktop.portal.ScreenCast cursor mode 1 hides the compositor cursor.
# Hyprland's embedded cursor mode currently increases latency and can exhaust
# screencopy buffers, so convergence draws a lightweight browser overlay instead.
CURSOR_MODE_HIDDEN = dbus.UInt32(1)


def _wait_response(request_path, timeout_seconds=30):
    loop = GLib.MainLoop()
    out = {"timed_out": False}

    def _on_response(code, results):
        if out.get("done"):
            return
        out["done"] = True
        out["code"] = int(code)
        out["results"] = {k: v for k, v in results.items()}
        loop.quit()

    def _on_timeout():
        if out.get("done"):
            return False
        out["done"] = True
        out["timed_out"] = True
        loop.quit()
        return False

    bus.add_signal_receiver(
        _on_response,
        signal_name="Response",
        dbus_interface="org.freedesktop.portal.Request",
        path=request_path,
    )
    timeout_id = GLib.timeout_add_seconds(int(timeout_seconds), _on_timeout)
    try:
        loop.run()
    finally:
        try:
            GLib.source_remove(timeout_id)
        except Exception:
            pass
        try:
            bus.remove_signal_receiver(
                _on_response,
                signal_name="Response",
                dbus_interface="org.freedesktop.portal.Request",
                path=request_path,
            )
        except Exception:
            pass
    if out.get("timed_out"):
        raise TimeoutError(f"Portal request timed out after {timeout_seconds}s: {request_path}")
    return out.get("code", -1), out.get("results", {})


def create_session():
    token = uuid.uuid4().hex
    opts = {
        "session_handle_token": dbus.String(token),
        "handle_token": dbus.String(token),
        "types": dbus.UInt32(7),  # monitor+window+virtual
        "cursor_mode": CURSOR_MODE_HIDDEN,
    }
    req = sc.CreateSession(opts)
    code, res = _wait_response(req)
    if code or "session_handle" not in res:
        raise RuntimeError(f"CreateSession failed ({code}): {res}")
    return res["session_handle"]


def select_sources(sess):
    token = uuid.uuid4().hex
    opts = {
        "types": dbus.UInt32(1),  # screens only
        "multiple": dbus.Boolean(False),
        "cursor_mode": CURSOR_MODE_HIDDEN,
        "handle_token": dbus.String(token),
    }
    req = sc.SelectSources(sess, opts)
    code, _ = _wait_response(req)
    if code:
        raise RuntimeError(f"SelectSources failed ({code})")


def start_session(session_handle):
    token = uuid.uuid4().hex
    opts = {'handle_token': dbus.String(token)}
    request_path = sc.Start(session_handle, '', opts)
    code, res = _wait_response(request_path)
    if code != 0 or 'streams' not in res or not res['streams']:
        raise RuntimeError(f'Start failed ({code}): {res}')

    # Extract stream properties to get resolution information
    stream_properties = res['streams'][0][1]
    print(f"Server: Stream properties: {stream_properties}")

    # Extract resolution from stream properties
    width, height = stream_properties['size']
    print(f"Server: Detected screen resolution: {width}x{height}")
    return res['streams'][0][0], width, height


def close_session(sess):
    session_obj = bus.get_object("org.freedesktop.portal.Desktop", sess)
    session_iface = dbus.Interface(session_obj, "org.freedesktop.portal.Session")
    session_iface.Close()
    print(f"Session {sess} closed.")
