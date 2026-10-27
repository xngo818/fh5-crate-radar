"""Cold first-map discovery with sparse fake memory; no game/native access."""
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_map_camera import CameraTracker, MAX_STEP_BYTES, READ_BEFORE, READ_SIZE


SESSION = 'cold-map:42:current-process-creation'
POOL = 0x20000000
POOL_SIZE = 256 * 1024
CAMERA = POOL + 0x175e0  # Deliberately not an allocation-relative probe offset.
NOISE = 0x100000000
NOISE_SIZE = 13 * 1024 ** 3


def camera_fixture(clock):
    """Measured layout family, with a valid pair of independent clock samples."""
    raw = bytearray(READ_SIZE)
    index, eye, x, z = READ_BEFORE, 2500.0, 1234.0, 2300.0
    struct.pack_into('<11f', raw, index, x, eye - 10, z, 1,
                     x, eye - 10000, z, 1, 2560, 1440, 2560 / 1440)
    depth = 10000 / 9990
    matrix = [.5625, 0, 0, 0, 0, 0, -depth, -1, 0, 1, 0, 0,
              -x * .5625, -z, eye * depth - 10 * depth, eye]
    for offset in (-672, -1712):
        struct.pack_into('<16f', raw, index + offset, *matrix)
    for offset in (0xB0, 0xC4, 0xD8):
        struct.pack_into('<f', raw, index + offset, .002)
    struct.pack_into('<f', raw, index + 0x194, clock)
    return raw


class SparseFirstMapProcess:
    min_address, max_address = 0x10000, 0x7fffffffffff

    def __init__(self, pool_already_committed=False):
        self.identity = {'session_key': SESSION}
        self.pool_committed = pool_already_committed
        self.camera_created = False
        self.clock = 20.0
        self.region_queries = 0
        self.reads = []

    def regions(self, **kwargs):
        self.region_queries += 1
        ranges = [(NOISE, NOISE_SIZE)]
        if self.pool_committed:
            ranges.insert(0, (POOL, POOL_SIZE))
        return [dict(base=base, size=size, state=0x1000, type=0x20000, protect=4)
                for base, size in ranges]

    def read(self, address, size):
        self.reads.append((address, size))
        ranges = [(NOISE, NOISE_SIZE)]
        if self.pool_committed:
            ranges.append((POOL, POOL_SIZE))
        if not any(base <= address and address + size <= base + length
                   for base, length in ranges):
            return None
        blob = bytearray(size)
        if self.camera_created:
            raw, start = camera_fixture(self.clock), CAMERA - READ_BEFORE
            low, high = max(address, start), min(address + size, start + len(raw))
            if low < high:
                blob[low - address:high - address] = raw[low - start:high - start]
        return bytes(blob)


class FirstMapColdStartTests(unittest.TestCase):
    def run_first_map(self, pool_already_committed):
        process = SparseFirstMapProcess(pool_already_committed)
        tracker = CameraTracker(SESSION)  # No earlier-map or same-session hints.
        for step in range(4):
            before = len(process.reads)
            self.assertIsNone(tracker.step(process, 100 + step * .05))
            self.assertLessEqual(sum(size for _, size in process.reads[before:]),
                                 MAX_STEP_BYTES)
        self.assertFalse(tracker.locator.complete)
        self.assertEqual(tracker.locator.hot_regions, set())
        # The first sweep has passed the small pool (if it existed), and is
        # still inside the unrelated 13 GiB range when the first map opens.
        self.assertTrue(any(NOISE <= address < NOISE + NOISE_SIZE
                            for address, _ in process.reads))
        prior_noise_bytes = sum(size for address, size in process.reads
                                if NOISE <= address < NOISE + NOISE_SIZE)
        process.pool_committed = process.camera_created = True
        found, first_pending_step = None, None
        for step in range(400):
            process.clock += .05
            before = len(process.reads)
            found = tracker.step(process, 100.2 + step * .05)
            self.assertLessEqual(sum(size for _, size in process.reads[before:]),
                                 MAX_STEP_BYTES)
            if CAMERA in tracker.pending and first_pending_step is None:
                first_pending_step = step
                self.assertIsNone(found, 'One clock sample cannot authorize a camera')
            if found is not None:
                break
        self.assertIsNotNone(found, 'First map must not wait for a 13 GiB sweep')
        self.assertLess(step * .05, 20)
        self.assertEqual(found['camera_address'], CAMERA)
        self.assertFalse(found['projection_verified'])
        self.assertGreater(step, first_pending_step)
        self.assertFalse(tracker.locator.complete)
        self.assertGreaterEqual(sum(size for address, size in process.reads
                                    if NOISE <= address < NOISE + NOISE_SIZE),
                                prior_noise_bytes)
        # Metadata refresh/revisits must not restart the unrelated full sweep.
        self.assertEqual(sum(address == NOISE for address, _ in process.reads), 1)
        return process, tracker

    def test_first_map_in_new_small_allocation_is_found_without_warm_hints(self):
        process, tracker = self.run_first_map(pool_already_committed=False)
        self.assertGreaterEqual(process.region_queries, 2)
        self.assertIn(POOL, tracker.locator.hot_regions)

    def test_first_map_created_behind_cursor_in_existing_small_pool_is_found(self):
        self.run_first_map(pool_already_committed=True)

    def test_pending_activity_pair_is_not_blocked_by_metadata_enumeration(self):
        process = SparseFirstMapProcess(pool_already_committed=True)
        process.camera_created = True
        tracker = CameraTracker(SESSION)
        self.assertIsNone(tracker.step(process, 100))
        self.assertIn(CAMERA, tracker.pending)
        # A metadata stall would consume the 250 ms activity window. A known
        # coherent candidate must get its second exact sample first.
        process.regions = Mock(side_effect=TimeoutError('synthetic slow metadata'))
        process.clock += .05
        before = len(process.reads)
        frame = tracker.step(process, 100.05)
        self.assertIsNotNone(frame)
        self.assertEqual(frame['camera_address'], CAMERA)
        process.regions.assert_not_called()
        self.assertLessEqual(sum(size for _, size in process.reads[before:]),
                             MAX_STEP_BYTES)


if __name__ == '__main__':
    unittest.main()
