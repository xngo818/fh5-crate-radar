"""Offline camera search benchmark and timestamped real-marker acceptance.

Never opens a game process, captures a screen, creates UI, or binds UDP.
``--synthetic`` uses sparse fake memory with a 13 GiB address layout; its
results are regression evidence, not a real-game performance guarantee.
``--record`` requires an explicit monotonic timestamp of the user's timed action
(for example, a physical timing key pressed immediately before opening the map).
Success requires fresh production camera AND an actual displayed level>=4
marker matching a current verified row; an empty authorized map is not success.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fh5_map_camera import (ALLOCATION_ANCHOR_OFFSETS, CameraTracker,
                            MAX_STEP_BYTES, READ_BEFORE, READ_SIZE,
                            SUPPORTED_VERSION)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluate_record(records, start_monotonic, *, seconds=20.0, session_key=None):
    if not _number(start_monotonic) or start_monotonic < 0:
        raise ValueError('explicit nonnegative map-ready monotonic time required')
    if not _number(seconds) or not 0 < seconds <= 20:
        raise ValueError('acceptance window must be 0 < seconds <= 20')
    deadline, observed_until = start_monotonic+seconds, None
    success, matched_level = None, None
    for row in records:
        if not isinstance(row, dict):
            continue
        now = row.get('monotonic')
        if not _number(now) or now < start_monotonic:
            continue
        observed_until = now if observed_until is None else max(observed_until, now)
        if now > deadline or row.get('replay') is not False:
            continue
        session = row.get('session_key')
        if not isinstance(session, str) or not session or (session_key is not None and session != session_key):
            continue
        frame, render = row.get('map_frame'), row.get('map_render')
        if not isinstance(frame, dict) or not isinstance(render, dict):
            continue
        if (frame.get('session_key') != session or frame.get('package_version') != SUPPORTED_VERSION
                or frame.get('projection_verified') is not True or frame.get('game_foreground') is not True
                or frame.get('map_open') is not True or frame.get('source') != 'engine_camera'
                or render.get('preview') is not False or render.get('projection_verified') is not True):
            continue
        if not all(_number(frame.get(stamp)) and 0 <= now-frame[stamp] < .25
                   for stamp in ('sample_monotonic', 'activity_sample_monotonic')):
            continue
        current = {(v.get('F', v.get('first_string_address')), v.get('fingerprint')): v for v in row.get('rows', [])
                   if isinstance(v, dict) and v.get('identity_status') == 'verified'
                   and type(v.get('F', v.get('first_string_address'))) is int
                   and isinstance(v.get('fingerprint'), str)
                   and type(v.get('level')) is int and 4 <= v['level'] <= 10}
        for marker in render.get('points', []):
            if (not isinstance(marker, dict) or type(marker.get('F')) is not int
                    or not isinstance(marker.get('fingerprint'), str)):
                continue
            actual = current.get((marker.get('F'), marker.get('fingerprint')))
            if (actual is not None and marker.get('level') == actual['level']
                    and _number(marker.get('pixel_x')) and _number(marker.get('pixel_y'))):
                success, matched_level = now, actual['level']
                break
        if success is not None:
            break
    return {'result': ('passed' if success is not None else 'failed' if observed_until is not None
                       and observed_until >= deadline else 'incomplete'),
            'basis': 'explicit user timing action to first recorded actual level>=4 marker',
            'map_ready_monotonic': start_monotonic, 'deadline_monotonic': deadline,
            'first_marker_monotonic': success,
            'elapsed_seconds': None if success is None else success-start_monotonic,
            'matched_level': matched_level, 'observed_until_monotonic': observed_until,
            'real_game_acceptance': True, 'maximum_seconds': seconds}


def _camera(address, *, clock, inactive=False):
    raw, index = bytearray(READ_SIZE), READ_BEFORE
    eye, x, z = (1000, 0, 0) if inactive else (2500, -7054.69, 1780.95)
    struct.pack_into('<11f', raw, index, x, eye-10, z, 1, x, eye-10000, z, 1,
                     2560, 1440, 2560/1440)
    a = 10000/9990
    matrix = [.5625, 0, 0, 0, 0, 0, -a, -1, 0, 1, 0, 0, -x*.5625, -z, eye*a-10*a, eye]
    for offset in (-672, -1712):
        struct.pack_into('<16f', raw, index+offset, *matrix)
    for offset in (0xB0, 0xC4, 0xD8):
        struct.pack_into('<f', raw, index+offset, 0 if inactive else .002)
    struct.pack_into('<f', raw, index+0x194, clock)
    return address-READ_BEFORE, bytes(raw)


class SparseProcess:
    min_address, max_address = 0x10000, 0x7fffffffffff

    def __init__(self):
        self.identity = {'session_key': 'synthetic-only:current-creation:fake.exe'}
        self.pool = 0x900000000
        self.anchor = self.pool+ALLOCATION_ANCHOR_OFFSETS[0]
        self.camera = self.anchor+3*1024*1024
        self.allocations = [dict(base=0x100000000+i*0x80000000, size=1024**3,
                                 state=0x1000, type=0x20000, protect=4) for i in range(13)]
        self.allocations.append(dict(base=self.pool, size=64*1024**2,
                                     state=0x1000, type=0x20000, protect=4))
        self.clock, self.visible, self.read_bytes, self.step_bytes = 10.0, True, 0, 0

    def regions(self, **kwargs):
        return self.allocations

    def read(self, address, size):
        self.read_bytes += size
        self.step_bytes += size
        raw = bytearray(size)
        patches = [_camera(self.anchor, clock=0, inactive=True)]
        if self.visible:
            patches.append(_camera(self.camera, clock=self.clock))
        for start, data in patches:
            low, high = max(address, start), min(address+size, start+len(data))
            if low < high:
                raw[low-address:high-address] = data[low-start:high-start]
        return bytes(raw)


def synthetic_benchmark():
    process = SparseProcess()
    tracker = CameraTracker(process.identity['session_key'])
    started, maximum, sample = time.perf_counter(), 0, None
    for index in range(400):
        process.step_bytes = 0
        process.clock += .01
        sample = tracker.step(process, now=100+index*.005)
        maximum = max(maximum, process.step_bytes)
        if maximum > MAX_STEP_BYTES:
            raise AssertionError('payload step exceeded 2 MiB')
        if sample is not None:
            break
    return {'result': 'passed' if sample is not None else 'failed',
            'kind': 'synthetic sparse fake-memory regression; not a measured game latency',
            'real_game_acceptance': False,
            'eligible_layout_bytes': sum(r['size'] for r in process.allocations),
            'payload_read_bytes': process.read_bytes, 'maximum_step_payload_bytes': maximum,
            'steps': index+1, 'modeled_discovery_wait_seconds': index*.005,
            'host_execution_seconds': time.perf_counter()-started,
            'camera_clock_proved': sample is not None,
            'projection_verified': False,
            'tracker_status': tracker.status}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--synthetic', action='store_true')
    group.add_argument('--record', type=Path)
    parser.add_argument('--start-monotonic', type=float)
    parser.add_argument('--session-key')
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.synthetic:
        result = synthetic_benchmark()
    else:
        if args.start_monotonic is None:
            parser.error('--record requires --start-monotonic from the user-observed map-ready action')
        with args.record.open(encoding='utf-8-sig') as stream:
            result = evaluate_record((json.loads(line) for line in stream if line.strip()),
                                     args.start_monotonic, seconds=args.seconds, session_key=args.session_key)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text)
    return 0 if result['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
