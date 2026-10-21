"""Original crest routing and pool cleanup, with an injected image provider."""
import unittest
from fh5_map_view import MapCanvasRenderer
from test_fh5_map_labels import FakeCanvas, layout, marker


class Icons:
    error = None

    def get(self, level):
        return f'original-level-{level}' if type(level) is int and 1 <= level <= 10 else None


class OriginalIconRendererTests(unittest.TestCase):
    def test_original_image_replaces_both_circle_and_grade_text(self):
        canvas = FakeCanvas()
        view = MapCanvasRenderer(canvas, icon_provider=Icons())
        result = view.draw(layout(marker()), origin=(240, 80))
        self.assertEqual(result['original_icon_count'], 1)
        self.assertEqual(result['visible_item_count'], 3)
        self.assertEqual(view.dots.used, 0)
        self.assertEqual(view.grades.used, 0)
        image = canvas.items[view.icons.items[0]]
        self.assertEqual(image['coords'], (400, 300))
        self.assertEqual(image['image'], 'original-level-4')
        self.assertEqual(canvas.items[view.names.items[0]]['coords'], (400, 320))
        self.assertEqual(result['points'][0]['icon_kind'], 'game_original')

    def test_level_change_reuses_item_and_clear_hides_image_and_name(self):
        canvas = FakeCanvas()
        view = MapCanvasRenderer(canvas, icon_provider=Icons())
        view.draw(layout(marker()))
        old = tuple(canvas.items)
        view.draw(layout(marker(level=10, grade='10')), names=False)
        self.assertEqual(tuple(canvas.items), old)
        self.assertEqual(canvas.items[view.icons.items[0]]['image'], 'original-level-10')
        self.assertEqual(len(canvas.visible()), 1)
        view.draw(layout())
        self.assertEqual(len(canvas.visible()), 0)
        self.assertEqual(view.last_render['visible_item_count'], 0)

    def test_missing_unknown_icon_uses_explicit_fallback_without_old_image(self):
        canvas = FakeCanvas()
        view = MapCanvasRenderer(canvas, icon_provider=Icons())
        view.draw(layout(marker()))
        result = view.draw(layout(marker(level=0, grade='?')))
        self.assertEqual(result['original_icon_count'], 0)
        self.assertEqual(result['points'][0]['icon_kind'], 'fallback')
        self.assertEqual(canvas.items[view.icons.items[0]]['state'], 'hidden')
        self.assertEqual(len(canvas.visible()), 4)


if __name__ == '__main__':
    unittest.main()
