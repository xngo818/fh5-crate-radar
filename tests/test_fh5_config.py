import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fh5_config
from fh5_config import DEFAULTS, validated
from fh5_controller import BUTTON_CHOICES


class ConfigTests(unittest.TestCase):
    def test_defaults_and_legacy_config_migration(self):
        self.assertEqual(DEFAULTS['pad_button'], 0x4000)
        self.assertEqual(DEFAULTS['map_pad_button'], 0x8000)
        self.assertIs(DEFAULTS['map_visible'], True)
        self.assertEqual(DEFAULTS['min_level'], 4)
        legacy = validated({'pad_button': 0x40, 'visible': False})
        self.assertEqual(legacy['pad_button'], 0x40)
        self.assertIs(legacy['visible'], False)
        self.assertEqual(legacy['map_pad_button'], 0x8000)
        self.assertIs(legacy['map_visible'], True)
        self.assertEqual(legacy['min_level'], 4)

    def test_both_bindings_accept_known_masks_independently(self):
        for _, mask in BUTTON_CHOICES:
            radar = validated({'pad_button': mask})
            map_config = validated({'map_pad_button': mask})
            self.assertEqual(radar['pad_button'], mask)
            self.assertEqual(radar['map_pad_button'], 0x8000)
            self.assertEqual(map_config['pad_button'], 0x4000)
            self.assertEqual(map_config['map_pad_button'], mask)

    def test_unknown_or_wrong_type_bindings_fall_back_independently(self):
        for invalid in (True, False, 32768.0, '32768', None, [], {}, -1,
                        0xC000, 0xFFFF):
            config = validated({'pad_button': invalid,
                                'map_pad_button': invalid})
            self.assertEqual(config['pad_button'], 0x4000)
            self.assertEqual(config['map_pad_button'], 0x8000)

    def test_visible_flags_are_separate_exact_booleans(self):
        for radar, map_visible in ((True, False), (False, True),
                                   (False, False), (True, True)):
            config = validated({'visible': radar, 'map_visible': map_visible})
            self.assertIs(config['visible'], radar)
            self.assertIs(config['map_visible'], map_visible)
        for invalid in (0, 1, 'false', 'true', None, [], {}):
            config = validated({'visible': invalid, 'map_visible': invalid})
            self.assertIs(config['visible'], True)
            self.assertIs(config['map_visible'], True)

    def test_minimum_level_is_exact_integer_at_least_four(self):
        for level in range(4, 11):
            self.assertEqual(validated({'min_level': level})['min_level'], level)
        for invalid in (True, False, 0, 1, 3, 11, -1, 4.0, '4',
                        None, [], {}):
            self.assertEqual(validated({'min_level': invalid})['min_level'], 4)

    def test_validated_returns_independent_default_copy(self):
        first = validated(None)
        first['map_visible'] = False
        first['min_level'] = 10
        self.assertEqual(validated(None), DEFAULTS)

    def test_save_and_load_round_trip_and_upgrade_legacy_file(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / 'fh5_settings.json'
            with patch.object(fh5_config, 'STATE_DIR', folder), \
                    patch.object(fh5_config, 'PATH', path):
                self.assertEqual(fh5_config.load(), DEFAULTS)
                fh5_config.save({'visible': False, 'map_visible': True,
                                 'pad_button': 0x4000,
                                 'map_pad_button': 0x8000, 'min_level': 4})
                loaded = fh5_config.load()
                self.assertIs(loaded['visible'], False)
                self.assertIs(loaded['map_visible'], True)
                self.assertEqual(loaded['pad_button'], 0x4000)
                self.assertEqual(loaded['map_pad_button'], 0x8000)
                self.assertEqual(loaded['min_level'], 4)
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                                 loaded)
                self.assertEqual(list(folder.glob('*.tmp')), [])
                path.write_text('{"visible":false,"pad_button":64}',
                                encoding='utf-8')
                migrated = fh5_config.load()
                self.assertIs(migrated['visible'], False)
                self.assertIs(migrated['map_visible'], True)
                self.assertEqual(migrated['pad_button'], 0x40)
                self.assertEqual(migrated['map_pad_button'], 0x8000)
                self.assertEqual(migrated['min_level'], 4)


if __name__ == '__main__':
    unittest.main()
