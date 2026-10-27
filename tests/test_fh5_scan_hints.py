"""Cross-session hints contain metadata only; local diagnostics are bounded."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fh5_runtime as module
import test_fh5_cold_start as fixtures

Clock, FakeMemory, region, MIB = fixtures.Clock, fixtures.FakeMemory, fixtures.region, fixtures.MIB


class ScanHintTests(unittest.TestCase):
    def test_address_free_shape_prioritizes_current_owner_after_relocation(self):
        clock = Clock()
        # Large numbers of otherwise eligible heaps precede a newly located
        # owner; only its shape is known, never its new base or object offset.
        layout = [region(0x100000 + i * 32 * MIB, 16 * MIB) for i in range(64)]
        owner = region(0x1000000000, 5898240)
        process = FakeMemory(clock, layout + [owner])
        first = owner['base'] + 4 * MIB + 8192
        process.install(first)
        planner = module.DiscoveryPlanner([], clock.now, [(5898240, 0x20000, 4)])
        found, elapsed = fixtures.ColdStartTests().discover_until(planner, process, clock, first, 2)
        self.assertIn(first, [row['first_string_address'] for row in found])
        self.assertLess(elapsed, 2)

    def test_shapes_are_version_scoped_and_cannot_supply_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            payload = {'package_version': module.SUPPORTED_VERSION,
                       'region_shapes': [{'size': 5898240, 'type': 0x20000, 'protect': 4,
                                          'base': 0xdead0000},
                                         {'size': 16 * MIB + 4096, 'type': 0x20000, 'protect': 4}]}
            profile = root / 'pool_region_shapes.json'
            profile.write_text(json.dumps(payload), encoding='utf-8')
            runtime = module.Runtime(state_dir=root)
            with patch.object(module, 'RESOURCE_DIR', root):
                self.assertEqual(runtime._load_region_shapes(module.SUPPORTED_VERSION),
                                 [(5898240, 0x20000, 4)])
                self.assertEqual(runtime._load_region_shapes('new-build'), [])
            runtime._save_region_shapes(module.SUPPORTED_VERSION, [(5898240, 0x20000, 4)])
            saved = json.loads(profile.read_text(encoding='utf-8'))
            self.assertEqual(set(saved['region_shapes'][0]), {'size', 'type', 'protect'})
            self.assertNotIn('session_key', saved)

    def test_learn_shape_only_from_an_owner_containing_a_fresh_slot(self):
        clock = Clock()
        owner = region(0x100000, 5898240)
        planner = module.DiscoveryPlanner([], clock.now)
        planner.locator.reset([owner])
        planner._region_bases = [owner['base']]
        planner.observe_rows([{'first_string_address': owner['end'] + 8192}])
        self.assertEqual(planner.region_shapes, [])
        planner.observe_rows([{'first_string_address': owner['base'] + 8192}])
        self.assertEqual(planner.region_shapes, [(5898240, 0x20000, 4)])

    def test_malformed_shape_profile_cannot_abort_memory_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = module.Runtime(state_dir=root)
            for payload in ([], None, 'bad', {'package_version': module.SUPPORTED_VERSION,
                                            'region_shapes': None}):
                with self.subTest(payload=payload), patch.object(module, 'RESOURCE_DIR', root):
                    (root / 'pool_region_shapes.json').write_text(json.dumps(payload), encoding='utf-8')
                    self.assertEqual(runtime._load_region_shapes(module.SUPPORTED_VERSION), [])

    def test_default_status_reports_progress_without_coordinates_or_append_log(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = module.Runtime(state_dir=directory)
            runtime.store.set_session({'pid': 123, 'session_key': 'fake:new'}, module.SUPPORTED_VERSION)
            runtime.store.set_status(state='searching', scan_progress={'attempted_mib': 123},
                                     memory_timing={'first_rows_elapsed_seconds': None})
            runtime._write_scan_status(force=True)
            file = Path(directory) / 'scan_status.json'
            report = json.loads(file.read_text(encoding='utf-8'))
            self.assertEqual(report['state'], 'searching')
            self.assertEqual(report['scan_progress']['attempted_mib'], 123)
            self.assertNotIn('rows', report)
            runtime.store.set_status(state='updated')
            runtime._write_scan_status(force=True)
            self.assertEqual(json.loads(file.read_text(encoding='utf-8'))['state'], 'updated')
            self.assertEqual(list(Path(directory).glob('scan_status*')), [file])


if __name__ == '__main__':
    unittest.main()
