"""FH5 coordinate radar, reusing the recovered project's canvas item pool.

Only the owner Tk application supplies data. No process, socket or background
scanner is created here; absolute game addresses are never cached by the UI.
"""
from __future__ import annotations

import ctypes
import math
import os
import tkinter as tk

from pool import Frame

BACKGROUND = '#010203'
TELEMETRY_MAX_AGE = 1.5


def fresh_player(snapshot):
    """Reject absent/stale position rather than carrying a previous frame."""
    player, age = snapshot.get('player'), snapshot.get('telemetry_age')
    if not isinstance(player, dict):
        return None
    try:
        if age is None or not 0 <= float(age) <= TELEMETRY_MAX_AGE:
            return None
        if not all(math.isfinite(float(player[key])) for key in ('x', 'y', 'z')):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return player


def project(row, player, span, radius, centre):
    """Car-up projection from this build's Data Out yaw (radians).

    Forward=(sin(yaw), cos(yaw)), right=(cos(yaw), -sin(yaw)); the
    convention was checked against recorded world movement, not FH6 offsets.
    """
    try:
        dx = float(row['x']) - float(player['x'])
        dz = float(row['z']) - float(player['z'])
        span = float(span)
        yaw = heading_yaw(player)
        radius, centre = float(radius), float(centre)
        distance = math.hypot(dx, dz)
        if (yaw is None or not all(math.isfinite(v) for v in (dx, dz, span, distance, radius, centre))
                or span <= 0 or radius <= 0):
            return None
        if distance > span / 2:
            return None
        scale = radius / (span / 2)
        sine, cosine = math.sin(yaw), math.cos(yaw)
        right, forward = dx * cosine - dz * sine, dx * sine + dz * cosine
        return centre + right * scale, centre - forward * scale, distance
    except (KeyError, TypeError, ValueError):
        return None


def heading_yaw(player):
    """Never silently replace a missing heading with a fixed-axis view."""
    if not isinstance(player, dict):
        return None
    value = player.get('yaw')
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) if math.isfinite(value) else None


def level_colour(level):
    if not level:
        return '#B8BDC5'
    if level <= 3:
        return '#4CE05B'
    if level <= 6:
        return '#4FA8FF'
    if level <= 9:
        return '#B06CFF'
    return '#FFC43D'


class Hotkeys:
    """Explicit Win32 signatures preserve 64-bit handles and return values."""
    def __init__(self):
        self.previous = {}
        self.api = None
        if os.name == 'nt':
            self.api = ctypes.WinDLL('user32', use_last_error=True)
            self.api.GetAsyncKeyState.argtypes = [ctypes.c_int]
            self.api.GetAsyncKeyState.restype = ctypes.c_short

    def pressed(self, vk):
        if self.api is None:
            return False
        down = bool(self.api.GetAsyncKeyState(vk) & 0x8000)
        previous = self.previous.get(vk, False)
        self.previous[vk] = down
        return down and not previous


def _click_through(window):
    if os.name != 'nt':
        return
    user = ctypes.WinDLL('user32', use_last_error=True)
    hwnd_type, long_ptr = ctypes.c_void_p, ctypes.c_ssize_t
    user.GetAncestor.argtypes = [hwnd_type, ctypes.c_uint]
    user.GetAncestor.restype = hwnd_type
    user.GetWindowLongPtrW.argtypes = [hwnd_type, ctypes.c_int]
    user.GetWindowLongPtrW.restype = long_ptr
    user.SetWindowLongPtrW.argtypes = [hwnd_type, ctypes.c_int, long_ptr]
    user.SetWindowLongPtrW.restype = long_ptr
    user.SetWindowPos.argtypes = [hwnd_type, hwnd_type, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_uint]
    user.SetWindowPos.restype = ctypes.c_int
    hwnd = user.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
    style = user.GetWindowLongPtrW(hwnd, -20)
    # Layered, transparent, tool window, no activation.
    user.SetWindowLongPtrW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x8000000)
    user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)


class RadarOverlay:
    def __init__(self, root, config):
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', True)
        if os.name == 'nt':
            self.window.attributes('-transparentcolor', BACKGROUND)
        self.size = int(config['size'])
        self.centre = self.size / 2
        self.radius = self.size / 2 - 24
        self.window.geometry(f'{self.size}x{self.size + 56}+35+200')
        self.canvas = tk.Canvas(self.window, width=self.size,
                                height=self.size + 56, bg=BACKGROUND,
                                highlightthickness=0)
        self.canvas.pack()
        self.frame = Frame(self.canvas)
        self.dots = self.frame.pool('dot', 'oval', outline='', state='hidden')
        self.labels = self.frame.pool('label', 'text', state='hidden')
        self.distances = self.frame.pool('distance', 'text', state='hidden')
        c, r = self.centre, self.radius
        self.canvas.create_oval(c-r, c-r, c+r, c+r, outline='#FFFFFF', width=2)
        for proportion in (0.4, 0.7):
            rr = r * proportion
            self.canvas.create_oval(c-rr, c-rr, c+rr, c+rr,
                                    outline='#87909D', width=1)
        self.canvas.create_text(c, c-r-12, text='前', fill='white', font=('Microsoft YaHei UI', 9))
        self.canvas.create_text(c+r+12, c, text='右', fill='white', font=('Microsoft YaHei UI', 9))
        self.self_dot = self.canvas.create_polygon(c,c-8,c-5,c+5,c+5,c+5,
                                               fill='white', outline='#252B34')
        self.caption = self.canvas.create_text(c, self.size + 6, text='',
                                               fill='white', font=('Microsoft YaHei UI', 9))
        self.detail = self.canvas.create_text(c, self.size + 27, text='',
                                              fill='#BDC4CE', font=('Microsoft YaHei UI', 8))
        self.visible = False
        self.last_render = None
        self.set_alpha(config['alpha'])

    def set_alpha(self, alpha):
        self.window.attributes('-alpha', min(1.0, max(0.2, float(alpha))))

    def set_visible(self, visible):
        visible = bool(visible)
        if visible == self.visible:
            return
        self.visible = visible
        if visible:
            self.window.update_idletasks()
            _click_through(self.window)
            self.window.deiconify()
            self.window.update_idletasks()
            _click_through(self.window)
        else:
            self.window.withdraw()

    def render(self, snapshot, span, *, min_level=0):
        # Replaced for every frame, including empty/stale telemetry frames.
        self.last_render = {'visible': self.visible, 'points': [],
                            'visible_item_count': 0, 'span': span,
                            'radius': self.radius, 'centre': self.centre,
                            'orientation': 'heading_up', 'heading_yaw_rad': None,
                            'min_level': min_level}
        self.frame.begin()
        player = fresh_player(snapshot)
        if player is not None and heading_yaw(player) is None:
            player = None
        self.last_render['heading_yaw_rad'] = heading_yaw(player)
        self.canvas.itemconfigure(self.self_dot, state='normal' if player else 'hidden')
        rows = snapshot.get('rows') or []
        displayed = 0
        if player is not None and type(min_level) is int and 0 <= min_level <= 10:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                level = row.get('level')
                if type(level) is not int or not 1 <= level <= 10:
                    level = 0
                if min_level and row.get('identity_status') != 'verified':
                    continue
                if level < min_level:
                    continue
                point = project(row, player, span, self.radius, self.centre)
                if point is None:
                    continue
                px, py, distance = point
                colour = level_colour(level)
                radius = 3.5 + max(level - 2, 0) * 0.4
                self.dots.oval(px, py, radius, fill=colour, state='normal')
                self.labels.text(px, py-radius-8, str(level) if level else '?',
                                 fill=colour, font=('Segoe UI', 8, 'bold'), state='normal')
                if distance < 300:
                    self.distances.text(px, py+radius+8, f'{distance:.0f}',
                                        fill=colour, font=('Segoe UI', 7), state='normal')
                self.last_render['points'].append({
                    'pixel_x': px, 'pixel_y': py, 'distance': distance,
                    'F': row.get('first_string_address', row.get('F')),
                    'fingerprint': row.get('fingerprint'), 'level': level,
                    'car': row.get('car'), 'car_id': row.get('car_id')})
                displayed += 1
        self.frame.end()
        self.last_render['visible_item_count'] = self.frame.visible()
        threshold = f'≥{min_level}级 · ' if type(min_level) is int and min_level > 0 else ''
        caption = f'{threshold}候选 {displayed}/{len(rows)} · 半径 {float(span)/2:.0f}' if player else '等待新鲜本车遥测 · 相对点已隐藏'
        self.canvas.itemconfigure(self.caption, text=caption)
        self.canvas.itemconfigure(self.detail, text='车头朝上 · F9/手柄 显示/隐藏 · END 退出')
        self.canvas.tag_raise(self.self_dot)

    def close(self):
        if self.window.winfo_exists():
            self.window.destroy()
