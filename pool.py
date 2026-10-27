# Manually reconstructed from the supplied CPython 3.13 bytecode.
# Readable equivalent; original formatting/comments are not recoverable.
"""Reusable canvas items, so a redraw moves things instead of rebuilding them.

Both overlays used to clear the canvas and create every shape again on each
frame. At sixty frames a second with seventy-two crates that is around
thirteen thousand Tk objects created per second, each one a trip through the
Tcl interpreter - measured at roughly forty percent of the radar's CPU, and
the reason the icons stutter while the map is panned.

Nothing about the picture requires that. The same handful of ovals and labels
are wanted every frame, just somewhere else, so they are made once and then
moved with coords() and itemconfig(). Anything left over from a frame with
more crates is parked off-screen rather than destroyed, because destroying
and recreating is the cost being avoided.
"""
HIDDEN = ('hidden',)

class Pool:
    """A bag of canvas items of one kind, handed out and reused per frame."""

    def __init__(self, cv, kind, **defaults):
        self.cv = cv
        self.kind = kind
        self.defaults = defaults
        self.items = []
        self.used = 0
        self.state = {}

    def _make(self):
        k, cv = (self.kind, self.cv)
        if k == 'oval':
            return cv.create_oval(-99, -99, -98, -98, **self.defaults)
        if k == 'text':
            return cv.create_text(-99, -99, text='', **self.defaults)
        if k == 'image':
            return cv.create_image(-99, -99, **self.defaults)
        if k == 'line':
            return cv.create_line(-99, -99, -98, -98, **self.defaults)
        raise ValueError(k)

    def get(self):
        if self.used >= len(self.items):
            self.items.append(self._make())
        item = self.items[self.used]
        self.used += 1
        return item

    def _apply(self, i, key, **kw):
        """itemconfig only when something changed, state included.

The caller's own state wins. Several call sites pass state="normal"
themselves, and adding it as a second keyword raised TypeError inside
a Tk after() callback - which does not crash anything visibly, it
just stops the redraw being rescheduled, so the overlay went silently
blank. Hence setdefault, and hence the guard around draw().
"""
        kw.setdefault('state', 'normal')
        key += (kw['state'],)
        if self.state.get(i) != key:
            self.state[i] = key
            self.cv.itemconfig(i, **kw)

    def oval(self, x, y, r, **kw):
        i = self.get()
        self.cv.coords(i, x - r, y - r, x + r, y + r)
        self._apply(i, ('oval',) + tuple(sorted(kw.items())), **kw)
        return i

    def text(self, x, y, s, **kw):
        """Move always, reconfigure only when something actually changed.

itemconfig is no cheaper than create in Tk - measured at 23.6 ms a
frame against 27.4 for rebuilding everything, which is nothing. The
win is in not calling it: position changes every frame, but the text,
colour and font almost never do.
"""
        i = self.get()
        self.cv.coords(i, x, y)
        self._apply(i, ('text', s, kw.get('fill'), kw.get('font')), text=s, **kw)
        return i

    def image(self, x, y, img):
        i = self.get()
        self.cv.coords(i, x, y)
        self._apply(i, ('image', img), image=img)
        return i

    def line(self, x0, y0, x1, y1, **kw):
        i = self.get()
        self.cv.coords(i, x0, y0, x1, y1)
        self._apply(i, ('line',) + tuple(sorted(kw.items())), **kw)
        return i

    def begin(self):
        self.used = 0

    def end(self):
        """Park whatever this frame did not need, without destroying it.

Hidden rather than merely moved off-canvas: Tk still walks hidden
items far more cheaply than it re-creates them, and leaving a few
hundred live objects stacked at the same off-screen point showed up
in profiles of its own.
"""
        for i in self.items[self.used:]:
            if self.state.get(i) != HIDDEN:
                self.cv.itemconfig(i, state='hidden')
                self.state[i] = HIDDEN

class Frame:
    """The pools an overlay needs, driven together."""

    def __init__(self, cv):
        self.cv = cv
        self.pools = {}

    def pool(self, name, kind, **defaults):
        if name not in self.pools:
            self.pools[name] = Pool(self.cv, kind, **defaults)
        return self.pools[name]

    def begin(self):
        for p in self.pools.values():
            p.begin()

    def end(self):
        for p in self.pools.values():
            p.end()

    def visible(self):
        """How many items are actually on screen right now.

The measure that matters, and the one nothing was checking. Every
test until now watched whether the overlay had *decided* the map
was shut - which it did within 0.2 s all along - while the badges
already drawn stayed exactly where they were, because the branch
that noticed returned without clearing anything.
"""
        n = 0
        for p in self.pools.values():
            for i in p.items:
                if p.state.get(i) != HIDDEN:
                    n += 1
        return n
