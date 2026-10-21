"""Public fixtures exercise replay without any process or socket access."""
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fh5_runtime import ReplayRuntime
from fh5_map_background import ReferenceBackdrop
from fh5_tier_icons import TierIcons


class PublicReleaseTests(unittest.TestCase):
    def test_synthetic_demo_resolves_identities_without_live_providers(self):
        with patch('fh5_runtime.Process', side_effect=AssertionError('opened game')), \
             patch('fh5_runtime.find_processes', side_effect=AssertionError('enumerated game')), \
             patch.object(socket, 'socket', side_effect=AssertionError('opened socket')):
            runtime = ReplayRuntime(ROOT / 'examples/demo.json')
            runtime.start()
            try:
                snapshot = runtime.snapshot()
                self.assertTrue(snapshot['replay'])
                self.assertEqual(len(snapshot['rows']), 8)
                self.assertTrue(all(row['identity_status'] == 'verified' for row in snapshot['rows']))
                self.assertIsNone(snapshot['process_pid'])
                self.assertEqual(snapshot['source_session_key'], 'synthetic-demo:no-game-process')
            finally:
                runtime.close()

    def test_public_release_has_no_game_artwork_and_uses_fallback(self):
        self.assertFalse((ROOT / 'data/tier_icons').exists())
        self.assertFalse((ROOT / 'data/map_research').exists())
        self.assertFalse(ReferenceBackdrop(None).load())
        cache = TierIcons(object())
        self.assertIsNone(cache.get(4))
        self.assertIsNone(cache.get(4))

    def test_public_evidence_links_resolve_without_private_capture_paths(self):
        profile = json.loads((ROOT / 'data/fh5_map_profile.json').read_text('utf-8'))
        catalog = json.loads((ROOT / 'data/fh5_loot_mapping.json').read_text('utf-8'))
        self.assertEqual(len(catalog['entries']), 45)
        links = profile['evidence'] + [p for row in catalog['entries'].values() for p in row['evidence']]
        for link in links:
            self.assertFalse(link.startswith(('state/', 'sessions/')))
            self.assertTrue((ROOT / link.split('#', 1)[0]).is_file())


if __name__ == '__main__':
    unittest.main()
