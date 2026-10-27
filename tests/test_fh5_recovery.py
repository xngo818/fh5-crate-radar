"""Deterministic fake-memory long-session recovery; no native APIs or UDP."""
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
from fh5_models import StateStore

MIB = 1024 * 1024
VERSION = module.SUPPORTED_VERSION
IDENTITY = {'pid': 123, 'session_key': 'fake:one',
            'image_path': 'fake/ForzaHorizon5.exe'}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0, seconds)


class SparseMemory:
    """Only in-Python bytes. Read cost is simulated, not a benchmark claim."""
    def __init__(self, clock):
        self.clock = clock
        self.identity = dict(IDENTITY)
        self.regions_calls = 0
        self.layout = [{'base': 0x100000, 'size': 16 * MIB,
                        'allocation_base': 0x100000}]
        self.parts = {}
        self.big_reads = []
        self.closed = False
        self.on_read = None
        self.large_read_seconds = .01

    def read(self, address, size):
        if self.on_read:
            self.on_read()
        if size >= 65536:
            self.clock.sleep(self.large_read_seconds)
            self.big_reads.append((self.clock.now, address, size))
        if not (any(r['base'] <= address and address + size <= r['base'] + r['size']
                    for r in self.layout)
                or any(low <= address and address + size <= low + len(data)
                       for low, data in self.parts.items())):
            return None
        result = bytearray(size)
        for low, data in self.parts.items():
            begin, end = max(address, low), min(address + size, low + len(data))
            if end > begin:
                result[begin - address:end - address] = data[begin - low:end - low]
        return bytes(result)

    def regions(self, **kwargs):
        self.regions_calls += 1
        return [dict(r) for r in self.layout]

    def is_alive(self):
        return not self.closed

    def close(self):
        self.closed = True

    def install(self, first, row=1):
        heap = 0x700000000000 + first
        markers = (b'pickup_position_ref_component', b'flare_group', b'pickup_model')
        self.parts[heap] = markers[0]
        for index, marker in enumerate(markers):
            descriptor = bytearray(32)
            if len(marker) > 15:
                struct.pack_into('<Q', descriptor, 0, heap)
                capacity = 31
            else:
                descriptor[:len(marker)] = marker
                capacity = 15
            struct.pack_into('<QQ', descriptor, 16, len(marker), capacity)
            self.parts[first + index * 32] = bytes(descriptor)
        self.parts[first - 120] = struct.pack('<Q', row)
        self.parts[first - 8] = struct.pack('<II', row, 0)
        self.parts[first + 152] = struct.pack('<QQQ', first + 4096, first + 8192, first)
        self.parts[first + 4096 + 144] = struct.pack('<3f', 100 + row, 20, 300 + row)

    def remove(self, first):
        self.parts.pop(first + 152, None)


class FakeEvent:
    def __init__(self, clock, end=None):
        self.clock, self.end, self.marked = clock, end, False

    def is_set(self):
        return self.marked or (self.end is not None and self.clock.now >= self.end)

    def set(self):
        self.marked = True

    def clear(self):
        self.marked = False

    def wait(self, seconds):
        self.clock.sleep(seconds)
        return self.is_set()


class RecordingStore(StateStore):
    def __init__(self, clock):
        super().__init__(clock=clock.monotonic)
        self.clock, self.samples = clock, []

    def set_status(self, **fields):
        super().set_status(**fields)
        if 'memory_timing' in fields:
            self.samples.append((self.clock.now, self.snapshot()))


class RecoveryRegressionTests(unittest.TestCase):
    def run_owner_shape_change(self, fail_first_save=False):
        """A live slot stays put while its freshly enumerated owner grows."""
        clock = Clock()
        process = SparseMemory(clock)
        old_size, new_size = 5898240, 5902336
        owner = process.layout[0]
        owner.update(size=old_size, type=0x20000, protect=4)
        first = owner['base'] + 8192
        process.install(first)
        original_regions = process.regions

        def changed_regions(**kwargs):
            if process.regions_calls:
                owner['size'] = new_size
            return original_regions(**kwargs)

        process.regions = changed_regions
        with tempfile.TemporaryDirectory() as directory:
            runtime = module.Runtime(state_dir=directory,
                seed_scope=Path(directory) / 'none.json',
                process_factory=lambda pid: process,
                process_finder=lambda: [{'pid': IDENTITY['pid']}])
            self.assertTrue(runtime._save_region_shapes(VERSION, [(old_size, 0x20000, 4)]))
            runtime.store = RecordingStore(clock)
            runtime.stop = FakeEvent(clock, clock.now + 14)
            runtime.wake = FakeEvent(clock)
            original_save = runtime._save_region_shapes
            attempts = []

            def save_shapes(*args):
                attempts.append(list(args[1]))
                if fail_first_save and len(attempts) == 1:
                    with patch.object(Path, 'replace', side_effect=OSError('fake disk failure')):
                        self.assertFalse(original_save(*args))
                    return False
                return original_save(*args)

            fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
            with patch.object(module, 'time', fake_time), \
                 patch.object(module, 'package_version', return_value=VERSION), \
                 patch.object(module.socket, 'socket', side_effect=AssertionError('UDP forbidden')), \
                 patch.object(module, 'Process', side_effect=AssertionError('native forbidden')), \
                 patch.object(runtime, '_save_scope', wraps=runtime._save_scope) as scope_save, \
                 patch.object(runtime, '_save_region_shapes', side_effect=save_shapes):
                runtime._memory_loop()
            self.assertGreaterEqual(process.regions_calls, 2)
            self.assertTrue(any(state['rows'] for _, state in runtime.store.samples))
            # The slot's nearby search extent never changed when the owner grew.
            self.assertEqual(scope_save.call_count, 1)
            restarted = module.Runtime(state_dir=directory)
            self.assertIn((new_size, 0x20000, 4), restarted._load_region_shapes(VERSION))
            return attempts

    def test_changed_owner_shape_persists_without_rewriting_unchanged_scope(self):
        attempts = self.run_owner_shape_change()
        self.assertEqual(len(attempts), 1, 'unchanged samples repeatedly rewrote shape hints')

    def test_failed_shape_save_is_retried_before_clearing_dirty(self):
        attempts = self.run_owner_shape_change(fail_first_save=True)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])

    def test_known_shape_lru_order_does_not_mark_persistent_membership_dirty(self):
        regions = [dict(base=0x100000, size=5898240, type=0x20000, protect=4),
                   dict(base=0x10000000, size=5902336, type=0x20000, protect=4)]
        shapes = [module.region_shape(region) for region in regions]
        planner = module.DiscoveryPlanner([], 0, shapes)
        planner.locator.reset(regions)
        planner._region_bases = [region['base'] for region in regions]
        rows = [{'first_string_address': region['base'] + 8192} for region in regions]
        planner.observe_rows(rows)
        self.assertFalse(planner.region_shapes_dirty)
        previous = list(planner.region_shapes)
        planner.observe_rows(list(reversed(rows)))
        self.assertNotEqual(planner.region_shapes, previous)
        self.assertFalse(planner.region_shapes_dirty)

    def run_memory(self, process, clock, seconds=12, refresh=None):
        with tempfile.TemporaryDirectory() as directory:
            runtime = module.Runtime(state_dir=directory,
                seed_scope=Path(directory) / 'none.json',
                process_factory=lambda pid: process,
                process_finder=lambda: [{'pid': IDENTITY['pid']}])
            runtime.store = RecordingStore(clock)
            runtime.stop = FakeEvent(clock, clock.now + seconds)
            runtime.wake = FakeEvent(clock)
            runtime._save_scope = lambda *args: None
            fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
            with patch.object(module, 'time', fake_time), \
                 patch.object(module, 'package_version', return_value=VERSION), \
                 patch.object(module.socket, 'socket', side_effect=AssertionError('UDP forbidden')), \
                 patch.object(module, 'Process', side_effect=AssertionError('native forbidden')):
                if refresh is None:
                    runtime._memory_loop()
                else:
                    with patch.object(module.CandidatePool, 'refresh', refresh):
                        runtime._memory_loop()
            return runtime.store.samples

    def test_first_global_pass_stays_fast_past_six_seconds(self):
        clock = Clock()
        process = SparseMemory(clock)
        process.layout[0]['size'] = 1024 * MIB
        first = process.layout[0]['base'] + 800 * MIB + 8192
        process.install(first)
        samples = self.run_memory(process, clock, seconds=30)
        seen = [instant for instant, state in samples
                if any(row['first_string_address'] == first for row in state['rows'])]
        self.assertTrue(seen, 'six-second backoff stranded the first pool late in a large heap')
        self.assertLess(seen[0] - 1000, 30)

    def test_dense_false_markers_and_one_slow_signature_resume_to_later_hit(self):
        clock = Clock()
        process = SparseMemory(clock)
        base = process.layout[0]['base']
        process.layout[0]['size'] = MIB
        # Each decoy has a third-name string but no complete layout. A valid
        # layout follows them in the same payload chunk, beyond one slice.
        for i in range(96):
            process.parts[base + 512 + i * 256] = b'pickup_model\0'
        first = base + 128 * 1024
        process.install(first)
        original = process.read
        def slow_reads(address, size):
            clock.sleep(.006)
            return original(address, size)
        process.read = slow_reads
        locator = module.Locator(process.layout)
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        found = []
        with patch.object(module, 'time', fake_time):
            for _ in range(240):
                rows, _ = locator.step(process, threading.Event(), seconds=.015, byte_limit=MIB)
                found.extend(rows)
                if any(row['first_string_address'] == first for row in found):
                    break
        self.assertTrue(any(row['first_string_address'] == first for row in found),
                        'a short slice restarted the same marker/stride forever')

    def test_periodic_metadata_timeout_keeps_existing_global_frontier(self):
        clock = Clock()
        process = SparseMemory(clock)
        planner = module.DiscoveryPlanner([], clock.now)
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time):
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
            before = planner.locator.attempted
            planner.next_metadata = 0
            with patch.object(process, 'regions', side_effect=TimeoutError('fake incomplete metadata')):
                planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
            self.assertGreaterEqual(planner.locator.attempted, before)
            self.assertIsNotNone(planner.locator.regions)
            self.assertEqual(planner.metadata_refresh_error, 'fake incomplete metadata')
            self.assertEqual(planner.metadata_refresh_failures, 1)

    def test_repeated_metadata_timeouts_back_off_and_success_resets_retry_delay(self):
        clock = Clock()
        process = SparseMemory(clock)
        planner = module.DiscoveryPlanner([], clock.now)
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time):
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
            with patch.object(process, 'regions', side_effect=TimeoutError('fake incomplete metadata')):
                for delay in (2, 4, 8, 16, 16):
                    began = clock.now
                    planner.next_metadata = 0
                    planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
                    # Native enumeration has its own budget; the cooldown
                    # starts after that attempt, independent of payload time.
                    self.assertAlmostEqual(planner.next_metadata - began, delay)
            planner.next_metadata = 0
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
            self.assertIsNone(planner.metadata_refresh_error)
            self.assertEqual(planner._metadata_failure_streak, 0)

    def test_pool_appearing_after_empty_pass_in_unchanged_allocation_is_seen(self):
        clock = Clock()
        process = SparseMemory(clock)
        process.large_read_seconds = .001
        process.layout[0]['size'] = 1024 * MIB
        planner = module.DiscoveryPlanner([], clock.now)
        stop = threading.Event()
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time):
            for _ in range(2000):
                planner.step(process, stop, fresh_rows=False, now=clock.now)
                if planner.full_scan_generation:
                    break
                clock.sleep(planner.poll_period(.5))
            self.assertGreater(planner.full_scan_generation, 0)
            # No allocation metadata changes and no formerly fresh row exists.
            # Entry into the mode must not depend on either of those signals.
            clock.sleep(10)
            first = process.layout[0]['base'] + 900 * MIB + 8192
            process.install(first)
            began, found = clock.now, []
            for _ in range(500):
                found.extend(planner.step(process, stop, fresh_rows=False, now=clock.now))
                if any(row['first_string_address'] == first for row in found):
                    break
                if clock.now - began >= 10:
                    break
                clock.sleep(planner.poll_period(.5))
            self.assertTrue(any(row['first_string_address'] == first for row in found),
                            'free-roam empty pass left later match acquisition in slow mode')
            self.assertLess(clock.now - began, 10)

    def test_expanding_region_during_partial_tail_chunk_does_not_skip_new_bytes(self):
        clock = Clock()
        process = SparseMemory(clock)
        base = process.layout[0]['base']
        process.layout[0]['size'] = 64 * 1024
        process.install(base + 448)
        original = process.read
        def slow_reads(address, size):
            clock.sleep(.006)
            return original(address, size)
        process.read = slow_reads
        locator = module.Locator(process.layout)
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time):
            locator.step(process, threading.Event(), seconds=.015, byte_limit=MIB)
            self.assertIsNotNone(locator._chunk_data)
            process.layout[0]['size'] = 512 * 1024
            first = base + 128 * 1024
            process.install(first)
            locator.reconcile(process.layout)
            found = []
            for _ in range(80):
                rows, _ = locator.step(process, threading.Event(), seconds=.015, byte_limit=MIB)
                found.extend(rows)
                if locator.complete:
                    break
        self.assertTrue(any(row['first_string_address'] == first for row in found))

    def test_resumed_discovery_hint_cannot_publish_a_changed_signature(self):
        clock = Clock()
        process = SparseMemory(clock)
        process.layout[0]['size'] = MIB
        first = process.layout[0]['base'] + 8192
        process.install(first)
        original, changed = process.read, [False]
        def mutate_after_read(address, size):
            clock.sleep(.006)
            data = original(address, size)
            if address == first + 32 and size == 32 and not changed[0]:
                # Discovery has read this descriptor, then the live object
                # changes before the next slice. It remains only a hint.
                changed[0] = True
                process.parts[first + 32] = bytes(32)
            return data
        process.read = mutate_after_read
        locator = module.Locator(process.layout)
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'time', fake_time):
            found = []
            for _ in range(60):
                rows, _ = locator.step(process, threading.Event(), seconds=.015, byte_limit=MIB)
                found.extend(rows)
                if any(row['first_string_address'] == first for row in found):
                    break
            self.assertTrue(changed[0])
            self.assertTrue(any(row['first_string_address'] == first for row in found))
            pool = module.CandidatePool(IDENTITY['session_key'], VERSION)
            pool.register(found)
            sample = pool.refresh(process, threading.Event(),
                module.IdentityCatalog(Path(directory) / 'none.json'))
        self.assertEqual(sample['rows'], [])
        self.assertIn(first, sample['removed'])
        self.assertIn('invalid_string_layout', sample['rejections'])

    def test_new_allocation_before_cursor_is_seen_without_waiting_for_full_sweep(self):
        clock, process = Clock(), None
        process = SparseMemory(clock)
        # A large original region keeps the main sweep far from completion.
        process.layout = [{'base': 0x10000000, 'size': 1024 * MIB,
                           'allocation_base': 0x10000000}]
        first, installed_at = 0x200000 + 8192, clock.now + 1
        installed = [False]

        def change_layout():
            if clock.now >= installed_at and not installed[0]:
                installed[0] = True
                process.layout.insert(0, {'base': 0x200000, 'size': MIB,
                                          'allocation_base': 0x200000})
                process.install(first)
        process.on_read = change_layout
        samples = self.run_memory(process, clock)
        seen = [instant for instant, state in samples
                if any(r['first_string_address'] == first for r in state['rows'])]
        self.assertTrue(seen, 'stale region metadata hid a new allocation')
        self.assertLess(seen[0] - installed_at, 6.0)
        self.assertGreater(process.regions_calls, 1)

    def test_metadata_deltas_over_priority_byte_cap_are_eventually_revisited(self):
        clock = Clock()
        process = SparseMemory(clock)
        process.layout = [{'base': 0x10000000, 'size': 16 * 1024 * MIB,
                           'allocation_base': 0x10000000}]
        first, installed_at = 0x200000 + 4 * 4 * MIB + 8192, clock.now + 1
        installed = [False]

        def change_layout():
            if clock.now >= installed_at and not installed[0]:
                installed[0] = True
                for i in range(5):
                    base = 0x200000 + i * 4 * MIB
                    process.layout.insert(i, {'base': base, 'size': 2 * MIB,
                                              'allocation_base': base})
                process.install(first)
        process.on_read = change_layout
        samples = self.run_memory(process, clock, seconds=16)
        self.assertTrue(any(any(r['first_string_address'] == first for r in state['rows'])
                            for _, state in samples), 'fifth metadata delta was dropped at the 8MiB priority cap')

    def test_changed_existing_region_discovers_candidate_beyond_first_priority_window(self):
        clock = Clock()
        process = SparseMemory(clock)
        base, far = 0x100000, 0x10000000
        process.layout = [{'base': base, 'size': 8 * MIB, 'allocation_base': base},
                          {'base': far, 'size': 1024 * MIB, 'allocation_base': far}]
        planner = module.DiscoveryPlanner([], clock.now)
        stop = threading.Event()
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time):
            planner.step(process, stop, fresh_rows=False, now=clock.now)
            # The normal sweep passed this allocation before the match began.
            planner.locator.index, planner.locator.address = 1, far + 256 * MIB
            planner.locator._clear_chunk()
            planner.next_global = clock.now + 1000
            first = base + 15 * MIB // 4 + 8192
            process.install(first)
            process.layout[0]['size'] += 4096
            planner.next_metadata = 0
            found = []
            for _ in range(80):
                found.extend(planner.step(process, stop, fresh_rows=False, now=clock.now))
                if any(row['first_string_address'] == first for row in found):
                    break
                clock.sleep(.04)
        self.assertTrue(any(row['first_string_address'] == first for row in found),
                        'accepting the first 2MiB discarded the changed region remainder')
        self.assertEqual(planner.full_scan_generation, 0)
        self.assertEqual(planner.locator.address, far + 256 * MIB)

    def test_large_changed_regions_keep_remainders_and_round_robin_windows(self):
        clock = Clock()
        process = SparseMemory(clock)
        bases = [0x100000, 0x10000000]
        process.layout = [{'base': base, 'size': 64 * MIB, 'allocation_base': base}
                          for base in bases]
        planner = module.DiscoveryPlanner([], clock.now)
        stop = threading.Event()
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time):
            planner.step(process, stop, fresh_rows=False, now=clock.now)
            for region in process.layout:
                region['size'] += 4096
            planner.next_metadata = 0
            planner.next_global = planner.next_priority = clock.now + 1000
            planner.step(process, stop, fresh_rows=False, now=clock.now)
        self.assertEqual(len(planner._deferred), 2,
                         'scheduling one bounded window must retain each large-region remainder')
        tasks = planner.priority.regions or []
        self.assertEqual([task['allocation_base'] for task in tasks[:4]],
                         [bases[0], bases[1], bases[0], bases[1]])
        self.assertLessEqual(sum(task['size'] for task in tasks), 8 * MIB)
        self.assertLessEqual(len(tasks), 256)

    def test_lost_rows_revisit_entire_known_owner_when_metadata_is_unchanged(self):
        clock = Clock()
        process = SparseMemory(clock)
        base, far = 0x100000, 0x10000000
        process.layout = [{'base': base, 'size': 8 * MIB, 'allocation_base': base},
                          {'base': far, 'size': 1024 * MIB, 'allocation_base': far}]
        planner = module.DiscoveryPlanner([], clock.now)
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        old, new = base + 8192, base + 6 * MIB + 8192
        with patch.object(module, 'time', fake_time):
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
            planner.observe_rows([{'first_string_address': old}])
            planner.step(process, threading.Event(), fresh_rows=True, now=clock.now)
            process.install(new)
            planner.locator.index, planner.locator.address = 1, far + 256 * MIB
            planner.locator._clear_chunk()
            planner._global_steps_since_priority = 3
            found = []
            for _ in range(100):
                found.extend(planner.step(process, threading.Event(), fresh_rows=False, now=clock.now))
                planner.next_global = clock.now + 1000
                if any(row['first_string_address'] == new for row in found):
                    break
                clock.sleep(.04)
        self.assertTrue(any(row['first_string_address'] == new for row in found),
                        'known owner beyond the near-slot window waited for the whole global sweep')
        self.assertEqual(planner.full_scan_generation, 0)

    def test_lost_rows_revisit_recent_region_behind_global_cursor(self):
        clock = Clock()
        process = SparseMemory(clock)
        process.layout[0]['size'] = 1024 * MIB
        old, new = 0x100000 + 8192, 0x100000 + 800 * 1024
        process.install(old)
        changed_at, changed = clock.now + 2, [False]

        def change_slots():
            if clock.now >= changed_at and not changed[0]:
                changed[0] = True
                process.remove(old)
                process.install(new, 2)
        process.on_read = change_slots
        samples = self.run_memory(process, clock, seconds=14)
        seen = [instant for instant, state in samples
                if any(r['first_string_address'] == new for r in state['rows'])]
        self.assertTrue(seen, 'a relocated slot behind the cursor waited for a whole sweep')
        self.assertLess(seen[0] - changed_at, 6.0)

    def test_no_fresh_rows_do_not_use_tracking_delay_for_retained_slots(self):
        clock, process = Clock(), None
        process = SparseMemory(clock)
        first = process.layout[0]['base'] + 8192
        process.install(first)
        failed_at = clock.now + 1
        original = module.CandidatePool.refresh

        def always_limited(pool, *args, **kwargs):
            if clock.now < failed_at:
                return original(pool, *args, **kwargs)
            return {'rows': [], 'removed': [], 'limited': True,
                    'sample_monotonic': clock.now, 'elapsed_seconds': 0,
                    'known_slots': len(pool.addresses), 'visited_slots': 0,
                    'read_calls': 0, 'read_bytes': 0, 'rejections': {}, 'read_gaps': 0}
        samples = self.run_memory(process, clock, seconds=12, refresh=always_limited)
        empty = [state for instant, state in samples if instant >= failed_at + 2]
        self.assertTrue(empty)
        for state in empty:
            self.assertFalse(state['rows'])
            self.assertNotEqual(state['memory_timing'].get('recovery', {}).get('mode'), 'tracking')
        self.assertTrue(any(state['memory_timing'].get('recovery', {}).get('mode') == 'empty_backoff'
                            for state in empty), 'unlimited burst while the world has no candidates')
        backed_off = [instant for instant, state in samples if instant >= failed_at + 6]
        self.assertGreater(len(backed_off), 3)
        self.assertLess(max(b - a for a, b in zip(backed_off, backed_off[1:])), .35,
                        'retained addresses still selected the old half-second delay')

    def test_scope_is_bounded_in_memory_and_keeps_recent_over_old(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            runtime = module.Runtime(state_dir=directory)
            planner = module.DiscoveryPlanner([], clock.now)
            rows = [{'first_string_address': 0x100000 + i * 65536} for i in range(1200)]
            planner.observe_rows(rows)
            self.assertLessEqual(len(planner.ranges), 256)
            self.assertLessEqual(sum(high - low for low, high in planner.ranges), 8 * MIB)
            last = rows[-1]['first_string_address']
            self.assertTrue(any(low <= last < high for low, high in planner.ranges))
            runtime.close()

    def test_old_overlapping_hint_cannot_evict_newest_slot_at_byte_cap(self):
        old = [[0x100000, 0x900000]]
        newest = 0x901000
        ranges = module.recent_ranges(old, [[0x8ff000, 0x903000]])
        self.assertLessEqual(sum(high - low for low, high in ranges), 8 * MIB)
        self.assertTrue(any(low <= newest < high for low, high in ranges))

    def test_priority_prune_removes_freed_pages_and_preserves_live_cursor(self):
        eligible = [{'base': 0x100000, 'size': 4 * MIB, 'allocation_base': 0x100000}]
        locator = module.Locator([dict(eligible[0])])
        locator.address = 0x180000
        locator.prune(eligible)
        self.assertEqual(locator.address, 0x180000)
        locator.prune([])
        self.assertTrue(locator.complete)
        self.assertEqual(locator.regions, [])
        self.assertIsNone(locator.address)

    def test_deferred_deltas_are_pruned_when_metadata_disappears(self):
        clock = Clock()
        process = SparseMemory(clock)
        planner = module.DiscoveryPlanner([], clock.now)
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time):
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
            process.layout = [{'base': 0x10000000 + i * 4 * MIB, 'size': 2 * MIB,
                               'allocation_base': 0x10000000 + i * 4 * MIB} for i in range(6)]
            planner.next_metadata = 0
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
            self.assertTrue(planner._deferred)
            removed_key = next(iter(planner._deferred))
            process.layout = [r for r in process.layout if r['base'] != removed_key[0]]
            planner.next_metadata = 0
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
        self.assertNotIn(removed_key, planner._deferred)
        self.assertLessEqual(len(planner._deferred), len(process.layout))

    def test_priority_queue_is_bounded_and_additions_keep_partial_cursor(self):
        locator = module.Locator([{'base': 0x100000, 'size': MIB}])
        locator.address = 0x180000
        locator.enqueue([{'base': 0x1000000 + i * MIB, 'size': MIB} for i in range(1000)])
        self.assertEqual(locator.address, 0x180000)
        self.assertLessEqual(len(locator.regions), 256)
        self.assertLessEqual(sum(r['size'] for r in locator.regions), 8 * MIB)
        locator.enqueue([{'base': 0x700000000 + i * MIB, 'size': MIB} for i in range(1000)])
        self.assertEqual(locator.address, 0x180000)
        self.assertLessEqual(len(locator.regions), 256)
        self.assertLessEqual(sum(r['size'] for r in locator.regions), 8 * MIB)

    def test_continuous_priority_work_cannot_starve_global_frontier(self):
        clock = Clock()
        process = SparseMemory(clock)
        planner = module.DiscoveryPlanner([], clock.now)
        planner.burst_until = clock.now - 1
        planner._search_pass_pending = False
        planner.next_metadata = clock.now + 100
        planner.locator.reset(process.layout)
        planner.priority.reset(process.layout)
        visits = []

        def priority(*args, **kwargs):
            visits.append('priority')
            return [], {}

        def global_step(*args, **kwargs):
            visits.append('global')
            planner.locator.attempted += 512 * 1024
            return [], {}

        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time), \
             patch.object(planner.priority, 'step', priority), \
             patch.object(planner.locator, 'step', global_step):
            for _ in range(12):
                planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
                clock.sleep(.21)
        self.assertGreaterEqual(visits.count('global'), 8)
        self.assertGreaterEqual(visits.count('priority'), 3)
        self.assertNotIn(['priority', 'priority', 'priority'],
                         [visits[i:i + 3] for i in range(len(visits) - 2)])
        self.assertGreater(planner.locator.attempted, 0)

    def test_metadata_order_and_recent_window_far_from_allocation_base(self):
        clock = Clock()
        process = SparseMemory(clock)
        process.layout = [{'base': 0x80000000, 'size': MIB, 'allocation_base': 0x80000000},
                          {'base': 0x100000, 'size': 1024 * MIB, 'allocation_base': 0x100000}]
        planner = module.DiscoveryPlanner([], clock.now)
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time):
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
            slot = 0x100000 + 500 * MIB
            planner.observe_rows([{'first_string_address': slot}])
            planner.request_recovery(clock.now, 'test')
            planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
        self.assertEqual(planner._region_bases, [0x100000, 0x80000000])
        self.assertTrue(any(low <= slot < high for low, high in planner.ranges))
        tasks = planner.priority.regions or []
        self.assertTrue(any(r['base'] <= slot < r['base'] + r['size'] for r in tasks))
        self.assertTrue(any(r['base'] > 0x100000 for r in tasks))

    def test_partial_scope_hits_cannot_starve_global_work(self):
        clock = Clock()
        process = SparseMemory(clock)
        planner = module.DiscoveryPlanner([[0x100000, 0x180000]], clock.now)
        planner.burst_until = clock.now - 1
        visits = []

        def repeated_prefix(reader, base, data, **kwargs):
            yield {'first_string_address': base + 8192}
            clock.sleep(.016)
            reader(base, 1)  # deadline after an already accepted prefix

        def global_step(*args, **kwargs):
            visits.append(clock.now)
            return [], {}

        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        with patch.object(module, 'time', fake_time), \
             patch.object(module, 'candidates_from_chunk', repeated_prefix), \
             patch.object(planner.locator, 'step', global_step):
            for _ in range(25):
                found = planner.step(process, threading.Event(), fresh_rows=False, now=clock.now)
                self.assertTrue(found)
                clock.sleep(.21)
        self.assertFalse(planner.scope.complete)
        self.assertIsNone(planner.scope.address)
        self.assertGreaterEqual(len(visits), 10)

    def test_128_migration_empty_recovery_cycles_stay_bounded(self):
        clock = Clock()
        process = SparseMemory(clock)
        store = StateStore(clock=clock.monotonic)
        store.set_session(IDENTITY, VERSION)
        planner = module.DiscoveryPlanner([], clock.now)
        pool = module.CandidatePool(IDENTITY['session_key'], VERSION)
        stop = threading.Event()
        fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        delays, scope_peak, priority_peak, priority_bytes_peak = [], 0, 0, 0
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(module, 'time', fake_time), \
             patch.object(module, 'Process', side_effect=AssertionError('native forbidden')), \
             patch.object(module.socket, 'socket', side_effect=AssertionError('UDP forbidden')):
            catalog = module.IdentityCatalog(Path(directory) / 'none.json')
            old = None
            for cycle in range(128):
                # The allocation is gone, rather than merely absent from one
                # limited refresh. The old row must disappear immediately.
                process.parts = {}
                process.layout = []
                if old is not None:
                    sample = pool.refresh(process, stop, catalog)
                    self.assertIn(old, sample['removed'])
                    store.publish_row_updates(IDENTITY['session_key'], sample['rows'],
                        removed=sample['removed'], now=sample['sample_monotonic'])
                    self.assertEqual(store.snapshot()['rows'], [])
                for _ in range(18):
                    planner.step(process, stop, fresh_rows=False, now=clock.now)
                    clock.sleep(.2)
                base = 0x100000 + cycle * 2 * MIB
                first = base + 8192
                process.layout = [{'base': base, 'size': MIB, 'allocation_base': base}]
                process.install(first, cycle + 1)
                installed_at, recovered = clock.now, False
                for _ in range(32):
                    found = planner.step(process, stop, fresh_rows=False, now=clock.now)
                    pool.register(found)
                    sample = pool.refresh(process, stop, catalog)
                    store.publish_row_updates(IDENTITY['session_key'], sample['rows'],
                        removed=sample['removed'], now=sample['sample_monotonic'])
                    planner.observe_rows(sample['rows'])
                    state = store.snapshot()
                    scope_size = sum(high - low for low, high in planner.ranges)
                    scope_peak = max(scope_peak, scope_size)
                    priority_peak = max(priority_peak, len(planner.priority.regions or []))
                    priority_bytes = sum(r['size'] for r in planner.priority.regions or [])
                    priority_bytes_peak = max(priority_bytes_peak, priority_bytes)
                    self.assertLessEqual(scope_size, 8 * MIB)
                    self.assertLessEqual(len(planner.ranges), 256)
                    self.assertLessEqual(len(planner.priority.regions or []), 256)
                    self.assertLessEqual(priority_bytes, 8 * MIB)
                    self.assertLessEqual(len(planner._deferred), len(process.layout))
                    if state['rows']:
                        self.assertEqual(state['rows'][0]['first_string_address'], first)
                        recovered = True
                        delays.append(clock.now - installed_at)
                        planner.step(process, stop, fresh_rows=True, now=clock.now)
                        break
                    clock.sleep(.2)
                self.assertTrue(recovered, f'migration cycle {cycle} did not recover')
                old = first
        self.assertEqual(len(delays), 128)
        self.assertLess(max(delays), 6.0)
        # Kept as plain numbers for an optional offline diagnostic runner.
        self.__class__.stress_result = {'cycles': len(delays),
            'maximum_simulated_recovery_seconds': round(max(delays), 3),
            'scope_peak_bytes': scope_peak, 'priority_peak_regions': priority_peak,
            'priority_peak_bytes': priority_bytes_peak,
            'metadata_refreshes': planner.metadata_refreshes}


if __name__ == '__main__':
    unittest.main()
