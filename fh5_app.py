"""FH5 control panel and radar using one provider and one UDP listener."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

import fh5_config


def _runtime(args, config):
    # A replay must never instantiate the process reader or a UDP listener.
    if args.replay:
        from fh5_runtime import ReplayRuntime
        return ReplayRuntime(args.replay)
    from fh5_runtime import Runtime
    from fh5_paths import RESOURCE_DIR
    profile = getattr(args, 'map_profile', None) or RESOURCE_DIR / 'data' / 'fh5_map_profile.json'
    return Runtime(port=args.port or config['port'], period=0.5,
                   seed_scope=args.seed_scope, mapping_path=args.mapping,
                   map_profile_path=profile)


def headless(runtime, seconds, record_path=None):
    from fh5_record import FrameRecorder
    recorder = FrameRecorder(record_path)
    runtime.start()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            recorder.record(runtime.snapshot())
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        snapshot = runtime.snapshot()
        print(json.dumps({'state': snapshot.get('state'),
                          'session_key': snapshot.get('session_key'),
                          'rows': len(snapshot.get('rows') or []),
                          'player_available': snapshot.get('player') is not None,
                          'memory_state': snapshot.get('memory_state'),
                          'telemetry_state': snapshot.get('telemetry_state')},
                         ensure_ascii=True))
        return 0
    finally:
        recorder.close()
        runtime.close()


class Application:
    def __init__(self, runtime, config, smoke_seconds=None, record_path=None):
        from fh5_ui_dpi import UIThreadDpi
        self.ui_dpi = UIThreadDpi()
        self.map_dpi_ready = self.ui_dpi.enter()
        try:
            self._initialize(runtime, config, smoke_seconds, record_path)
        except Exception:
            root = getattr(self, 'root', None)
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
            self.ui_dpi.close()
            raise

    def _initialize(self, runtime, config, smoke_seconds=None, record_path=None):
        import tkinter as tk
        from tkinter import ttk
        from fh5_overlay import Hotkeys, RadarOverlay
        from fh5_record import FrameRecorder
        from fh5_map_view import MapOverlay
        from fh5_controller import BUTTON_CHOICES, ControllerHotkey

        self.tk, self.runtime, self.config = tk, runtime, config
        self.recorder = FrameRecorder(record_path)
        self.closed, self.after_id, self.save_id = False, None, None
        self.keys = Hotkeys()
        self.pad = ControllerHotkey(config.get('pad_button', 0x4000))
        self.map_pad = ControllerHotkey(config.get('map_pad_button', 0x8000))
        self.root = tk.Tk()
        self.root.title('FH5 车包坐标 · 实验接入')
        self.root.geometry('980x560')
        self.root.minsize(760, 430)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.configure(bg='#FFFFFF')
        self.visible = tk.BooleanVar(value=config['visible'])
        self.map_visible = tk.BooleanVar(value=config.get('map_visible', True))
        self.min_level = tk.StringVar(value=str(self._minimum_level()))
        self.span = tk.StringVar(value=str(int(config['range'])))
        self.alpha = tk.DoubleVar(value=config['alpha'])
        toolbar = ttk.Frame(self.root, padding=12)
        toolbar.pack(fill='x')
        self.toggle_button = ttk.Button(toolbar, command=self.toggle)
        self.toggle_button.pack(side='left')
        ttk.Button(toolbar, text='重新定位', command=self.runtime.request_rescan).pack(side='left', padx=(8, 0))
        ttk.Label(toolbar, text='直径（游戏坐标单位）').pack(side='left', padx=(18, 5))
        entry = ttk.Spinbox(toolbar, from_=200, to=24000, increment=200,
                            textvariable=self.span, width=8, command=self.changed)
        entry.pack(side='left')
        entry.bind('<Return>', lambda event: self.changed())
        entry.bind('<FocusOut>', lambda event: self.changed())
        ttk.Label(toolbar, text='透明度').pack(side='left', padx=(18, 5))
        ttk.Scale(toolbar, from_=0.2, to=1, variable=self.alpha,
                   command=lambda value: self.changed()).pack(side='left', fill='x', expand=True)
        ttk.Button(toolbar, text='退出', command=self.close).pack(side='right', padx=(12, 0))
        controller_row = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        controller_row.pack(fill='x')
        ttk.Label(controller_row, text='手柄开关雷达').pack(side='left', padx=(0, 8))
        self.pad_options = dict(BUTTON_CHOICES)
        selected_button = next((label for label, mask in BUTTON_CHOICES
                                if mask == config.get('pad_button', 0x4000)), 'X')
        self.pad_selection = tk.StringVar(value=selected_button)
        self.pad_combo = ttk.Combobox(controller_row, state='readonly', width=24,
                                     textvariable=self.pad_selection,
                                     values=[label for label, mask in BUTTON_CHOICES])
        self.pad_combo.pack(side='left')
        self.pad_combo.bind('<<ComboboxSelected>>', self.controller_changed)
        ttk.Label(controller_row, text='按一下切换显示；选择后自动保存。',
                  foreground='#687381').pack(side='left', padx=12)
        map_controller_row = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        map_controller_row.pack(fill='x')
        ttk.Label(map_controller_row, text='手柄开关地图车包').pack(side='left', padx=(0, 8))
        selected_map_button = next((label for label, mask in BUTTON_CHOICES
                                    if mask == config.get('map_pad_button', 0x8000)), 'Y')
        self.map_pad_selection = tk.StringVar(value=selected_map_button)
        self.map_pad_combo = ttk.Combobox(map_controller_row, state='readonly', width=24,
                                         textvariable=self.map_pad_selection,
                                         values=[label for label, mask in BUTTON_CHOICES])
        self.map_pad_combo.pack(side='left')
        self.map_pad_combo.bind('<<ComboboxSelected>>', self.map_controller_changed)
        self.map_toggle_button = ttk.Button(map_controller_row, command=self.toggle_map)
        self.map_toggle_button.pack(side='left', padx=12)
        ttk.Label(map_controller_row, text='雷达/地图最低等级').pack(side='left', padx=(12, 5))
        minimum = ttk.Spinbox(map_controller_row, from_=4, to=10, width=4,
                              textvariable=self.min_level, command=self.changed)
        minimum.pack(side='left')
        minimum.bind('<Return>', lambda event: self.changed())
        minimum.bind('<FocusOut>', lambda event: self.changed())
        self.status = tk.StringVar(value='准备读取…')
        ttk.Label(self.root, textvariable=self.status, padding=(12, 5),
                   wraplength=940, justify='left').pack(fill='x')
        ttk.Label(self.root, text='候选数量仅代表当前读取范围。车型与等级按当前版本车包表识别；雷达车头朝上，随车辆朝向旋转。',
                   padding=(12, 5), foreground='#687381').pack(fill='x')
        frame = ttk.Frame(self.root, padding=(12, 5, 12, 12))
        frame.pack(fill='both', expand=True)
        columns = ('level', 'car', 'x', 'y', 'z', 'distance', 'row', 'identity')
        self.table = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse')
        for name, text, width in (
            ('level', '等级', 45), ('car', '车型', 160),
            ('x', 'X', 85), ('y', 'Y', 85), ('z', 'Z', 85),
            ('distance', '平面距离', 85), ('row', '车包行号', 110),
            ('identity', '校验状态', 170)):
            self.table.heading(name, text=text)
            self.table.column(name, width=width, minwidth=40,
                              anchor='w' if name in ('car', 'identity') else 'e')
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.overlay = RadarOverlay(self.root, config)
        self.map_overlay = MapOverlay(self.root)
        self._set_visibility()
        self._set_map_visibility()
        self.table_next = 0.0
        self.runtime.start()
        self.after_id = self.root.after(0, self.tick)
        if smoke_seconds is not None:
            self.root.after(max(1, int(smoke_seconds * 1000)), self.close)

    def _set_visibility(self):
        self.overlay.set_visible(self.visible.get())
        self.toggle_button.configure(text='停止雷达（F9）' if self.visible.get() else '启动雷达（F9）')

    def _set_map_visibility(self):
        if not self.map_visible.get():
            snapshot = self.runtime.snapshot()
            if not isinstance(snapshot, dict):
                snapshot = {'rows': []}
            camera = snapshot.get('map_frame') if self.map_dpi_ready else None
            self.map_overlay.render(snapshot, camera, min_level=self._minimum_level(), enabled=False)
        self.map_toggle_button.configure(text='地图车包：已开启（Y）' if self.map_visible.get() else '地图车包：已关闭（Y）')

    def _minimum_level(self):
        value = self.config.get('min_level', 4)
        return value if type(value) is int and 4 <= value <= 10 else 4

    def toggle(self):
        self.visible.set(not self.visible.get())
        self._set_visibility()
        self.changed()

    def toggle_map(self):
        self.map_visible.set(not self.map_visible.get())
        self._set_map_visibility()
        self.changed()

    def controller_changed(self, event=None):
        mask = self.pad_options.get(self.pad_selection.get(), 0x4000)
        self.config['pad_button'] = mask
        self.pad.set_button(mask)
        self.changed()

    def map_controller_changed(self, event=None):
        mask = self.pad_options.get(self.map_pad_selection.get(), 0x8000)
        self.config['map_pad_button'] = mask
        self.map_pad.set_button(mask)
        self.changed()

    def changed(self):
        if self.closed:
            return
        try:
            span = float(self.span.get())
            if math.isfinite(span) and 200 <= span <= 24000:
                self.config['range'] = span
        except (TypeError, ValueError):
            pass
        self.config['alpha'] = min(1, max(0.2, float(self.alpha.get())))
        self.config['visible'] = self.visible.get()
        self.config['map_visible'] = self.map_visible.get()
        try:
            level = int(self.min_level.get())
            if 4 <= level <= 10:
                self.config['min_level'] = level
        except (TypeError, ValueError):
            pass
        if hasattr(self, 'overlay'):
            self.overlay.set_alpha(self.config['alpha'])
        if self.save_id is not None:
            self.root.after_cancel(self.save_id)
        self.save_id = self.root.after(300, self.save)

    def save(self):
        self.save_id = None
        try:
            fh5_config.save(self.config)
        except OSError as error:
            self.status.set(f'配置保存失败：{error}')

    def _refresh_table(self, snapshot):
        from fh5_overlay import fresh_player
        player = fresh_player(snapshot)
        self.table.delete(*self.table.get_children())
        for row in snapshot.get('rows') or []:
            try:
                xyz = [float(row[key]) for key in ('x', 'y', 'z')]
                if not all(math.isfinite(v) for v in xyz):
                    continue
                distance = math.hypot(xyz[0] - player['x'], xyz[2] - player['z']) if player else None
                level = row.get('level') or '?'
                self.table.insert('', 'end', values=(level, row.get('car') or '未知车型',
                    *(f'{v:.2f}' for v in xyz), f'{distance:.1f}' if distance is not None else '—',
                    row.get('loot_row_id', row.get('row_raw', '—')), '表数据已核实' if row.get('identity_status') == 'verified' else '车型和等级未知'))
            except (KeyError, TypeError, ValueError):
                continue

    def _tick_interval(self, snapshot):
        frame = snapshot.get('map_frame')
        if (self.map_visible.get() and self.map_dpi_ready and snapshot.get('map_state') == 'active'
                and isinstance(frame, dict) and frame.get('projection_verified') is True
                and frame.get('game_foreground') is True):
            return 16
        return 50

    def tick(self):
        if self.closed:
            return
        next_interval = 50
        try:
            if self.keys.pressed(0x23):  # END
                self.close()
                return
            keyboard_toggle = self.keys.pressed(0x78)  # F9
            controller_toggle = self.pad.pressed()
            if keyboard_toggle or controller_toggle:
                self.toggle()
            if self.map_pad.pressed():
                self.toggle_map()
            # A single snapshot supplies every field for this entire frame.
            snapshot = self.runtime.snapshot()
            if not self.map_dpi_ready:
                snapshot = dict(snapshot, map_frame=None, map_state='inactive',
                                map_reason='ui_physical_dpi_unavailable')
            minimum = self._minimum_level()
            snapshot.update(display_min_level=minimum, radar_visible=self.visible.get(),
                            map_visible=self.map_visible.get())
            self.overlay.render(snapshot, self.config['range'], min_level=minimum)
            self.map_overlay.render(snapshot, snapshot.get('map_frame'),
                                    min_level=minimum, enabled=self.map_visible.get())
            self.recorder.record(snapshot, self.overlay.last_render,
                                 map_render=self.map_overlay.last_render)
            now = time.monotonic()
            if now >= self.table_next:
                self.table_next = now + 0.5
                self._refresh_table(snapshot)
                age = snapshot.get('telemetry_age')
                age_text = f'{age:.1f}s' if isinstance(age, (int, float)) else '无'
                player = snapshot.get('player') or {}
                car_name = player.get('car') or ('车型未识别' if player else '等待本车遥测')
                progress = snapshot.get('scan_progress')
                scan_text = (f"{progress.get('attempted_mib', 0):.0f}/{progress.get('eligible_mib', 0):.0f} MiB"
                             if isinstance(progress, dict) else '尚未开始')
                self.status.set(f"{snapshot.get('state', '')} | 内存：{snapshot.get('memory_state', '等待')} | "
                    f"遥测：{snapshot.get('telemetry_state', '等待')}（{age_text}） | "
                    f"已读候选 {len(snapshot.get('rows') or [])} | PID {snapshot.get('process_pid') or '—'} | "
                    f"包版本 {snapshot.get('game_version') or '未知'}\n"
                    f"本车：{car_name} | 定位扫描：{scan_text} | "
                    f"地图：{snapshot.get('map_state', '等待')} / {snapshot.get('map_reason', '')}")
            next_interval = self._tick_interval(snapshot)
        except Exception as error:
            # A bad UI frame clears relative points and always reschedules.
            try:
                self.overlay.render({'rows': [], 'player': None}, self.config['range'],
                                    min_level=self._minimum_level())
                self.map_overlay.render({'rows': [], 'player': None}, None,
                                        min_level=self._minimum_level())
            except Exception:
                pass
            self.status.set(f'界面刷新失败：{type(error).__name__}: {error}')
        if not self.closed:
            self.after_id = self.root.after(next_interval, self.tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.recorder.close()
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
        if self.save_id is not None:
            self.root.after_cancel(self.save_id)
            self.save_id = None
            self.save()
        try:
            self.runtime.close()
        finally:
            try:
                try:
                    self.overlay.close()
                finally:
                    try:
                        self.map_overlay.close()
                    finally:
                        self.root.destroy()
            finally:
                self.map_dpi_ready = False
                guard = getattr(self, 'ui_dpi', None)
                if guard is not None:
                    self.dpi_restore_complete = guard.close()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', help='离线回放 live_sync JSON；不连接游戏或UDP')
    parser.add_argument('--headless', action='store_true', help='不创建窗口，只测试数据提供器')
    parser.add_argument('--seconds', type=float, default=2, help='headless 持续秒数')
    parser.add_argument('--smoke-seconds', type=float, help='界面测试后自动退出')
    parser.add_argument('--port', type=int, help='本机 Data Out UDP 端口（默认53005）')
    parser.add_argument('--seed-scope', help='有限扫描范围配置，交给数据提供器验证')
    parser.add_argument('--mapping', help='经核实的 FH5 车型/等级映射 JSON')
    parser.add_argument('--record', help='可选本地显示帧 JSONL 日志路径（默认不记录）')
    parser.add_argument('--map-preview', action='store_true', help='离线地图预览；不连接游戏或UDP')
    parser.add_argument('--map-profile', type=Path,
                        help='FH5 地图对齐配置；默认使用随程序配置，仅通过验证时显示地图覆盖')
    args = parser.parse_args(argv)
    if not 0 < args.seconds <= 60:
        parser.error('--seconds 必须在 0 到 60 秒之间')
    if args.smoke_seconds is not None and not 0 < args.smoke_seconds <= 60:
        parser.error('--smoke-seconds 必须在 0 到 60 秒之间')
    if args.port is not None and not 49152 <= args.port <= 65535:
        parser.error('--port 必须在 49152 到 65535 之间')
    if args.map_preview:
        from fh5_map_view import run_preview
        from fh5_paths import RESOURCE_DIR
        report = args.replay or str(RESOURCE_DIR / 'examples' / 'demo.json')
        return run_preview(report, headless=args.headless, seconds=args.seconds,
                           smoke_seconds=args.smoke_seconds, record_path=args.record)
    config = fh5_config.load()
    runtime = _runtime(args, config)
    if args.headless:
        return headless(runtime, args.seconds, args.record)
    try:
        Application(runtime, config, args.smoke_seconds, record_path=args.record).run()
    finally:
        runtime.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
