"""Investigation safeguards and pure filters; never open or read a game."""
from argparse import Namespace
import importlib
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.fh5_map_probe import (ReadBudget, candidates_from_chunk, matrix_shape,
                                 nearby_matrices, parser, scan_process,
                                 select_regions, snapshot_process)


VIEW = {"width": 1920, "height": 1080}
BASE = 0x10000


def camera_blob(size=2048, camera=256, view_delta=32):
    data = bytearray(size)
    struct.pack_into("<8f", data, camera, 1234, 8000, -2345, 1,
                     1234, 0, -2345, 1)
    struct.pack_into("<3f", data, camera + view_delta, 1920, 1080, 1920 / 1080)
    return data


def region(base=BASE, size=8192, **changes):
    r = dict(base=base, size=size, state=0x1000, type=0x20000, protect=4)
    r.update(changes)
    return r


def scan_args(**changes):
    args = dict(max_region_mib=8192, region_base=[], start=0, back=512,
                details=0, radius=1024, chunk_kib=4, max_candidates=128,
                view_tolerance=2, any_viewports=False, matrix_limit=16)
    args.update(changes)
    return Namespace(**args)


class FakeProcess:
    min_address = BASE
    max_address = 0x7fffffffffff
    last_error = None
    identity = {"session_key": "42:current:image", "image_path": "missing/FH5.exe"}

    def __init__(self, data, *, fail_at=None):
        self.data, self.calls, self.fail_at = bytes(data), [], fail_at

    def regions(self, **kwargs):
        return [region(size=len(self.data))]

    def read(self, address, size):
        self.calls.append((address, size))
        if self.fail_at is not None and address == self.fail_at:
            return None
        offset = address - BASE
        return self.data[offset:offset + size]


class CandidateTests(unittest.TestCase):
    def test_known_layout_raw_bytes_are_coherent_and_unverified(self):
        blob = camera_blob()
        hits = candidates_from_chunk(blob, BASE, VIEW, use_numpy=False)
        self.assertEqual(len(hits), 1)
        c = hits[0]
        self.assertEqual(c["base"], BASE + 256)
        self.assertEqual(c["layout"], {"position": 0, "target": 16, "viewport": 32})
        self.assertEqual(bytes.fromhex(c["raw_hex"]), blob[256:448])
        self.assertIs(c["verified"], False)
        self.assertIsNone(c["map_open"])

    def test_layout_is_measured_not_hardcoded(self):
        hits = candidates_from_chunk(camera_blob(view_delta=416), BASE, VIEW,
                                     use_numpy=False)
        self.assertEqual(hits[0]["layout"]["viewport"], 416)

    def test_numpy_and_scalar_paths_agree(self):
        data = camera_blob(view_delta=160)
        self.assertEqual(candidates_from_chunk(data, BASE, VIEW, use_numpy=False),
                         candidates_from_chunk(data, BASE, VIEW, use_numpy=True))

    def test_reject_bad_homogeneous_or_nonfinite_values(self):
        for offset, value in ((256 + 12, 0), (256 + 4, float("nan")),
                              (256 + 16, 1500), (256 + 20, 9000)):
            data = camera_blob()
            struct.pack_into("<f", data, offset, value)
            self.assertEqual(candidates_from_chunk(data, BASE, VIEW), [])

    def test_viewport_mismatch_requires_explicit_broader_search(self):
        view = {"width": 1280, "height": 720}
        self.assertEqual(candidates_from_chunk(camera_blob(), BASE, view), [])
        hit = candidates_from_chunk(camera_blob(), BASE, view, any_viewports=True)[0]
        self.assertIs(hit["viewport_exact_match"], False)

    def test_invalid_base_and_bounds_rejected(self):
        with self.assertRaises(ValueError):
            candidates_from_chunk(camera_blob(), BASE + 1, VIEW)
        with self.assertRaises(ValueError):
            candidates_from_chunk(camera_blob(), BASE, VIEW, back=10000)


class MatrixTests(unittest.TestCase):
    def matrix(self, theta=0):
        # Orthographic 0.2 px/m with a rotated world basis and nonzero centre.
        a, b = math.cos(theta) * .2, math.sin(theta) * .2
        sx, sy = VIEW["width"] / 2, VIEW["height"] / 2
        x, z = 1234, -2345
        return [a/sx, b/sy, 0, 0, 0, 0, 0, 0,
                -b/sx, a/sy, 0, 0, -(x*a-z*b)/sx, -(x*b+z*a)/sy, .5, 1]

    def test_rotated_basis_retained_as_unverified_hypothesis(self):
        score = matrix_shape(self.matrix(.6), (1234, 0, -2345), VIEW)
        self.assertIsNotNone(score)
        self.assertAlmostEqual(score["pixel_scale_x"], .2)
        self.assertIs(score["verified"], False)

    def test_reject_ui_degenerate_nonfinite_and_wrong_centre(self):
        self.assertIsNone(matrix_shape([0] * 16, (1234, 0, -2345), VIEW))
        self.assertIsNone(matrix_shape([float("nan")] * 16, (0, 0, 0), VIEW))
        self.assertIsNone(matrix_shape(self.matrix(), (10000, 0, 10000), VIEW))
        distorted = self.matrix()
        distorted[9] *= 2
        self.assertIsNone(matrix_shape(distorted, (1234, 0, -2345), VIEW))

    def test_matrix_windows_preserve_original_bytes_and_both_hypotheses(self):
        data = camera_blob()
        struct.pack_into("<16f", data, 800, *self.matrix(.4))
        candidate = candidates_from_chunk(data, BASE, VIEW)[0]
        matrices = nearby_matrices(data, BASE, candidate, VIEW)
        found = next(m for m in matrices if m["address"] == BASE + 800
                     and m["convention"] == "row_vector")
        self.assertEqual(bytes.fromhex(found["raw_hex"]), data[800:864])
        self.assertIs(found["projection_verified"], False)

    def test_matrix_scoring_uses_engine_viewport_instead_of_scaled_capture(self):
        data = camera_blob()
        struct.pack_into("<16f", data, 800, *self.matrix(.4))
        wrong_capture = {"width": 2048, "height": 1280}
        candidate = candidates_from_chunk(data, BASE, wrong_capture, any_viewports=True)[0]
        matrices = nearby_matrices(data, BASE, candidate, wrong_capture)
        found = next(m for m in matrices if m["address"] == BASE + 800
                     and m["convention"] == "row_vector")
        self.assertEqual(found["viewport_hypothesis"]["width"], 1920)
        self.assertEqual(found["viewport_hypothesis"]["height"], 1080)
        self.assertEqual(found["viewport_hypothesis"]["source"],
                         "candidate_viewport_float_triple")


class ScanSafeguardsTests(unittest.TestCase):
    def test_metadata_excludes_mapped_guarded_executable_readonly_and_bad_ranges(self):
        records = [region(), region(base=BASE + 0x3000, type=0x40000),
                   region(base=BASE + 0x6000, protect=0x104),
                   region(base=BASE + 0x9000, protect=0x20),
                   region(base=1), region(base=BASE + 0xC000, size=-1)]
        selected, excluded = select_regions(records)
        self.assertEqual([r["base"] for r in selected], [BASE])
        self.assertEqual(sum(excluded.values()), 5)

    def test_failed_read_consumes_byte_budget(self):
        process = FakeProcess(b"x" * 8192, fail_at=BASE)
        budget = ReadBudget(10, 4096)
        self.assertIsNone(budget.read(process, BASE, 4096))
        self.assertEqual(budget.attempted_bytes, 4096)
        self.assertFalse(budget.active())
        self.assertIsNone(budget.read(process, BASE + 4096, 1))
        self.assertEqual(len(process.calls), 1)

    def test_scan_preserves_cross_chunk_camera_and_viewport(self):
        # Position pair at the end of the first chunk, viewport in the next.
        data = camera_blob(12288, camera=4096 - 96, view_delta=160)
        process = FakeProcess(data)
        with tempfile.TemporaryDirectory() as directory:
            result = scan_process(process, scan_args(), VIEW, Path(directory),
                                  ReadBudget(10, 20 * 1024))
        self.assertEqual(result["candidate_count"], 1)
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["candidates"][0]["base"], BASE + 4000)

    def test_no_fake_shape_join_across_failed_chunk(self):
        data = camera_blob(12288, camera=4096 - 96, view_delta=160)
        process = FakeProcess(data, fail_at=BASE)
        with tempfile.TemporaryDirectory() as directory:
            result = scan_process(process, scan_args(), VIEW, Path(directory),
                                  ReadBudget(10, 20 * 1024))
        self.assertEqual(result["candidate_count"], 0)

    def test_explicit_resume_skips_previously_scanned_bytes(self):
        data = camera_blob(12288, camera=8192 + 256)
        process = FakeProcess(data)
        with tempfile.TemporaryDirectory() as directory:
            result = scan_process(process, scan_args(start=BASE + 8192), VIEW,
                                  Path(directory), ReadBudget(10, 20 * 1024))
        self.assertEqual(process.calls[0][0], BASE + 8192)
        self.assertEqual(result["candidate_count"], 1)

    def test_snapshot_refuses_prior_process_creation_before_payload_reads(self):
        process = FakeProcess(camera_blob(8192))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "other.json"
            path.write_text(json.dumps({"identity": {"session_key": "42:old:image"}}))
            args = Namespace(from_report=str(path))
            with self.assertRaisesRegex(ValueError, "another process creation"):
                snapshot_process(process, args, VIEW, Path(directory), ReadBudget(10, 4096))
        self.assertEqual(process.calls, [])

    def test_import_and_help_do_not_construct_process_or_socket(self):
        # Other suites may already import the harmless wrapper definitions.
        # Reload the probe while both entry points are guarded against use.
        import tools.fh5_map_probe as module
        with patch("win_readonly.Process", side_effect=AssertionError("native access")) as native:
            with patch("socket.socket", side_effect=AssertionError("UDP access")) as udp:
                importlib.reload(module)
                native.assert_not_called()
                udp.assert_not_called()
        parsed = parser().parse_args(["scan", "--pid", "42", "--session-key", "current",
                                      "--width", "1920", "--height", "1080",
                                      "--phase", "open", "--output", "new.json"])
        self.assertEqual(parsed.max_mib, 512)


if __name__ == "__main__":
    unittest.main()
