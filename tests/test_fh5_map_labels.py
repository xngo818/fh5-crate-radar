"""Map badge presentation tests with no Tk window, game or network access."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_map_view import MapCanvasRenderer
from fh5_overlay import level_colour


class FakeCanvas:
    def __init__(self):
        self.items = {}
        self.raised_tags = []

    def _create(self, coords, options):
        item = len(self.items) + 1
        self.items[item] = {'coords': tuple(coords), 'state': 'normal', **options}
        return item

    def create_oval(self, *coords, **options):
        return self._create(coords, options)

    def create_text(self, *coords, **options):
        return self._create(coords, options)

    def create_image(self, *coords, **options):
        return self._create(coords, options)

    def coords(self, item, *coords):
        self.items[item]['coords'] = tuple(coords)

    def itemconfig(self, item, **options):
        self.items[item].update(options)

    def tag_raise(self, tag):
        self.raised_tags.append(tag)

    def visible(self):
        return {item: details for item, details in self.items.items()
                if details['state'] != 'hidden'}


def marker(**changes):
    result = {'F': '0x123400', 'fingerprint': 'object-a', 'pixel': [640, 380],
              'level': 4, 'grade': '4', 'name': '2008 Mitsubishi Lancer Evolution X GSR',
              'car_id': 1009, 'identity_status': 'verified'}
    result.update(changes)
    return result


def layout(*markers):
    return {'markers': list(markers), 'preview': False,
            'projection_verified': True, 'reason': 'ok'}


class MapLabelTests(unittest.TestCase):
    def setUp(self):
        self.canvas = FakeCanvas()
        self.renderer = MapCanvasRenderer(self.canvas)

    def item(self, pool, index=0):
        return self.canvas.items[pool.items[index]]

    def test_badge_is_at_projected_object_and_model_is_below_it(self):
        source = layout(marker())
        original = deepcopy(source)
        result = self.renderer.draw(source, origin=(240, 80))
        self.assertEqual(self.item(self.renderer.dots)['coords'], (390, 290, 410, 310))
        self.assertEqual(self.item(self.renderer.grades)['coords'], (400, 300))
        self.assertEqual(self.item(self.renderer.grades)['text'], '4')
        self.assertEqual(self.item(self.renderer.dots)['fill'], level_colour(4))
        name = self.item(self.renderer.names)
        shadow = self.item(self.renderer.name_shadows)
        self.assertEqual(name['coords'], (400, 314))
        self.assertEqual(shadow['coords'], (401, 315))
        self.assertEqual(name['anchor'], 'n')
        self.assertEqual(name['fill'], '#FFC4ED')
        self.assertNotEqual(shadow['fill'], name['fill'])
        self.assertEqual(name['text'], "Mitsubishi Lancer Evolution X GSR '08")
        self.assertEqual(shadow['text'], name['text'])
        self.assertEqual(result['visible_item_count'], 4)
        self.assertEqual(result['points'][0]['car'], original['markers'][0]['name'])
        self.assertEqual(result['points'][0]['display_label'], name['text'])
        self.assertEqual(result['points'][0]['car_id'], 1009)
        self.assertEqual(source, original)

    def test_year_compaction_preserves_model_digits_and_existing_labels(self):
        cases = [
            ('1984 Peugeot 205 T16', "Peugeot 205 T16 '84"),
            ('1963 Ferrari 250 GT Berlinetta Lusso', "Ferrari 250 GT Berlinetta Lusso '63"),
            ("Lancer GSR '08", "Lancer GSR '08"),
            ('Peugeot 205 T16 1984', 'Peugeot 205 T16 1984'),
            ('205 T16', '205 T16'),
            ('2008', '2008'),
            ('9999 Vehicle', '9999 Vehicle'),
            ('２００８ Vehicle', '２００８ Vehicle'),
            (None, '未知车型'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(self.renderer.display_label(source), expected)

    def test_names_off_hides_foreground_and_shadow_but_retains_grade(self):
        source = layout(marker())
        self.renderer.draw(source)
        result = self.renderer.draw(source, names=False)
        self.assertEqual(self.item(self.renderer.names)['state'], 'hidden')
        self.assertEqual(self.item(self.renderer.name_shadows)['state'], 'hidden')
        self.assertEqual(self.item(self.renderer.grades)['state'], 'normal')
        self.assertEqual(result['visible_item_count'], 2)
        self.assertEqual(len(self.canvas.visible()), 2)
        self.renderer.draw(source)
        self.assertEqual(self.item(self.renderer.names)['state'], 'normal')
        self.assertEqual(self.item(self.renderer.name_shadows)['state'], 'normal')
        self.assertEqual(len(self.canvas.items), 4)

    def test_changed_marker_count_reuses_items_and_clears_all_stale_parts(self):
        second = marker(F='0x567800', fingerprint='object-b', pixel=[200, 180],
                        level=0, grade='?', name=None, car_id=None,
                        identity_status='unknown')
        result = self.renderer.draw(layout(marker(), second))
        self.assertEqual(result['visible_item_count'], 8)
        self.assertEqual(self.item(self.renderer.dots, 1)['fill'], level_colour(0))
        self.assertEqual(self.item(self.renderer.names, 1)['text'], '未知车型')
        self.assertEqual(result['points'][1]['car'], None)
        self.assertEqual(result['points'][1]['car_id'], None)
        self.renderer.draw(layout(marker(pixel=[150, 160])))
        for pool in self.renderer.frame.pools.values():
            if len(pool.items) > 1:
                self.assertEqual(self.item(pool, 1)['state'], 'hidden')
        self.assertEqual(self.item(self.renderer.grades)['coords'], (150, 160))
        empty = self.renderer.draw(layout())
        self.assertEqual(empty['points'], [])
        self.assertEqual(empty['visible_item_count'], 0)
        self.assertFalse(self.canvas.visible())
        self.renderer.draw(layout(second))
        self.assertEqual(len(self.canvas.items), 8)
        self.assertEqual(self.item(self.renderer.grades)['text'], '?')
        self.assertEqual(self.item(self.renderer.names)['text'], '未知车型')

    def test_layer_order_restored_after_pool_grows(self):
        self.renderer.draw(layout(marker()))
        self.renderer.draw(layout(marker(), marker(F='0x567800')))
        self.assertEqual(self.canvas.raised_tags[-5:],
                         ['map_dot', 'map_grade', 'map_icon', 'map_name_shadow', 'map_name'])


if __name__ == '__main__':
    unittest.main()
