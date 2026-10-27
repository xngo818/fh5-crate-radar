"""Complete-enumeration retries retain the camera locator's forward progress."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_map_camera import CameraLocator, MAX_STEP_BYTES


SESSION = 'metadata-recovery-session'
BASE = 0x10000000


class MetadataProcess:
    identity = {'session_key': SESSION}
    min_address = 0x10000
    max_address = 0x7fffffffffff

    def __init__(self, size=512*1024*1024):
        self.metadata = [dict(base=BASE, size=size, state=0x1000, type=0x20000, protect=4)]
        self.fail_queries = 0
        self.deadlines = []
        self.reads = []

    def regions(self, **kwargs):
        self.deadlines.append(kwargs['deadline'])
        if self.fail_queries:
            self.fail_queries -= 1
            raise TimeoutError('complete enumeration deadline')
        return [dict(region) for region in self.metadata]

    def read(self, address, size):
        self.reads.append((address, size))
        return bytes(size)


class MetadataRecoveryTests(unittest.TestCase):
    def test_refresh_timeout_retains_frontier_and_does_not_retry_each_step(self):
        process, locator = MetadataProcess(), CameraLocator(SESSION)
        locator.step(process, 100)
        before_address, before_bytes = locator.address, locator.read_bytes
        process.fail_queries = 1
        before_reads = len(process.reads)
        locator.step(process, 103)
        self.assertIsNotNone(locator.regions)
        self.assertGreaterEqual(locator.address, before_address)
        self.assertGreater(locator.read_bytes, before_bytes)
        self.assertEqual(locator.metadata_refresh_failures, 1)
        self.assertIn('deadline', locator.metadata_refresh_error)
        self.assertFalse(locator.complete)
        self.assertLessEqual(sum(size for _, size in process.reads[before_reads:]), MAX_STEP_BYTES)
        queries = len(process.deadlines)
        locator.step(process, 103.1)
        self.assertEqual(len(process.deadlines), queries)
        locator.step(process, 106)
        self.assertIsNone(locator.metadata_refresh_error)
        self.assertEqual(locator.diagnostics()['metadata_failure_streak'], 0)

    def test_initial_enumeration_failure_retries_with_capped_budget_then_resets(self):
        process, locator = MetadataProcess(), CameraLocator(SESSION)
        process.fail_queries = 5
        with patch('fh5_map_camera.time.monotonic', return_value=100.0):
            for instant in (100, 102, 104, 106, 108):
                self.assertEqual(locator.step(process, instant), [])
                self.assertIsNone(locator.regions)
                self.assertFalse(locator.complete)
            self.assertEqual(process.deadlines, [101, 102, 103, 104, 104])
            self.assertEqual(process.reads, [])
            locator.step(process, 110)
            self.assertIsNotNone(locator.regions)
            self.assertEqual(process.deadlines[-1], 104)
            locator.step(process, 112)
            self.assertEqual(process.deadlines[-1], 101)
            self.assertEqual(locator.diagnostics()['metadata_query_budget_seconds'], 1.0)

    def test_failed_refresh_cannot_claim_completed_coverage(self):
        process, locator = MetadataProcess(size=4096), CameraLocator(SESSION)
        locator.step(process, 100)
        self.assertTrue(locator.complete)
        process.fail_queries = 1
        locator.step(process, 103)
        self.assertFalse(locator.complete)
        self.assertEqual(locator.metadata_refresh_failures, 1)

    def test_metadata_split_keeps_full_scan_frontier_without_false_new_coverage(self):
        process, locator = MetadataProcess(), CameraLocator(SESSION)
        locator.step(process, 100)
        count = len(locator._urgent_regions)
        frontier = locator.address
        old = process.metadata[0]
        process.metadata = [dict(old, size=old['size']//2),
                            dict(old, base=BASE+old['size']//2, size=old['size']//2)]
        locator.step(process, 103)
        self.assertEqual(len(process.deadlines), 2)
        self.assertEqual(len(locator._urgent_regions), count)
        self.assertGreaterEqual(locator.address, frontier)

    def test_retry_backoff_starts_after_slow_enumeration_completes(self):
        process, locator = MetadataProcess(), CameraLocator(SESSION)
        process.fail_queries = 1
        with patch('fh5_map_camera.time.monotonic', side_effect=[100, 100, 104]):
            self.assertFalse(locator._refresh_metadata(process, 200))
        self.assertEqual(locator._next_metadata_refresh, 206)
        self.assertEqual(locator.step(process, 205), [])
        self.assertEqual(len(process.deadlines), 1)

    def test_small_new_camera_pool_precedes_new_resource_heap(self):
        process, locator = MetadataProcess(), CameraLocator(SESSION)
        locator.step(process, 100)
        large = dict(process.metadata[0], base=0x40000000, size=1024**3)
        small = dict(process.metadata[0], base=0x90000000, size=256*1024)
        process.metadata.extend([large, small])
        before = len(process.reads)
        locator.step(process, 103)
        addresses = [address for address, _ in process.reads[before:]]
        self.assertIn(small['base'], addresses)
        self.assertLess(addresses.index(small['base']), addresses.index(large['base']))


if __name__ == '__main__':
    unittest.main()
