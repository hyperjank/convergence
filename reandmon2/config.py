"""Simple streaming configuration."""

from __future__ import annotations

import os

HOST = "0.0.0.0"
HTTP_PORT = 8000
WS_PORT = 8767
HOST_IP = os.getenv("HOST_IP", "").strip() or None

# WebCodecs codec string for H.264 baseline.
WC_H264 = "avc1.42001E"

# Optional override to force a specific host IP in the client URL.

# Ordered encoder candidates. The first available on the host wins.
ENCODER_PIPELINES = [
    (
        "vah264enc",
        (
            "vah264enc target-usage=7 rate-control=cbr cabac=false bitrate=18000 key-int-max=15 ! "
            "h264parse config-interval=1 ! "
            "video/x-h264,stream-format=avc,alignment=au"
        ),
    ),
    (
        "vah264lpenc",
        (
            "vah264lpenc target-usage=7 rate-control=cqp cabac=false "
            "qpi=24 qpp=26 key-int-max=30 ! "
            "h264parse config-interval=1 ! "
            "video/x-h264,profile=constrained-baseline,stream-format=avc,alignment=au"
        ),
    ),
    (
        "vaapih264enc",
        (
            "vaapih264enc rate-control=cbr bitrate=8000 ! "
            "h264parse config-interval=1 ! "
            "video/x-h264,stream-format=avc,alignment=au"
        ),
    ),
    (
        "nvh264enc",
        (
            "nvh264enc bitrate=20000 iframeinterval=15 zerolatency=true ! "
            "h264parse config-interval=1 ! "
            "video/x-h264,stream-format=avc,alignment=au"
        ),
    ),
    (
        "x264enc",
        (
            "x264enc speed-preset=fast bitrate=15000 key-int-max=5 ! "
            "h264parse config-interval=1 ! "
            "video/x-h264,stream-format=avc,alignment=au"
        ),
    ),
]
