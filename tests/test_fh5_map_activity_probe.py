"""Single-range activity recording safeguards, using only fake memory/time."""
from argparse import Namespace
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.fh5_map_activity_probe import capture, tracked_fields, validate_args


BASE = 0x12340000


def args(**changes):
    values = dict(pid=42, session_key="42:created:image", expect_version="3.688.109.0",
                  base=BASE, offset=0, size=4096, samples=50, interval=.04,
                  seconds=8, phase="open_static")
    values.update(changes)
    return Namespace(**values)


class Clock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value

    def sleep(self, delay):
        self.value += delay


class Process:
    identity = {"pid": 42, "session_key": "42:created:image", "image_path": "missing/FH5.exe"}
    min_address = 0x10000
    max_address = 0x7fffffffffff
    last_error = {"detail": "fake exact read error"}

    def __init__(self, failures=()):
        self.calls, self.failures = [], set(failures)

    def regions(self, **kwargs):
        return [dict(base=BASE - 8192, size=32768, state=0x1000, type=0x20000, protect=4)]

    def read(self, address, size):
        index = len(self.calls)
        self.calls.append((address, size))
        if index in self.failures:
            return None
        data = bytearray(size)
        struct.pack_into("<I", data, 0x194, index)
        struct.pack_into("<f", data, 0xB0, index / 10)
        return bytes(data)


class ActivityProbeTests(unittest.TestCase):
    def run_capture(self, process, options=None):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "test.bin"
            with patch("tools.fh5_map_activity_probe.package_version", return_value="3.688.109.0"):
                with patch("tools.fh5_map_activity_probe.time.monotonic", clock.monotonic):
                    with patch("tools.fh5_map_activity_probe.time.sleep", clock.sleep):
                        report = capture(process, options or args(), raw)
            return report, raw.read_bytes() if raw.exists() else b""

    def test_exact_single_range_and_continuous_binary_offsets(self):
        process = Process()
        report, raw = self.run_capture(process)
        self.assertEqual(set(process.calls), {(BASE, 4096)})
        self.assertEqual(report["successful_frames"], 50)
        self.assertEqual(len(raw), 50 * 4096)
        self.assertEqual(report["raw"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual([f["file_offset"] for f in report["frames"]], list(range(0, len(raw), 4096)))
        self.assertTrue(all(f["read_finished_monotonic"] >= f["sample_monotonic"]
                            and f["captured_utc"].endswith("+00:00") for f in report["frames"]))
        self.assertEqual(report["tracked_field_summary"]["0x194"]["unique_raw_values"], 50)
        self.assertIs(report["activity_is_map_open_flag"], False)

    def test_failed_reads_are_never_padded_and_count_against_budget(self):
        report, raw = self.run_capture(Process(failures=(1,)))
        self.assertEqual(report["read_failures"], 1)
        self.assertEqual(report["attempted_bytes"], 50 * 4096)
        self.assertEqual(len(raw), 49 * 4096)
        self.assertIsNone(report["frames"][1]["file_offset"])
        self.assertEqual(report["frames"][2]["file_offset"], 4096)
        self.assertNotIn("changed_bytes_from_previous_successful_frame", report["frames"][2])

    def test_three_consecutive_failures_stop_recording(self):
        process = Process(failures=(0, 1, 2))
        report, raw = self.run_capture(process)
        self.assertEqual(len(process.calls), 3)
        self.assertEqual(report["stop_reason"], "three_consecutive_exact_read_failures")
        self.assertEqual(raw, b"")

    def test_deadline_limits_frame_count(self):
        report, _ = self.run_capture(Process(), args(seconds=.2))
        self.assertLess(report["frame_count"], 50)
        self.assertLessEqual(report["budget"]["elapsed_seconds"], .2 + 1e-8)
        self.assertEqual(report["stop_reason"], "time_or_byte_budget")

    def test_stale_session_or_version_rejected_before_payload_reads(self):
        process = Process()
        with self.assertRaisesRegex(ValueError, "creation identity"):
            self.run_capture(process, args(session_key="42:old:image"))
        self.assertEqual(process.calls, [])
        with self.assertRaisesRegex(ValueError, "package version"):
            self.run_capture(process, args(expect_version="wrong"))
        self.assertEqual(process.calls, [])

    def test_guarded_or_outside_range_rejected(self):
        process = Process()
        with patch.object(process, "regions", return_value=[dict(base=BASE, size=4096,
                                                                 state=0x1000, type=0x20000, protect=0x104)]):
            with self.assertRaisesRegex(ValueError, "committed writable private"):
                self.run_capture(process)
        self.assertEqual(process.calls, [])

    def test_hard_limits_and_nonfinite_values_rejected(self):
        for options in (args(samples=101), args(interval=.01), args(seconds=9),
                        args(seconds=float("nan")), args(size=16384), args(base=BASE+1)):
            with self.assertRaises(ValueError):
                validate_args(options)

    def test_tracked_float_nonfinite_retains_raw_without_invalid_json_float(self):
        blob = bytearray(4096)
        struct.pack_into("<f", blob, 0xB0, float("nan"))
        result = tracked_fields(blob, start_offset=0)
        self.assertIsNone(result["0xb0"]["f32"])
        self.assertEqual(result["0xb0"]["raw_hex"], blob[0xB0:0xB4].hex())


if __name__ == "__main__":
    unittest.main()
