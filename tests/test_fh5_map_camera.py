"""Pure map-camera lifecycle/math tests; fake memory only, no native access."""
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_map_camera import (CameraLocator, CameraTracker, ClockActivity,
                            ALLOCATION_ANCHOR_OFFSETS, MAX_HOT_REGIONS, MAX_STEP_BYTES, READ_BEFORE, READ_SIZE,
                            candidate_addresses, decode_camera)


SESSION = '42:created:current-image'
REGION = 0x10000
ADDRESS = REGION+4096


def camera_bytes(address=ADDRESS, clock=10, eye=2500, x=-7054.69, z=3685.54):
    blob = bytearray(READ_SIZE)
    index = READ_BEFORE
    struct.pack_into('<11f', blob, index, x, eye-10, z, 1,
                     x, eye-10000, z, 1, 2560, 1440, 2560/1440)
    a = 10000/9990
    matrix = [.5625, 0, 0, 0, 0, 0, -a, -1, 0, 1, 0, 0,
              -x*.5625, -z, eye*a-10*a, eye]
    for offset in (-672, -1712):
        struct.pack_into('<16f', blob, index+offset, *matrix)
    for offset in (0xB0, 0xC4, 0xD8):
        struct.pack_into('<f', blob, index+offset, .002)
    struct.pack_into('<f', blob, index+0x194, clock)
    return blob


class FakeProcess:
    min_address = REGION
    max_address = 0x7fffffffffff

    def __init__(self, size=32768, session=SESSION):
        self.identity = {'session_key': session}
        self.data = bytearray(size)
        self.reads = []
        self.region_queries = 0
        self.failures = set()

    def install(self, address=ADDRESS, clock=10, **changes):
        raw = camera_bytes(address=address, clock=clock, **changes)
        offset = address-READ_BEFORE-REGION
        self.data[offset:offset+len(raw)] = raw

    def regions(self, **kwargs):
        self.region_queries += 1
        return [dict(base=REGION, size=len(self.data), state=0x1000, type=0x20000, protect=4)]

    def read(self, address, size):
        self.reads.append((address, size))
        if address in self.failures:
            return None
        offset = address-REGION
        if offset < 0 or offset+size > len(self.data):
            return None
        return bytes(self.data[offset:offset+size])


class DecodeTests(unittest.TestCase):
    def decode(self, raw=None):
        return decode_camera(raw if raw is not None else camera_bytes(), ADDRESS-READ_BEFORE, ADDRESS, 100)

    def test_geometry_depth_and_height_flattening(self):
        frame = self.decode()
        self.assertIsNotNone(frame)
        self.assertAlmostEqual(frame['eye_y'], 2500)
        self.assertAlmostEqual(frame['near_distance'], 10, places=3)
        self.assertAlmostEqual(frame['far_distance'], 10000, delta=2)
        self.assertEqual(frame['flattened_matrix'][4:8], [0]*4)
        self.assertEqual(frame['raw_matrix'][7], -1)
        self.assertIs(frame['projection_verified'], False)
        self.assertIs(frame['independent_map_open_flag'], False)

    def test_reject_short_mismatched_or_nonfinite_matrix_and_bad_clock(self):
        bad_offsets = [(READ_BEFORE-672, '<f', float('nan')),
                       (READ_BEFORE-672+48, '<f', 0),
                       (READ_BEFORE+0x194, '<f', float('nan')),
                       (READ_BEFORE+0xB0, '<f', 0),
                       (READ_BEFORE+0xC4, '<f', .003),
                       (READ_BEFORE+32, '<f', 1920)]
        for offset, fmt, value in bad_offsets:
            raw = camera_bytes()
            struct.pack_into(fmt, raw, offset, value)
            self.assertIsNone(self.decode(raw), offset)
        self.assertIsNone(self.decode(camera_bytes()[:-1]))

    def test_projection_centre_error_and_near_far_conventions_are_strict(self):
        raw = camera_bytes()
        for offset in (-672, -1712):
            struct.pack_into('<f', raw, READ_BEFORE+offset+48, 0)
        self.assertIsNone(self.decode(raw))
        raw = camera_bytes()
        struct.pack_into('<f', raw, READ_BEFORE+4, 2500)  # Eye is not record near point.
        self.assertIsNone(self.decode(raw))

    def test_discovery_requires_fixed_view32_and_aligned_vec4_pattern(self):
        raw = camera_bytes()
        self.assertEqual(candidate_addresses(raw, ADDRESS-READ_BEFORE), [ADDRESS])
        self.assertEqual(candidate_addresses(b'', REGION), [])
        self.assertEqual(candidate_addresses(raw, ADDRESS-READ_BEFORE+1), [])

    def test_inactive_geometry_is_only_a_hint_never_a_default_camera_frame(self):
        raw = camera_bytes(eye=1000, x=0, z=0, clock=0)
        for offset in (0xB0, 0xC4, 0xD8):
            struct.pack_into('<f', raw, READ_BEFORE+offset, 0)
        self.assertIsNone(self.decode(raw))
        hint = decode_camera(raw, ADDRESS-READ_BEFORE, ADDRESS, 100, allow_inactive_hint=True)
        self.assertTrue(hint['search_hint_only'])
        self.assertFalse(hint['activity_fields_valid'])
        self.assertIsNone(hint['activity_clock'])
        self.assertFalse(hint['projection_verified'])


class ClockTests(unittest.TestCase):
    def test_two_distinct_samples_freeze_and_restart(self):
        activity = ClockActivity()
        self.assertFalse(activity.observe(10, 100))
        self.assertFalse(activity.observe(10, 100.05))
        self.assertTrue(activity.observe(10.1, 100.1))
        self.assertTrue(activity.observe(10.1, 100.3))
        self.assertFalse(activity.observe(10.1, 100.36))
        self.assertEqual(activity.reason, 'activity_frozen')
        self.assertFalse(activity.observe(10.1, 100.4))
        self.assertTrue(activity.observe(10.2, 100.45))

    def test_clock_decrease_and_sample_gap_require_new_pair(self):
        activity = ClockActivity()
        self.assertFalse(activity.observe(10, 100))
        self.assertTrue(activity.observe(11, 100.05))
        self.assertFalse(activity.observe(12, 101))
        self.assertTrue(activity.observe(13, 101.05))
        self.assertFalse(activity.observe(1, 101.1))
        self.assertTrue(activity.observe(2, 101.15))


class TrackerTests(unittest.TestCase):
    def test_first_frame_pending_second_clock_active_then_exact_read_failure_clears(self):
        process, tracker = FakeProcess(), CameraTracker(SESSION, hot_regions=[REGION])
        process.install(clock=10)
        self.assertIsNone(tracker.step(process, 100))
        process.install(clock=10.1)
        frame = tracker.step(process, 100.05)
        self.assertEqual(frame['camera_address'], ADDRESS)
        process.failures.add(ADDRESS-READ_BEFORE)
        self.assertIsNone(tracker.step(process, 100.1))
        self.assertIsNone(tracker.camera_address)

    def test_scan_complete_without_camera_retries_after_backoff(self):
        process, tracker = FakeProcess(), CameraTracker(SESSION)
        self.assertIsNone(tracker.step(process, 100))
        self.assertTrue(tracker.locator.complete)
        tracker.step(process, 100.05)  # Sets bounded no-camera rescan backoff.
        process.install(clock=10)
        self.assertIsNone(tracker.step(process, 100.3))
        self.assertEqual(process.region_queries, 1)
        self.assertIsNone(tracker.step(process, 100.6))
        self.assertGreaterEqual(process.region_queries, 2)
        process.install(clock=10.1)
        self.assertIsNotNone(tracker.step(process, 100.65))

    def test_frozen_stale_camera_cannot_block_new_allocation(self):
        process, tracker = FakeProcess(), CameraTracker(SESSION, hot_regions=[REGION])
        process.install(clock=10)
        tracker.step(process, 100)
        process.install(clock=10.1)
        self.assertIsNotNone(tracker.step(process, 100.05))
        self.assertIsNotNone(tracker.step(process, 100.1))
        self.assertIsNotNone(tracker.step(process, 100.2))
        self.assertIsNotNone(tracker.step(process, 100.29))
        self.assertIsNone(tracker.step(process, 100.36))
        self.assertIsNone(tracker.camera_address)
        new_address = ADDRESS+12000
        process.install(address=new_address, clock=50, x=1234)
        self.assertIsNone(tracker.step(process, 100.4))
        process.install(address=new_address, clock=50.1, x=1234)
        frame = tracker.step(process, 100.45)
        self.assertEqual(frame['camera_address'], new_address)

    def test_sample_gap_clears_selected_camera_until_new_activity_pair(self):
        process, tracker = FakeProcess(), CameraTracker(SESSION)
        process.install(clock=10)
        tracker.step(process, 100)
        process.install(clock=10.1)
        self.assertIsNotNone(tracker.step(process, 100.05))
        process.install(clock=20)
        self.assertIsNone(tracker.step(process, 101))
        self.assertIsNone(tracker.camera_address)
        self.assertIsNone(tracker.step(process, 101.05))
        process.install(clock=20.1)
        self.assertIsNotNone(tracker.step(process, 101.1))

    def test_close_and_reopen_changes_address_and_reproves_activity(self):
        process, tracker = FakeProcess(), CameraTracker(SESSION, hot_regions=[REGION])
        process.install(clock=10)
        tracker.step(process, 100)
        process.install(clock=10.1)
        self.assertIsNotNone(tracker.step(process, 100.05))
        process.data[:] = bytes(len(process.data))
        self.assertIsNone(tracker.step(process, 100.1))
        process.install(address=ADDRESS+12000, clock=20)
        self.assertIsNone(tracker.step(process, 100.15))
        process.install(address=ADDRESS+12000, clock=20.1)
        self.assertEqual(tracker.step(process, 100.2)['camera_address'], ADDRESS+12000)

    def test_multiple_active_cameras_are_ambiguous(self):
        process, tracker = FakeProcess(), CameraTracker(SESSION)
        process.install(clock=10)
        process.install(address=ADDRESS+12000, clock=20, x=1234)
        self.assertIsNone(tracker.step(process, 100))
        process.install(clock=10.1)
        process.install(address=ADDRESS+12000, clock=20.1, x=1234)
        self.assertIsNone(tracker.step(process, 100.05))
        self.assertEqual(tracker.reason, 'ambiguous_active_cameras')

    def test_session_and_build_mismatch_do_not_read_payload(self):
        process = FakeProcess(session='42:new-creation:current-image')
        tracker = CameraTracker(SESSION, hot_regions=[REGION])
        self.assertIsNone(tracker.step(process, 100))
        self.assertEqual(process.reads, [])
        self.assertEqual(tracker.locator.hot_regions, set())
        process = FakeProcess()
        tracker = CameraTracker(SESSION, package_version='unknown')
        self.assertIsNone(tracker.step(process, 100))
        self.assertEqual(process.reads, [])

    def test_payload_budget_includes_discovery_and_candidate_validation(self):
        process = FakeProcess(size=4*1024*1024)
        process.install(clock=10)
        tracker = CameraTracker(SESSION)
        tracker.step(process, 100)
        self.assertLessEqual(sum(size for _, size in process.reads), MAX_STEP_BYTES)
        self.assertTrue(all(size <= 512*1024 for _, size in process.reads))

    def test_hot_revisit_finds_reopened_camera_behind_unfinished_full_scan(self):
        process = FakeProcess(size=32*1024*1024)
        tracker = CameraTracker(SESSION, hot_regions=[REGION])
        process.install(clock=10)
        tracker.step(process, 100)
        process.install(clock=10.1)
        self.assertIsNotNone(tracker.step(process, 100.05))
        process.data[:] = bytes(len(process.data))
        self.assertIsNone(tracker.step(process, 100.1))
        # Let discovery pass the old hot allocation while the map is closed.
        for i in range(2, 8):
            before = len(process.reads)
            self.assertIsNone(tracker.step(process, 100+i*.1))
            self.assertLessEqual(sum(n for _, n in process.reads[before:]), MAX_STEP_BYTES)
        self.assertFalse(tracker.locator.complete)
        new_address = ADDRESS+16384  # Behind the ongoing full-scan cursor.
        process.install(address=new_address, clock=20, x=1234)
        found = None
        for i in range(8, 20):
            before = len(process.reads)
            found = tracker.step(process, 100+i*.1)
            self.assertLessEqual(sum(n for _, n in process.reads[before:]), MAX_STEP_BYTES)
            if new_address in tracker.pending:
                process.install(address=new_address, clock=20.1, x=1234)
            if found is not None:
                break
        self.assertIsNotNone(found)
        self.assertEqual(found['camera_address'], new_address)
        self.assertFalse(tracker.locator.complete)
        # The union now also covers saved allocation windows, so this bounded
        # recovery may finish during its longer first hot pass plus forward
        # scan progress rather than requiring a second pass counter.
        self.assertGreaterEqual(tracker.locator.hot_passes, 1)

    def test_first_map_open_uses_inactive_geometry_hint_without_prior_active_frame(self):
        process = FakeProcess(size=32*1024*1024)
        tracker = CameraTracker(SESSION)
        inactive = camera_bytes(eye=1000, x=0, z=0, clock=0)
        for offset in (0xB0, 0xC4, 0xD8):
            struct.pack_into('<f', inactive, READ_BEFORE+offset, 0)
        process.data[ADDRESS-READ_BEFORE-REGION:ADDRESS-REGION+len(inactive)-READ_BEFORE] = inactive
        for i in range(7):
            self.assertIsNone(tracker.step(process, 100+i*.1))
        self.assertIsNone(tracker.camera_address)
        self.assertIn(ADDRESS, tracker.locator.hot_anchors)
        self.assertEqual(len(tracker.locator.inactive_hint_regions), 1)
        self.assertFalse(tracker.locator.complete)
        new_address = ADDRESS+3*1024*1024
        process.install(address=new_address, clock=50, x=1234)
        result = None
        for i in range(7, 24):
            before = len(process.reads)
            result = tracker.step(process, 100+i*.1)
            self.assertLessEqual(sum(n for _, n in process.reads[before:]), MAX_STEP_BYTES)
            if new_address in tracker.pending:
                process.install(address=new_address, clock=50.1, x=1234)
            if result:
                break
        self.assertIsNotNone(result)
        self.assertEqual(result['camera_address'], new_address)
        self.assertFalse(result['search_hint_only'])
        self.assertFalse(tracker.locator.complete)

    def test_float_head_without_matching_geometry_does_not_create_hot_hint(self):
        process = FakeProcess()
        head = camera_bytes()[READ_BEFORE:READ_BEFORE+44]
        process.data[ADDRESS-REGION:ADDRESS-REGION+44] = head
        tracker = CameraTracker(SESSION)
        self.assertIsNone(tracker.step(process, 100))
        self.assertEqual(tracker.locator.hot_anchors, [])
        self.assertEqual(tracker.locator.inactive_hint_regions, {})


class HotRangeTests(unittest.TestCase):
    def test_many_same_session_migrations_retain_only_recent_active_region_hints(self):
        regions = [dict(base=REGION+i*64*1024**2, size=8*1024**2) for i in range(100)]
        locator = CameraLocator(SESSION)
        for index, region in enumerate(regions):
            locator.regions = regions
            self.assertTrue(locator.remember(region['base']+4096))
            expected = {r['base'] for r in regions[max(0, index+1-MAX_HOT_REGIONS):index+1]}
            self.assertEqual(locator.hot_regions, expected)
            self.assertLessEqual(len(locator.hot_anchors), 4)
            self.assertLessEqual(len(locator.hot_ranges), 8)
            locator.reset()
            self.assertEqual(locator.hot_regions, expected)
        self.assertEqual(locator.step(FakeProcess(session='other-session')), [])
        self.assertEqual(locator.hot_regions, set())
        self.assertEqual(locator._hot_region_order, [])

    def test_cached_hint_priority_is_bounded_and_inactive_hint_cannot_evict_it(self):
        bases = [REGION+i*64*1024**2 for i in range(MAX_HOT_REGIONS+2)]
        locator = CameraLocator(SESSION, hot_regions=bases)
        self.assertEqual(locator.hot_regions, set(bases[:MAX_HOT_REGIONS]))
        locator.regions = [dict(base=base, size=8*1024**2) for base in bases]
        inactive = bases[-1]+4096
        self.assertTrue(locator.remember(inactive, active=False))
        self.assertEqual(locator.hot_regions, set(bases[:MAX_HOT_REGIONS]))
        self.assertIn(inactive, locator.hot_anchors)
        # A newly confirmed active allocation wins a slot over the oldest
        # cached hint, while the inactive anchor keeps its separate window.
        active = bases[-2]+4096
        self.assertTrue(locator.remember(active))
        self.assertIn(bases[-2], locator.hot_regions)
        self.assertNotIn(bases[MAX_HOT_REGIONS-1], locator.hot_regions)
        self.assertEqual(len(locator.hot_regions), MAX_HOT_REGIONS)
        locator._begin_hot_pass(FakeProcess(), 100, metadata_is_fresh=True)
        self.assertTrue(any(r['base'] <= inactive < r['base']+r['size']
                            for r in locator._hot_regions))
        self.assertTrue(any(r['base'] <= bases[0] < r['base']+r['size']
                            for r in locator._hot_regions))

    def test_hot_pass_evaluates_metadata_priority_once_for_all_hint_windows(self):
        bases = [REGION+i*64*1024**2 for i in range(64)]
        locator = CameraLocator(SESSION, hot_regions=bases[:MAX_HOT_REGIONS])
        locator.regions = [dict(base=base, size=8*1024**2) for base in bases]
        locator.remember(bases[0]+4096)
        real_priority = locator._priority
        calls = []

        def priority(region):
            calls.append(region['base'])
            return real_priority(region)

        locator._priority = priority
        locator._begin_hot_pass(FakeProcess(), 100, metadata_is_fresh=True)
        self.assertEqual(calls, bases)
        # The union still covers each independently cached allocation.
        for base in bases[:MAX_HOT_REGIONS]:
            self.assertTrue(any(r['base'] <= base < r['base']+r['size']
                                for r in locator._hot_regions))

    def test_inactive_anchor_cannot_exclude_later_camera_in_distant_cached_region(self):
        # Reproduce the field failure: app starts in driving view; the only
        # structural camera is inactive in pool A. Pool B, remembered from an
        # earlier map, is recommitted only when the user subsequently opens it.
        # The resumable full sweep is already far away in a 13 GiB noise range.
        main, remote, noise = REGION, REGION+60*1024**2, 0x100000000
        anchor = main+ALLOCATION_ANCHOR_OFFSETS[0]
        live_address = remote+0x175e0

        class Allocations:
            min_address, max_address = REGION, 0x7fffffffffff

            def __init__(self):
                self.identity = {'session_key': SESSION}
                self.live, self.clock, self.reads = False, 10.0, []

            def regions(self, **kwargs):
                ranges = [(main, 32*1024**2), (noise, 13*1024**3)]
                if self.live:
                    ranges.append((remote, 256*1024))
                return [dict(base=base, size=size, state=0x1000, type=0x20000, protect=4)
                        for base, size in ranges]

            def read(self, address, size):
                self.reads.append((address, size))
                blob = bytearray(size)
                inactive = camera_bytes(address=anchor, eye=1000, x=0, z=0, clock=0)
                for offset in (0xB0, 0xC4, 0xD8):
                    struct.pack_into('<f', inactive, READ_BEFORE+offset, 0)
                patches = [(anchor, inactive)]
                if self.live:
                    patches.append((live_address, camera_bytes(address=live_address,
                                                              clock=self.clock, x=1234)))
                for point, raw in patches:
                    start = point-READ_BEFORE
                    low, high = max(address, start), min(address+size, start+len(raw))
                    if low < high:
                        blob[low-address:high-address] = raw[low-start:high-start]
                return bytes(blob)

        process = Allocations()
        tracker = CameraTracker(SESSION, hot_regions=(main, remote))
        for i in range(45):
            self.assertIsNone(tracker.step(process, 100+i*.05))
        self.assertIn(anchor, tracker.locator.hot_anchors)
        self.assertNotIn(remote, [r['base'] for r in tracker.locator._scan_regions])
        prior_full_index, prior_full_address = tracker.locator.index, tracker.locator.address
        process.live = True
        found = None
        for i in range(45, 145):
            process.clock += .05
            before = len(process.reads)
            found = tracker.step(process, 100+i*.05)
            self.assertLessEqual(sum(n for _, n in process.reads[before:]), MAX_STEP_BYTES)
            if found is not None:
                break
        self.assertIsNotNone(found)
        self.assertEqual(found['camera_address'], live_address)
        self.assertLess((i-45)*.05, 5)
        self.assertFalse(tracker.locator.complete)
        self.assertIn(remote, tracker.locator.hot_regions)
        self.assertGreaterEqual(tracker.locator.hot_passes, 2)
        self.assertGreaterEqual(tracker.locator.index, prior_full_index)
        # Hot revisits leave a progress interval for the background sweep.
        self.assertTrue(tracker.locator.index > prior_full_index
                        or tracker.locator.address != prior_full_address)

    def test_cold_allocation_relative_probe_narrows_search_without_absolute_address(self):
        process = FakeProcess(size=16*1024*1024)
        anchor = REGION+ALLOCATION_ANCHOR_OFFSETS[0]
        inactive = camera_bytes(address=anchor, eye=1000, x=0, z=0, clock=0)
        for offset in (0xB0, 0xC4, 0xD8):
            struct.pack_into('<f', inactive, READ_BEFORE+offset, 0)
        process.data[anchor-READ_BEFORE-REGION:anchor-READ_BEFORE-REGION+READ_SIZE] = inactive
        live = anchor+3*1024*1024
        process.install(address=live, clock=10, x=1234)
        tracker = CameraTracker(SESSION)
        result = None
        for i in range(20):
            before = len(process.reads)
            result = tracker.step(process, 100+i*.005)
            self.assertLessEqual(sum(n for _, n in process.reads[before:]), MAX_STEP_BYTES)
            if live in tracker.pending:
                process.install(address=live, clock=10.1+i*.005, x=1234)
            if result:
                break
        self.assertIsNotNone(result)
        self.assertEqual(result['camera_address'], live)
        self.assertLess(i*.005, .1)
        self.assertIn(anchor, tracker.locator.hot_anchors)
        self.assertGreater(tracker.locator.anchor_probe_hits, 0)
        self.assertFalse(result['projection_verified'])
        self.assertFalse(result['independent_map_open_flag'])

    def test_allocation_probe_head_lookalike_cannot_create_hint(self):
        process = FakeProcess(size=4*1024*1024)
        address = REGION+ALLOCATION_ANCHOR_OFFSETS[0]
        raw = camera_bytes(address=address)
        process.data[address-REGION:address-REGION+44] = raw[READ_BEFORE:READ_BEFORE+44]
        locator = CameraLocator(SESSION)
        locator.step(process, now=100, byte_limit=512*1024)
        self.assertEqual(locator.anchor_probe_hits, 0)
        self.assertEqual(locator.hot_anchors, [])

    def test_split_region_base_no_longer_equal_hint_is_prioritized(self):
        process = FakeProcess(size=32*1024*1024)
        old_base = REGION+8*1024*1024
        address = old_base+16384
        process.install(address=address, clock=10)
        locator = CameraLocator(SESSION, hot_regions=[old_base])
        locator.regions = [dict(base=old_base, size=8*1024*1024)]
        locator.remember(address)
        locator.reset()
        new_base = old_base+4096
        split = [dict(base=REGION, size=1024*1024, state=0x1000, type=0x20000, protect=4),
                 dict(base=new_base, size=4*1024*1024-4096, state=0x1000, type=0x20000, protect=4),
                 dict(base=old_base+4*1024*1024, size=4*1024*1024, state=0x1000, type=0x20000, protect=4)]
        process.regions = lambda **kwargs: split
        found = locator.step(process, now=100, byte_limit=512*1024)
        self.assertIn(address, found)
        self.assertEqual(process.reads[0][0], new_base)
        self.assertNotEqual(new_base, old_base)

    def test_adjacent_eligible_metadata_splits_cover_one_coherent_read(self):
        locator = CameraLocator(SESSION)
        start = ADDRESS-READ_BEFORE
        locator.regions = [dict(base=start, size=1000),
                           dict(base=start+1000, size=READ_SIZE-1000)]
        self.assertTrue(locator.contains(start, READ_SIZE))
        locator.regions[1]['base'] += 1
        self.assertFalse(locator.contains(start, READ_SIZE))

    def test_session_change_drops_ranges_and_search_anchors(self):
        locator = CameraLocator(SESSION, hot_regions=[REGION])
        locator.regions = [dict(base=REGION, size=32768)]
        locator.remember(ADDRESS)
        process = FakeProcess(session='new-session')
        self.assertEqual(locator.step(process), [])
        self.assertEqual(locator.hot_regions, set())
        self.assertEqual(locator.hot_ranges, [])
        self.assertEqual(locator.hot_anchors, [])
        self.assertEqual(process.reads, [])

    def test_inactive_geometry_hints_are_capped_per_region_and_globally(self):
        locator = CameraLocator(SESSION)
        locator.regions = [dict(base=REGION+i*65536, size=32768) for i in range(3)]
        self.assertTrue(locator.remember(ADDRESS, active=False))
        self.assertFalse(locator.remember(ADDRESS+4096, active=False))
        self.assertTrue(locator.remember(ADDRESS+65536, active=False))
        self.assertFalse(locator.remember(ADDRESS+2*65536, active=False))
        self.assertEqual(len(locator.inactive_hint_regions), 2)


if __name__ == '__main__':
    unittest.main()
