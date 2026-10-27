"""Map canvas frontend and an explicit offline pan/zoom preview.

The transparent renderer accepts verified camera frames only. The separate
preview synthesizes a view over historical world coordinates and never opens
a game process or a telemetry socket. It is not a live FH5 map camera reader.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk

from fh5_map_model import map_markers
from fh5_overlay import BACKGROUND, _click_through, level_colour
from pool import Frame


def synthetic_frame(snapshot, centre, span_x, width, height, *, opened=True, now=None):
    width, height = max(1, width), max(1, height)
    span_y = span_x * height / width
    matrix = [0.0] * 16
    matrix[0], matrix[9] = 2 / span_x, 2 / span_y
    matrix[12], matrix[13] = -2 * centre[0] / span_x, -2 * centre[1] / span_y
    matrix[14], matrix[15] = .5, 1
    return {'map_open': bool(opened), 'matrix': matrix,
            'viewport': {'x': 0, 'y': 0, 'width': width, 'height': height},
            'session_key': snapshot.get('session_key'),
            'package_version': snapshot.get('game_version') or 'unknown',
            'sample_monotonic': time.monotonic() if now is None else now,
            'source': 'synthetic_preview', 'projection_verified': False,
            'game_foreground': False}


class MapCanvasRenderer:
    NAME_COLOUR = '#FFC4ED'
    SHADOW_COLOUR = '#171323'
    NAME_FONT = ('Microsoft YaHei UI', 9, 'bold')

    def __init__(self, canvas, icon_provider=None):
        self.canvas = canvas
        if icon_provider is None and getattr(canvas, 'tk', None) is not None:
            from fh5_tier_icons import TierIcons
            icon_provider = TierIcons(canvas, size=32)
        self.icon_provider = icon_provider
        self.frame = Frame(canvas)
        self.icons = self.frame.pool('map_icon', 'image', tags=('map_icon',))
        self.dots = self.frame.pool('map_dot', 'oval', outline='#111827',
                                    width=2, tags=('map_dot',))
        self.grades = self.frame.pool('map_grade', 'text', tags=('map_grade',))
        self.name_shadows = self.frame.pool('map_name_shadow', 'text',
                                           tags=('map_name_shadow',))
        self.names = self.frame.pool('map_name', 'text', tags=('map_name',))
        self.last_render = {'points': [], 'visible_item_count': 0}

    @staticmethod
    def display_label(name):
        """Compact a leading model year without altering the source identity."""
        if not name:
            return '未知车型'
        text = str(name).strip()
        year, separator, model = text.partition(' ')
        if (separator and model.strip() and len(year) == 4 and year.isascii()
                and year.isdigit() and 1886 <= int(year) <= 2099):
            return f"{model.strip()} '{year[-2:]}"
        return text or '未知车型'

    def draw(self, layout, *, names=True, origin=(0, 0)):
        self.frame.begin()
        points = []
        for marker in layout.get('markers') or []:
            x, y = marker['pixel'][0] - origin[0], marker['pixel'][1] - origin[1]
            level = marker['level']
            colour = level_colour(level)
            display_label = self.display_label(marker['name'])
            icon = self.icon_provider.get(level) if self.icon_provider is not None else None
            if icon is not None:
                self.icons.image(x, y, icon)
                label_offset = 20
            else:
                self.dots.oval(x, y, 10, fill=colour, state='normal')
                self.grades.text(x, y, marker['grade'], fill='#101827',
                                 font=('Segoe UI', 10, 'bold'), state='normal')
                label_offset = 14
            if names:
                self.name_shadows.text(x + 1, y + label_offset + 1, display_label,
                                       anchor='n', fill=self.SHADOW_COLOUR,
                                       font=self.NAME_FONT, state='normal')
                self.names.text(x, y + label_offset, display_label,
                                anchor='n', fill=self.NAME_COLOUR,
                                font=self.NAME_FONT, state='normal')
            points.append({'F': marker['F'], 'fingerprint': marker['fingerprint'],
                           'pixel_x': x, 'pixel_y': y, 'level': level,
                           'car_id': marker['car_id'], 'car': marker['name'],
                           'display_label': display_label,
                           'icon_kind': 'game_original' if icon is not None else 'fallback',
                           'icon_level': level if icon is not None else None})
        self.frame.end()
        # Pool growth can create a new shadow above an older foreground label.
        # Keep the same layer order when items are reused after a count change.
        for tag in ('map_dot', 'map_grade', 'map_icon', 'map_name_shadow', 'map_name'):
            self.canvas.tag_raise(tag)
        self.last_render = {'kind': 'map', 'points': points,
                            'visible_item_count': self.frame.visible(),
                            'preview': layout.get('preview'),
                            'projection_verified': layout.get('projection_verified'),
                            'reason': layout.get('reason'),
                            'min_level': layout.get('min_level', 0),
                            'original_icon_count': sum(p['icon_kind'] == 'game_original' for p in points),
                            'icon_error': getattr(self.icon_provider, 'error', None)}
        return self.last_render


class MapOverlay:
    """Hidden until a future verified FH5 camera adapter publishes map_frame."""
    def __init__(self, root):
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', True)
        self.transparent_supported = False
        try:
            self.window.attributes('-transparentcolor', BACKGROUND)
            self.transparent_supported = True
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self.window, bg=BACKGROUND, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.renderer = MapCanvasRenderer(self.canvas)
        self.status_background = self.canvas.create_rectangle(0, 0, 0, 0,
            fill='#172332', outline='#576579', state='hidden')
        self.status_label = self.canvas.create_text(0, 0, anchor='nw',
            font=('Microsoft YaHei UI', 10, 'bold'), state='hidden')
        self._status_cache = None
        self.visible = False
        self.geometry = None
        self.last_render = None

    def _hide_status(self):
        if getattr(self, '_status_cache', None) is None:
            return
        for name in ('status_background', 'status_label'):
            item = getattr(self, name, None)
            if item is not None:
                self.canvas.itemconfigure(item, state='hidden')
        self._status_cache = None

    def _show_status(self, width, enabled):
        text = '地图车包：已开启 · Y切换' if enabled else '地图车包：已关闭 · Y切换'
        if hasattr(self, 'status_label'):
            key = (width, enabled, text)
            if getattr(self, '_status_cache', None) != key:
                left = max(12, width-288)
                self.canvas.coords(self.status_background, left, 14, left+272, 48)
                self.canvas.coords(self.status_label, left+10, 22)
                self.canvas.itemconfigure(self.status_background, state='normal')
                self.canvas.itemconfigure(self.status_label, text=text, state='normal',
                                          fill='#7FFFB0' if enabled else '#FFD486')
                self._status_cache = key
            self.canvas.tag_raise(self.status_background)
            self.canvas.tag_raise(self.status_label)
        self.last_render.update(enabled=enabled, status_text=text)

    def render(self, snapshot, camera=None, *, now=None, min_level=0, enabled=True):
        if not self.transparent_supported:
            self.last_render = self.renderer.draw({'markers': [], 'reason': 'transparent_overlay_unavailable'})
            self.window.withdraw()
            self.visible = False
            return self.last_render
        layout = map_markers(snapshot, camera, time.monotonic() if now is None else now,
                             min_level=min_level)
        camera_allowed = layout.get('reason') in ('ready', 'no_visible_markers')
        if type(enabled) is not bool or not camera_allowed:
            if type(enabled) is not bool:
                layout.update(markers=[], visible=False, reason='invalid_display_option')
            self.last_render = self.renderer.draw(layout)
            self._hide_status()
            self.window.withdraw()
            self.visible = False
            return self.last_render
        if not enabled:
            layout.update(markers=[], visible=False, reason='map_display_disabled')
        view = camera.viewport if hasattr(camera, 'viewport') else camera['viewport']
        get = (lambda key: getattr(view, key)) if hasattr(view, 'width') else view.__getitem__
        x, y, width, height = (int(round(get(k))) for k in ('x', 'y', 'width', 'height'))
        if not 1 <= width <= 32768 or not 1 <= height <= 32768 or abs(x) > 65536 or abs(y) > 65536:
            self.last_render = self.renderer.draw({'markers': [], 'reason': 'invalid_screen_viewport'})
            self._hide_status()
            self.window.withdraw()
            self.visible = False
            return self.last_render
        geometry = (x, y, width, height)
        if geometry != self.geometry:
            # Tk interprets a negative geometry origin as an offset from the
            # right/bottom edge, not a negative virtual-desktop coordinate.
            self.window.geometry(f'{width}x{height}+0+0')
            self.window.update_idletasks()
            if os.name == 'nt':
                import ctypes
                user = ctypes.WinDLL('user32', use_last_error=True)
                user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                user.GetAncestor.restype = ctypes.c_void_p
                user.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                user.SetWindowPos.restype = ctypes.c_int
                hwnd = user.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
                if not user.SetWindowPos(hwnd, -1, x, y, width, height, 0x10):
                    self.last_render = self.renderer.draw({'markers': [], 'reason': 'viewport_placement_failed'})
                    self._hide_status()
                    self.window.withdraw()
                    self.visible = False
                    return self.last_render
            else:
                self.window.geometry(f'{width}x{height}{x:+d}{y:+d}')
            self.geometry = geometry
        self.last_render = self.renderer.draw(layout, origin=(x, y))
        self._show_status(width, enabled)
        if not self.visible:
            self.window.update_idletasks()
            _click_through(self.window)
            self.window.deiconify()
            _click_through(self.window)
            self.visible = True
        return self.last_render

    def close(self):
        if self.window.winfo_exists():
            self.window.destroy()


class MapPreview:
    def __init__(self, report, *, smoke_seconds=None, record_path=None):
        from fh5_runtime import ReplayRuntime
        from fh5_record import FrameRecorder
        from fh5_map_background import ReferenceBackdrop
        self.runtime = ReplayRuntime(report)
        self.runtime.start()
        self.recorder = FrameRecorder(record_path)
        self.root = tk.Tk()
        self.root.title('FH5 车包地图预览 · 离线合成示例')
        self.root.geometry('1180x760')
        self.root.minsize(760, 500)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.closed, self.after_id = False, None
        self.opened, self.show_names = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.show_background = tk.BooleanVar(value=False)
        self.min_level = tk.IntVar(value=0)
        toolbar = ttk.Frame(self.root, padding=10)
        toolbar.pack(fill='x')
        ttk.Button(toolbar, text='显示全部车包', command=self.fit).pack(side='left')
        ttk.Checkbutton(toolbar, text='地图打开', variable=self.opened, command=self.draw).pack(side='left', padx=12)
        ttk.Checkbutton(toolbar, text='显示车型', variable=self.show_names, command=self.draw).pack(side='left')
        ttk.Checkbutton(toolbar, text='公开版：网格底图', variable=self.show_background, state='disabled').pack(side='left', padx=8)
        ttk.Label(toolbar, text='最低等级').pack(side='left', padx=(18, 5))
        ttk.Spinbox(toolbar, from_=0, to=10, width=4, textvariable=self.min_level,
                    command=self.draw).pack(side='left')
        ttk.Label(toolbar, text='拖动平移 · 滚轮缩放 · 悬停查看详情').pack(side='right')
        ttk.Label(self.root, text='离线预览：合成车包 + 合成视角。此画布不代表已经与游戏地图对齐。',
                  foreground='#9A5B00', padding=(10, 0, 10, 8)).pack(fill='x')
        self.canvas = tk.Canvas(self.root, bg='#111B29', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.status, self.detail = tk.StringVar(), tk.StringVar(value='固定坐标轴：+X 向右，+Z 向上。')
        ttk.Label(self.root, textvariable=self.status, padding=(10, 6)).pack(fill='x')
        ttk.Label(self.root, textvariable=self.detail, padding=(10, 0, 10, 8)).pack(fill='x')
        self.renderer = MapCanvasRenderer(self.canvas)
        self.backdrop = ReferenceBackdrop(self.canvas)
        self.centre, self.span_x = [0.0, 0.0], 20000.0
        self.fit_pending = True
        self.drag = None
        self.grid_signature = None
        self.layout = {'markers': []}
        self.canvas.bind('<Configure>', lambda event: self.draw())
        self.canvas.bind('<ButtonPress-1>', lambda event: setattr(self, 'drag', (event.x, event.y)))
        self.canvas.bind('<B1-Motion>', self.pan)
        self.canvas.bind('<ButtonRelease-1>', lambda event: setattr(self, 'drag', None))
        self.canvas.bind('<MouseWheel>', self.zoom)
        self.canvas.bind('<Motion>', self.hover)
        self.root.bind('<Return>', lambda event: self.draw())
        self.root.update_idletasks()
        self.fit()
        self.tick()
        if smoke_seconds is not None:
            self.root.after(max(1, int(smoke_seconds * 1000)), self.close)

    def fit(self):
        self.fit_pending = True
        self.draw()

    def _fit_rows(self, snapshot, width, height):
        rows = snapshot.get('rows') or []
        valid = [r for r in rows if all(isinstance(r.get(k), (int, float)) and math.isfinite(r[k]) for k in ('x', 'z'))]
        if valid:
            x0, x1 = min(r['x'] for r in valid), max(r['x'] for r in valid)
            z0, z1 = min(r['z'] for r in valid), max(r['z'] for r in valid)
            self.centre = [(x0 + x1) / 2, (z0 + z1) / 2]
            aspect = width / height
            self.span_x = max(200, (x1 - x0) * 1.15, (z1 - z0) * aspect * 1.15)
        self.fit_pending = False

    def pan(self, event):
        if self.drag is None:
            return
        scale = self.span_x / max(1, self.canvas.winfo_width())
        self.centre[0] -= (event.x - self.drag[0]) * scale
        self.centre[1] += (event.y - self.drag[1]) * scale
        self.drag = (event.x, event.y)
        self.draw()

    def zoom(self, event):
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        factor = 1 / 1.2 if event.delta > 0 else 1.2
        new_span = min(60000, max(200, self.span_x * factor))
        old_scale, new_scale = self.span_x / width, new_span / width
        self.centre[0] += (event.x - width / 2) * (old_scale - new_scale)
        self.centre[1] -= (event.y - height / 2) * (old_scale - new_scale)
        self.span_x = new_span
        self.draw()

    def grid(self, width, height):
        signature = (width, height, tuple(self.centre), self.span_x)
        if signature == self.grid_signature:
            return
        self.grid_signature = signature
        self.canvas.delete('grid')
        step = 10 ** math.floor(math.log10(max(self.span_x / 8, 1)))
        if self.span_x / step > 14:
            step *= 2
        scale = width / self.span_x
        left, right = self.centre[0] - self.span_x / 2, self.centre[0] + self.span_x / 2
        span_z = height / scale
        bottom, top = self.centre[1] - span_z / 2, self.centre[1] + span_z / 2
        for i in range(math.ceil(left / step), math.floor(right / step) + 1):
            x = width / 2 + (i * step - self.centre[0]) * scale
            self.canvas.create_line(x, 0, x, height, fill='#26364A', tags='grid')
            self.canvas.create_text(x + 4, height - 12, text=f'X {i*step:.0f}', fill='#75889E', anchor='w', tags='grid')
        for i in range(math.ceil(bottom / step), math.floor(top / step) + 1):
            y = height / 2 - (i * step - self.centre[1]) * scale
            self.canvas.create_line(0, y, width, y, fill='#26364A', tags='grid')
            self.canvas.create_text(5, y - 8, text=f'Z {i*step:.0f}', fill='#75889E', anchor='w', tags='grid')
        self.canvas.tag_lower('grid')

    def draw(self):
        if self.closed:
            return
        snapshot = self.runtime.snapshot()
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        if width <= 1 or height <= 1:
            self.layout = {'markers': [], 'preview': True, 'projection_verified': False,
                           'reason': 'waiting_preview_viewport'}
            self.renderer.draw(self.layout)
            self.status.set('等待预览画布完成布局…')
            return
        if self.fit_pending:
            self._fit_rows(snapshot, width, height)
        self.grid(width, height)
        self.backdrop.draw(self.centre, self.span_x, width, height, self.show_background.get())
        now = time.monotonic()
        camera = synthetic_frame(snapshot, self.centre, self.span_x, width, height,
                                 opened=self.opened.get(), now=now)
        try:
            min_level = int(self.min_level.get())
        except (ValueError, tk.TclError):
            min_level = 0
        self.layout = map_markers(snapshot, camera, now, allow_preview=True,
                                  min_level=max(0, min(10, min_level)))
        render = self.renderer.draw(self.layout, names=self.show_names.get())
        self.recorder.record(snapshot, render)
        self.status.set(f"历史记录 · 可见 {len(self.layout['markers'])}/{len(snapshot.get('rows') or [])} · "
                        f"中心 X {self.centre[0]:.1f} / Z {self.centre[1]:.1f} · {self.layout['reason']}")

    def hover(self, event):
        best = min(self.layout.get('markers') or [],
                   key=lambda m: math.hypot(m['pixel'][0]-event.x, m['pixel'][1]-event.y), default=None)
        if best and math.hypot(best['pixel'][0]-event.x, best['pixel'][1]-event.y) <= 18:
            self.detail.set(f"{best['label']} · CarID {best['car_id'] or '未知'} · 历史记录")
        else:
            self.detail.set('固定坐标轴：+X 向右，+Z 向上。')

    def tick(self):
        if self.closed:
            return
        try:
            self.draw()
        except Exception as error:
            self.renderer.draw({'markers': [], 'reason': 'preview_error'})
            self.status.set(f'预览已清空：{type(error).__name__}: {error}')
        self.after_id = self.root.after(100, self.tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
        self.recorder.close()
        self.runtime.close()
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.close()


def run_preview(report, *, headless=False, seconds=2, smoke_seconds=None, record_path=None):
    if not headless:
        MapPreview(report, smoke_seconds=smoke_seconds, record_path=record_path).run()
        return 0
    from fh5_runtime import ReplayRuntime
    from fh5_record import FrameRecorder
    from fh5_map_background import ReferenceBackdrop
    runtime, recorder = ReplayRuntime(report), FrameRecorder(record_path)
    runtime.start()
    try:
        # The public preview intentionally has no extracted game artwork.
        reference = ReferenceBackdrop(None)
        reference_available = reference.load()
        snapshot = runtime.snapshot()
        xs, zs = [r['x'] for r in snapshot['rows']], [r['z'] for r in snapshot['rows']]
        centre = [(min(xs)+max(xs))/2, (min(zs)+max(zs))/2] if xs else [0, 0]
        span = max(200, (max(xs)-min(xs))*1.15, (max(zs)-min(zs))*1.6*1.15) if xs else 20000
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            snapshot, now = runtime.snapshot(), time.monotonic()
            camera = synthetic_frame(snapshot, centre, span, 1200, 750, now=now)
            layout = map_markers(snapshot, camera, now, allow_preview=True)
            recorder.record(snapshot, {'kind': 'map_preview_projection', 'markers': layout['markers'],
                                       'preview': True, 'projection_verified': False})
            time.sleep(.1)
        print(json.dumps({'mode': 'offline_map_preview', 'markers': len(layout['markers']),
                          'projection_verified': False, 'game_connected': False,
                          'reference_background_available': bool(reference_available),
                          'reference_alignment_verified': False}, ensure_ascii=True))
        return 0
    finally:
        recorder.close()
        runtime.close()
