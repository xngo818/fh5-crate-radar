"""Cold discovery regression in sparse fake memory; never opens a game.

The clock models bounded read latency, not wall-clock performance. In particular,
the speed limits below distinguish fair discovery from walking gigabytes before
visiting a small object allocation. They are not a real-game latency guarantee.
"""
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fh5_runtime as module

MIB = 1024 * 1024
GIB = 1024 * MIB
MARKERS = (b'pickup_position_ref_component', b'flare_group', b'pickup_model')


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0, seconds)


def region(base, size):
    return {'base': base, 'size': size, 'end': base + size,
            'allocation_base': base, 'allocation_protect': 4,
            'state': 0x1000, 'protect': 4, 'type': 0x20000,
            'readable': True, 'writable': True, 'executable': False}


class FakeMemory:
    """Sparse readable regions plus small concrete byte buffers."""
    def __init__(self, clock, layout):
        self.clock, self.layout = clock, layout
        self.identity = {'pid': 4321, 'session_key': '4321:new-creation',
                         'image_path': 'fake/ForzaHorizon5.exe'}
        self.buffers = {}
        self.reads = []
        self.large_cost, self.small_cost = .001, .0002
        self.on_read = None

    def regions(self, **kwargs):
        return [dict(item) for item in self.layout]

    def read(self, address, size):
        self.reads.append((address, size))
        self.clock.sleep(self.large_cost if size >= 65536 else self.small_cost)
        if self.on_read:
            self.on_read(address, size)
        accessible = any(r['base'] <= address and address + size <= r['end']
                         for r in self.layout)
        accessible |= any(low <= address and address + size <= low + len(data)
                          for low, data in self.buffers.items())
        if not accessible:
            return None
        result = bytearray(size)
        for low, data in self.buffers.items():
            begin, end = max(address, low), min(address + size, low + len(data))
            if begin < end:
                result[begin - address:end - address] = data[begin - low:end - low]
        return bytes(result)

    def install(self, first, row=1):
        heap = 0x700000000000 + first
        self.buffers[heap] = MARKERS[0]
        for index, marker in enumerate(MARKERS):
            descriptor = bytearray(32)
            if len(marker) > 15:
                struct.pack_into('<Q', descriptor, 0, heap)
                capacity = 31
            else:
                descriptor[:len(marker)] = marker
                capacity = 15
            struct.pack_into('<QQ', descriptor, 16, len(marker), capacity)
            self.buffers[first + index * 32] = bytes(descriptor)
        self.buffers[first - 120] = struct.pack('<Q', row)
        self.buffers[first - 8] = struct.pack('<II', row, 0)
        self.buffers[first + 152] = struct.pack('<QQQ', first + 4096, first + 8192, first)
        self.buffers[first + 4096 + 144] = struct.pack('<3f', 100 + row, 20, 300 + row)


class ColdStartTests(unittest.TestCase):
    def clock_context(self, clock):
        return patch.object(module, 'time', SimpleNamespace(
            monotonic=clock.monotonic, sleep=clock.sleep))

    def discover_until(self, planner, process, clock, target, seconds, *, stop=None):
        stop = stop or threading.Event()
        start, found = clock.now, []
        with self.clock_context(clock):
            for _ in range(20000):
                if clock.now - start >= seconds:
                    break
                found.extend(planner.step(process, stop, fresh_rows=False, now=clock.now))
                if any(item['first_string_address'] == target for item in found):
                    break
                clock.sleep(planner.poll_period(.5))
        return found, clock.now - start

    def test_new_session_without_scope_reaches_high_small_region_interior(self):
        clock = Clock()
        low, high = 0x100000, 0x1000000000
        process = FakeMemory(clock, [region(low, 16 * GIB), region(high, 8 * MIB)])
        first = high + 15 * MIB // 4 + 8192
        process.install(first)
        planner = module.DiscoveryPlanner([], clock.now)
        self.assertIsNone(planner.scope)
        found, elapsed = self.discover_until(planner, process, clock, first, 2)
        self.assertIn(first, [item['first_string_address'] for item in found],
                      'a small high allocation waited behind an unrelated 16 GiB heap')
        self.assertLess(elapsed, 2)

    def test_dense_non_descriptors_do_not_spend_remote_reads_on_every_stride(self):
        clock = Clock()
        base = 0x100000
        process = FakeMemory(clock, [region(base, MIB)])
        # Text hits deliberately lack a third MSVC string descriptor. All
        # evidence needed to reject them is in this already-read chunk.
        decoys = bytearray(4096 * 96)
        for offset in range(0, len(decoys), 96):
            decoys[offset:offset + len(MARKERS[2]) + 1] = MARKERS[2] + b'\0'
        process.buffers[base + 512] = bytes(decoys)
        first = base + 448 * 1024
        process.install(first)
        planner = module.DiscoveryPlanner([], clock.now)
        found, elapsed = self.discover_until(planner, process, clock, first, 1)
        self.assertIn(first, [item['first_string_address'] for item in found],
                      'isolated text hits monopolized discovery before the real descriptor')
        self.assertLess(elapsed, 1)

    def test_small_region_acceleration_does_not_starve_full_large_region_sweep(self):
        clock = Clock()
        base = 0x100000
        layout = [region(base, 64 * MIB)]
        layout.extend(region(0x100000000 + i * 16 * MIB, 8 * MIB) for i in range(96))
        process = FakeMemory(clock, layout)
        first = base + 50 * MIB + 8192
        process.install(first)
        planner = module.DiscoveryPlanner([], clock.now)
        found, elapsed = self.discover_until(planner, process, clock, first, 6)
        self.assertIn(first, [item['first_string_address'] for item in found],
                      'cold priority traffic starved the complete global lane')
        self.assertLess(elapsed, 6)
        self.assertFalse(planner.locator.progress()['complete_world_coverage'])

    def test_refreshed_metadata_removes_freed_tail_and_discovers_new_region(self):
        clock = Clock()
        low, small, added = 0x100000, 0x1000000000, 0x2000000000
        process = FakeMemory(clock, [region(low, 16 * GIB), region(small, 8 * MIB)])
        planner = module.DiscoveryPlanner([], clock.now, [(8 * MIB, 0x20000, 4)])
        with self.clock_context(clock):
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
        # The previous small allocation loses its upper half. A different
        # allocation appears; neither operation supplies any old pool hint.
        process.layout[1] = region(small, 4 * MIB)
        process.layout.append(region(added, 8 * MIB))
        first = added + 15 * MIB // 4 + 8192
        process.install(first)
        clock.sleep(6)
        mark = len(process.reads)
        found, _ = self.discover_until(planner, process, clock, first, 3)
        self.assertIn(first, [item['first_string_address'] for item in found])
        self.assertFalse(any(small + 4 * MIB <= address < small + 8 * MIB
                             for address, size in process.reads[mark:] if size >= 65536),
                         'a cold queue kept reading freed tail pages after fresh metadata')

    def test_pool_appearing_after_empty_preferred_pass_does_not_wait_for_general_queue(self):
        clock = Clock()
        generic, preferred = 0x1000000000, 0x2000000000
        shape = (5898240, 0x20000, 4)
        layout = [region(0x100000, 16 * GIB)]
        layout.extend(region(generic + i * 16 * MIB, 8 * MIB) for i in range(96))
        layout.append(region(preferred, shape[0]))
        process = FakeMemory(clock, layout)
        planner = module.DiscoveryPlanner([], clock.now, [shape])
        with self.clock_context(clock):
            for _ in range(2000):
                planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
                if planner.status(clock.now).get('preferred_passes', 0):
                    break
                clock.sleep(planner.poll_period(.5))
        self.assertGreater(planner.status(clock.now).get('preferred_passes', 0), 0)
        self.assertTrue(planner._deferred,
                        'the setup must leave the general queue unfinished')
        # Entry into a match creates objects in a previously scanned allocation.
        # Its metadata does not change, and no formerly fresh row signals loss.
        first = preferred + 15 * MIB // 4 + 8192
        process.install(first)
        found, elapsed = self.discover_until(planner, process, clock, first, 5)
        self.assertIn(first, [item['first_string_address'] for item in found],
                      'an empty preferred pass waited for the entire general queue')
        self.assertLess(elapsed, 5)
        self.assertTrue(planner._deferred,
                        'discovery should not depend on emptying the general queue')

    def test_recurring_preferred_passes_leave_payload_time_for_general_and_global_lanes(self):
        clock = Clock()
        low, generic, preferred = 0x100000, 0x1000000000, 0x2000000000
        shape = (5898240, 0x20000, 4)
        layout = [region(low, 64 * MIB)]
        layout.extend(region(generic + i * 16 * MIB, 8 * MIB) for i in range(96))
        layout.append(region(preferred, shape[0]))
        process = FakeMemory(clock, layout)
        global_first, general_first = low + 50 * MIB + 8192, generic + 8192
        process.install(global_first)
        process.install(general_first, 2)
        planner = module.DiscoveryPlanner([], clock.now, [shape])
        start, found = clock.now, []
        with self.clock_context(clock):
            for _ in range(2000):
                if clock.now - start >= 6:
                    break
                found.extend(planner.step(process, threading.Event(), fresh_rows=False,
                                          now=clock.now))
                clock.sleep(planner.poll_period(.5))
        addresses = {item['first_string_address'] for item in found}
        self.assertIn(global_first, addresses, 'preferred passes starved the global frontier')
        self.assertIn(general_first, addresses, 'preferred passes starved the general queue')
        self.assertGreaterEqual(planner.status(clock.now).get('preferred_passes', 0), 2,
                                'the fairness check must include recurring preferred traffic')

    def test_stop_set_during_payload_read_prevents_followup_reads(self):
        clock = Clock()
        process = FakeMemory(clock, [region(0x100000, 16 * GIB),
                                     region(0x1000000000, 8 * MIB)])
        stop = threading.Event()
        process.on_read = lambda *_: stop.set()
        planner = module.DiscoveryPlanner([], clock.now)
        with self.clock_context(clock):
            planner.step(process, stop, fresh_rows=False, now=clock.now)
            count = len(process.reads)
            planner.step(process, stop, fresh_rows=False, now=clock.now)
        self.assertEqual(count, 1)
        self.assertEqual(len(process.reads), count)

    def test_discovery_hit_is_revalidated_before_it_can_be_displayed(self):
        clock, base = Clock(), 0x100000
        process = FakeMemory(clock, [region(base, MIB)])
        first = base + 8192
        process.install(first)
        planner = module.DiscoveryPlanner([], clock.now)
        found, _ = self.discover_until(planner, process, clock, first, 1)
        self.assertTrue(found)
        # A signature remains intact after discovery but its owner is gone.
        # Accelerated discovery must not turn a signature snapshot into a row.
        process.buffers[first + 152] = struct.pack('<QQQ', first + 4096,
                                                  first + 8192, first + 8)
        pool = module.CandidatePool(process.identity['session_key'], module.SUPPORTED_VERSION)
        pool.register(found)
        with self.clock_context(clock):
            refreshed = pool.refresh(process, threading.Event(), None)
        self.assertEqual(refreshed['rows'], [])
        self.assertIn(first, refreshed['removed'])
        self.assertEqual(refreshed['rejections'], {'wrong_tag_backref': 1})

    def test_game_restart_cannot_use_old_absolute_scope_as_current_session_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            old = {'session_key': '4321:old-creation',
                   'package_version': module.SUPPORTED_VERSION,
                   'ranges': [[0x100000, 0x102000]]}
            (path / 'pool_scope.json').write_text(json.dumps(old), encoding='utf-8')
            runtime = module.Runtime(state_dir=path, seed_scope=path / 'absent.json')
            identity = {'pid': 4321, 'session_key': '4321:new-creation',
                        'image_path': 'fake/ForzaHorizon5.exe'}
            self.assertEqual(runtime._load_scope(identity, module.SUPPORTED_VERSION), [])


if __name__ == '__main__':
    unittest.main()
