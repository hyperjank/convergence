#!/usr/bin/env python3
"""Quick local capture test: PipeWire -> video sink."""

import signal
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib

try:
    from .portal_session import PortalSession
except ImportError:
    from portal_session import PortalSession


def main() -> int:
    Gst.init(None)
    portal = PortalSession()

    try:
        pw_id, width, height = portal.start()
        print(f"Test: PipeWire node id {pw_id}, resolution {width}x{height}")
    except Exception as exc:
        print(f"Test: Failed to start portal session: {exc}")
        return 1

    desc = (
        f"pipewiresrc path={pw_id} do-timestamp=true min-buffers=8 max-buffers=64 ! "
        "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        "videoconvert ! "
        "autovideosink sync=false"
    )
    print("Test pipeline:", desc)

    try:
        pipeline = Gst.parse_launch(desc)
    except Exception as exc:
        print(f"Test: Failed to build pipeline: {exc}")
        portal.stop()
        return 1

    loop = GLib.MainLoop()

    def _stop(*_):
        try:
            pipeline.set_state(Gst.State.NULL)
        finally:
            portal.stop()
            loop.quit()
        return True

    signal.signal(signal.SIGINT, lambda *_: _stop())
    signal.signal(signal.SIGTERM, lambda *_: _stop())

    pipeline.set_state(Gst.State.PLAYING)
    print("Test: Pipeline playing; Ctrl+C to stop.")
    try:
        loop.run()
    finally:
        try:
            pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass
        portal.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
