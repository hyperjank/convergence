"""Validation helpers for receiver-supplied WebRTC telemetry."""

from __future__ import annotations

import math


NUMERIC_FIELDS = {
    "timestampMs",
    "width",
    "height",
    "jitterBufferMs",
    "jitterBufferTargetMs",
    "jitterBufferMinimumMs",
    "jitterBufferTargetSupported",
    "jitterBufferTargetRequestedMs",
    "jitterBufferTargetReadbackMs",
    "jitterBufferTargetApplied",
    "decodeFps",
    "renderFps",
    "framesDropped",
    "framesDroppedDelta",
    "packetsLost",
    "packetsLostDelta",
    "packetLossPercent",
    "jitterMs",
    "rttMs",
    "bitrateMbps",
}

INTEGER_FIELDS = {
    "width",
    "height",
    "framesDropped",
    "framesDroppedDelta",
    "packetsLost",
    "packetsLostDelta",
    "jitterBufferTargetSupported",
    "jitterBufferTargetApplied",
}


def sanitize_receiver_telemetry(message: dict) -> dict | None:
    """Return a finite, bounded telemetry sample or reject it."""
    sample: dict[str, float | int | None] = {}
    for field in NUMERIC_FIELDS:
        value = message.get(field)
        if value is None:
            sample[field] = None
            continue
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        sample[field] = int(number) if field in INTEGER_FIELDS else number

    width = sample.get("width")
    height = sample.get("height")
    if isinstance(width, int) and not 0 <= width <= 16384:
        return None
    if isinstance(height, int) and not 0 <= height <= 16384:
        return None
    for field in ("jitterBufferTargetSupported", "jitterBufferTargetApplied"):
        value = sample.get(field)
        if isinstance(value, int) and value not in (0, 1):
            return None
    for field in ("jitterBufferTargetRequestedMs", "jitterBufferTargetReadbackMs"):
        value = sample.get(field)
        if isinstance(value, (int, float)) and not 0 <= value <= 4000:
            return None
    return sample
