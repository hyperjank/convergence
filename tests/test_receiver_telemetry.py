from __future__ import annotations

import unittest

from reandmon2.telemetry import sanitize_receiver_telemetry


class ReceiverTelemetryTest(unittest.TestCase):
    def test_sanitizes_finite_receiver_sample(self):
        sample = sanitize_receiver_telemetry(
            {
                "timestampMs": 123456,
                "width": 1920,
                "height": 1080,
                "jitterBufferMs": 42.25,
                "jitterBufferTargetSupported": 1,
                "jitterBufferTargetRequestedMs": 100,
                "jitterBufferTargetReadbackMs": 100,
                "jitterBufferTargetApplied": 1,
                "decodeFps": 29.97,
                "framesDroppedDelta": 1,
                "packetLossPercent": 0.125,
                "rttMs": 8.5,
            }
        )

        self.assertIsNotNone(sample)
        self.assertEqual(sample["width"], 1920)
        self.assertEqual(sample["framesDroppedDelta"], 1)
        self.assertAlmostEqual(sample["jitterBufferMs"], 42.25)
        self.assertIsNone(sample["jitterBufferTargetMs"])
        self.assertEqual(sample["jitterBufferTargetSupported"], 1)

    def test_rejects_non_finite_or_implausible_receiver_sample(self):
        self.assertIsNone(
            sanitize_receiver_telemetry({"decodeFps": float("nan")})
        )
        self.assertIsNone(
            sanitize_receiver_telemetry({"width": 99999})
        )
        self.assertIsNone(
            sanitize_receiver_telemetry({"rttMs": True})
        )
        self.assertIsNone(
            sanitize_receiver_telemetry({"jitterBufferTargetSupported": 2})
        )
        self.assertIsNone(
            sanitize_receiver_telemetry({"jitterBufferTargetRequestedMs": 5000})
        )

if __name__ == "__main__":
    unittest.main()
