"""Headless car-up radar geometry and stale-heading cleanup checks."""
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_overlay import RadarOverlay, project
from pool import Frame


def player(yaw=0):
    return {'x': 1234.0, 'y': 100.0, 'z': -2345.0, 'yaw': yaw}


def relative_row(dx, dz):
    return {'x': 1234.0 + dx, 'y': 100.0, 'z': -2345.0 + dz,
            'first_string_address': '0x1000', 'fingerprint': 'fixture', 'level': 4}


class FakeCanvas:
    def __init__(self):
        self.items = {}

    def create_oval(self, *coords, **options):
        return self._create(coords, options)

    def create_text(self, *coords, **options):
        return self._create(coords, options)

    def _create(self, coords, options):
        item = len(self.items) + 1
        self.items[item] = {'coords': coords, **options}
        return item

    def coords(self, item, *coords):
        self.items[item]['coords'] = coords

    def itemconfig(self, item, **options):
        self.items[item].update(options)

    itemconfigure = itemconfig

    def tag_raise(self, item):
        pass


def headless_overlay():
    # Bypass the constructor; this fixture never creates Tk or native windows.
    radar = RadarOverlay.__new__(RadarOverlay)
    radar.canvas = FakeCanvas()
    radar.frame = Frame(radar.canvas)
    radar.dots = radar.frame.pool('dot', 'oval', state='hidden')
    radar.labels = radar.frame.pool('label', 'text', state='hidden')
    radar.distances = radar.frame.pool('distance', 'text', state='hidden')
    radar.radius = 100.0
    radar.centre = 150.0
    radar.visible = True
    radar.self_dot = radar.canvas.create_text(150, 150)
    radar.caption = radar.canvas.create_text(150, 310)
    radar.detail = radar.canvas.create_text(150, 330)
    return radar


class RadarHeadingTests(unittest.TestCase):
    def test_front_right_back_left_at_cardinal_headings(self):
        # Explicit world vectors: no production rotation is reused to build them.
        headings = [
            (0, [(0, 100), (100, 0), (0, -100), (-100, 0)]),
            (math.pi / 2, [(100, 0), (0, -100), (-100, 0), (0, 100)]),
            (-math.pi / 2, [(-100, 0), (0, 100), (100, 0), (0, -100)]),
            (math.pi, [(0, -100), (-100, 0), (0, 100), (100, 0)]),
        ]
        expected = [(150, 130), (170, 150), (150, 170), (130, 150)]
        for yaw, world_vectors in headings:
            for direction, (vector, pixel) in enumerate(zip(world_vectors, expected)):
                with self.subTest(yaw=yaw, direction=direction):
                    result = project(relative_row(*vector), player(yaw), 1000, 100, 150)
                    self.assertIsNotNone(result)
                    self.assertAlmostEqual(result[0], pixel[0], places=8)
                    self.assertAlmostEqual(result[1], pixel[1], places=8)
                    self.assertAlmostEqual(result[2], 100, places=8)

    def test_stationary_car_rotation_moves_marker_preserves_world_distance(self):
        fixed = relative_row(0, 100)
        results = [project(fixed, player(yaw), 1000, 100, 150)
                   for yaw in (0, math.pi / 2, math.pi, -math.pi / 2)]
        expected = [(150, 130), (130, 150), (150, 170), (170, 150)]
        for result, pixel in zip(results, expected):
            self.assertAlmostEqual(result[0], pixel[0], places=8)
            self.assertAlmostEqual(result[1], pixel[1], places=8)
            self.assertAlmostEqual(result[2], 100, places=8)
            self.assertAlmostEqual(math.hypot(result[0]-150, result[1]-150), 20)

    def test_heading_wrap_is_continuous(self):
        fixed = relative_row(75, -100)
        before = project(fixed, player(math.pi - 1e-8), 1000, 100, 150)
        after = project(fixed, player(-math.pi + 1e-8), 1000, 100, 150)
        self.assertLess(math.hypot(before[0]-after[0], before[1]-after[1]), 1e-5)
        self.assertEqual(before[2], after[2])
        # Exactly equivalent representations also agree.
        self.assertAlmostEqual(project(fixed, player(math.pi), 1000, 100, 150)[0],
                               project(fixed, player(-math.pi), 1000, 100, 150)[0])

    def test_missing_or_invalid_heading_never_returns_a_point(self):
        invalid = [None, float('nan'), float('inf'), -float('inf'), '0', True, {}, []]
        for yaw in invalid:
            with self.subTest(yaw=repr(yaw)):
                self.assertIsNone(project(relative_row(0, 100), player(yaw), 1000, 100, 150))
        missing = player()
        del missing['yaw']
        self.assertIsNone(project(relative_row(0, 100), missing, 1000, 100, 150))

    def test_range_boundary_and_invalid_projection_dimensions(self):
        self.assertIsNotNone(project(relative_row(300, 400), player(), 1000, 100, 150))
        self.assertIsNone(project(relative_row(300, 400.001), player(), 1000, 100, 150))
        for span, radius in [(0, 100), (-1, 100), (1000, 0), (1000, -1),
                             (float('inf'), 100), (1000, float('nan'))]:
            with self.subTest(span=span, radius=radius):
                self.assertIsNone(project(relative_row(0, 100), player(), span, radius, 150))

    def test_frame_pool_hides_previous_points_when_heading_disappears(self):
        radar = headless_overlay()
        snapshot = {'player': player(), 'telemetry_age': .01, 'rows': [relative_row(0, 100)]}
        radar.render(snapshot, 1000)
        self.assertEqual(len(radar.last_render['points']), 1)
        self.assertEqual(radar.frame.visible(), 3)
        old_ids = [item for pool in radar.frame.pools.values() for item in pool.items]
        del snapshot['player']['yaw']
        radar.render(snapshot, 1000)
        self.assertEqual(radar.last_render['points'], [])
        self.assertEqual(radar.last_render['visible_item_count'], 0)
        self.assertEqual(radar.frame.visible(), 0)
        self.assertIsNone(radar.last_render['heading_yaw_rad'])
        self.assertEqual(radar.canvas.items[radar.self_dot]['state'], 'hidden')
        self.assertTrue(all(radar.canvas.items[item]['state'] == 'hidden' for item in old_ids))
        snapshot['player']['yaw'] = math.pi / 2
        radar.render(snapshot, 1000)
        self.assertEqual(radar.frame.visible(), 3)
        self.assertAlmostEqual(radar.last_render['points'][0]['pixel_x'], 130)


if __name__ == '__main__':
    unittest.main()
