"""Public source release: grid-only preview; no game artwork is included."""
class ReferenceBackdrop:
    def __init__(self, canvas):
        self.canvas = canvas
        self.rgb = self.meta = self.photo = None
        self.signature = None

    def load(self):
        return False

    def draw(self, centre, span_x, width, height, enabled):
        if self.canvas is not None:
            self.canvas.delete('map_background')
