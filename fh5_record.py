"""Optional, bounded-rate local display diagnostics; never reads the game."""
from __future__ import annotations

import datetime
import json
import math
import time

from fh5_map_model import MapFrame, SUPPORTED_PACKAGE_VERSIONS


ROW_KEYS = ('x', 'y', 'z', 'first_string_address', 'F', 'fingerprint',
            'row_raw', 'loot_row_id', 'row_padding_u32', 'fingerprint_version',
            'value', 'car_id', 'level', 'car', 'identity_status')
PLAYER_KEYS = ('x', 'y', 'z', 'yaw', 'car', 'car_ordinal')
POINT_KEYS = ('F', 'fingerprint', 'pixel_x', 'pixel_y', 'distance', 'level',
              'car_id', 'car', 'display_label', 'icon_kind', 'icon_level')
RENDER_KEYS = ('kind', 'visible', 'visible_item_count', 'span', 'radius', 'centre',
               'orientation', 'heading_yaw_rad', 'preview', 'projection_verified', 'reason',
               'min_level', 'original_icon_count', 'icon_error', 'enabled', 'status_text')
MARKER_KEYS = ('F', 'fingerprint', 'level', 'grade', 'name', 'car_id', 'label',
               'identity_status', 'replay', 'verified')
PROGRESS_KEYS = ('attempted_mib', 'region_index', 'region_count', 'eligible_mib',
                 'failed_chunks', 'complete_selected_policy', 'complete_world_coverage',
                 'scan_address', 'pending_hit_offset', 'pending_stride_index')
ERROR_KEYS = ('operation', 'winerror', 'detail', 'address', 'requested', 'actual')
MEMORY_TIMING_KEYS = ('session_started_monotonic', 'first_rows_monotonic',
                     'first_rows_elapsed_seconds', 'discovery_started_monotonic',
                     'discovery_first_rows_monotonic', 'discovery_elapsed_seconds',
                     'iteration_elapsed_seconds', 'known_slots')
RECOVERY_TIMING_KEYS = ('mode', 'reason', 'priority_range_count', 'priority_bytes',
                        'deferred_region_count', 'scope_range_count',
                        'scope_bytes', 'metadata_refreshes', 'metadata_age_seconds',
                        'metadata_refresh_error', 'metadata_refresh_failures',
                        'full_scan_generation', 'empty_seconds', 'burst_remaining_seconds')
REFRESH_TIMING_KEYS = ('limited', 'sample_monotonic', 'elapsed_seconds',
                      'known_slots', 'visited_slots', 'read_calls', 'read_bytes', 'read_gaps')
MAP_TIMING_KEYS = ('basis', 'session_started_monotonic', 'sample_monotonic',
                  'session_elapsed_seconds', 'first_camera_ready_monotonic',
                  'first_authorized_foreground_frame_monotonic',
                  'last_authorized_foreground_frame_monotonic')
MAP_TRACKER_KEYS = ('reason', 'candidate_count', 'locator_complete',
                    'locator_read_bytes', 'camera_address')
MAP_TRACKER_TIMING_KEYS = ('session_search_started_monotonic',
                           'current_search_started_monotonic', 'first_structure_monotonic',
                           'first_active_camera_monotonic', 'last_active_camera_monotonic',
                           'search_elapsed_seconds', 'target_seconds')
MAP_LOCATOR_KEYS = ('metadata_queries', 'hot_passes', 'hot_read_bytes',
                    'hot_pass_interval_count', 'hot_pass_interval_index', 'hot_pass_remaining_bytes',
                    'allocation_anchor_probe_reads', 'allocation_anchor_probe_hits',
                    'allocation_anchor_probes_remaining', 'scan_address', 'hot_scan_address',
                    'read_failures', 'last_read_failure_address')
MAX_ITEMS = 1024
MAX_TEXT = 2048
_SKIP = object()


def _scalar(value):
    """Copy bounded JSON scalars without repr/str of arbitrary objects."""
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        return value[:MAX_TEXT]
    if type(value) is int:
        return value if -(1 << 63) <= value <= (1 << 64) - 1 else _SKIP
    if type(value) is float and math.isfinite(value):
        return value
    return _SKIP


def _fields(value, keys):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in keys:
        if key in value:
            item = _scalar(value[key])
            if item is not _SKIP:
                result[key] = item
    return result


def _items(value, keys):
    if not isinstance(value, (list, tuple)):
        return []
    return [_fields(item, keys) for item in value[:MAX_ITEMS] if isinstance(item, dict)]


def _address_items(value, limit):
    # Exact built-in containers avoid invoking arbitrary iteration/getitem
    # methods. Inspect only the bounded prefix, even if most entries are bad.
    if type(value) not in (list, tuple):
        return []
    return [item for item in value[:limit]
            if type(item) is int and 0 <= item <= (1 << 64) - 1]


def _map_tracker(map_status, *, replay=False):
    """Diagnostic scalars only; this record never authorizes projection."""
    if type(map_status) is not dict:
        return None
    tracker = map_status.get('tracker_status')
    if type(tracker) is not dict:
        return None
    result = _fields(tracker, MAP_TRACKER_KEYS)
    timing = tracker.get('timing')
    if type(timing) is dict:
        result['timing'] = _fields(timing, MAP_TRACKER_TIMING_KEYS)
    locator = tracker.get('locator_diagnostics')
    if type(locator) is dict:
        diagnostics = _fields(locator, MAP_LOCATOR_KEYS)
        for key, limit in (('hot_region_bases', 16), ('hot_search_anchors', 4)):
            if key in locator:
                diagnostics[key] = _address_items(locator[key], limit)
        ranges = locator.get('hot_ranges')
        if type(ranges) in (list, tuple):
            diagnostics['hot_ranges'] = [list(pair) for pair in ranges[:8]
                if type(pair) in (list, tuple) and len(pair) == 2
                and all(type(item) is int for item in pair)
                and 0 <= pair[0] < pair[1] <= (1 << 64) - 1]
        result['locator_diagnostics'] = diagnostics
    if replay:
        result['replay'] = True
    return result


def _map_frame(snapshot):
    raw = snapshot.get('map_frame')
    try:
        frame = MapFrame.from_mapping(raw)
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    if (frame.session_key != snapshot.get('session_key')
            or frame.package_version != snapshot.get('game_version')):
        return None
    replay = snapshot.get('replay') is True or frame.source != 'engine_camera'
    source = 'recorded' if replay and frame.source == 'engine_camera' else frame.source
    verified = bool(not replay and frame.projection_verified
                    and frame.package_version in SUPPORTED_PACKAGE_VERSIONS)
    result = {'map_open': frame.map_open, 'matrix': list(frame.matrix),
              'viewport': {key: float(getattr(frame.viewport, key))
                           for key in ('x', 'y', 'width', 'height')},
              'session_key': frame.session_key[:MAX_TEXT],
              'package_version': frame.package_version[:MAX_TEXT],
              'sample_monotonic': frame.sample_monotonic, 'source': source,
              'projection_verified': verified,
              'game_foreground': bool(not replay and frame.game_foreground),
              'replay': replay}
    result.update(_fields(raw, ('activity_sample_monotonic', 'camera_address')))
    return result


def _render(value, *, replay=False, map_frame=None, is_map=False):
    if not isinstance(value, dict):
        return None
    result = _fields(value, RENDER_KEYS)
    if 'points' in value:
        result['points'] = _items(value.get('points'), POINT_KEYS)
    if 'markers' in value:
        markers = []
        source = value.get('markers')
        for raw in source[:MAX_ITEMS] if isinstance(source, (list, tuple)) else ():
            if not isinstance(raw, dict):
                continue
            marker = _fields(raw, MARKER_KEYS)
            pixel = raw.get('pixel')
            if (isinstance(pixel, (list, tuple)) and len(pixel) == 2
                    and all(type(v) in (int, float) and _scalar(v) is not _SKIP for v in pixel)):
                marker['pixel'] = list(pixel)
            if replay or value.get('preview') is True:
                marker.update(replay=True, verified=False)
            markers.append(marker)
        result['markers'] = markers
    if replay or value.get('preview') is True:
        result.update(replay=True, preview=True, projection_verified=False)
    if is_map:
        result['pixel_space'] = 'overlay_client_pixels'
        result['projection_verified'] = bool(map_frame and map_frame['projection_verified']
                                             and value.get('projection_verified') is True and not replay)
        # These are actual renderer outputs, including unexpected residual dots.
        # Never invent a clear merely because source camera data is absent.
        result['frame_available'] = map_frame is not None
    return result


def display_record(snapshot, render=None, *, map_render=None, utc=None, monotonic=None):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    record = _fields(snapshot,
              ('session_key', 'process_pid', 'game_version', 'state', 'memory_state',
               'telemetry_state', 'telemetry_age', 'scan_progress', 'read_gaps',
               'refresh_limited', 'last_error'))
    if isinstance(snapshot.get('scan_progress'), dict):
        record['scan_progress'] = _fields(snapshot['scan_progress'], PROGRESS_KEYS)
    if isinstance(snapshot.get('last_error'), dict):
        record['last_error'] = _fields(snapshot['last_error'], ERROR_KEYS)
    if isinstance(snapshot.get('memory_timing'), dict):
        timing = snapshot['memory_timing']
        record['memory_timing'] = _fields(timing, MEMORY_TIMING_KEYS)
        record['memory_timing']['refreshes'] = _items(timing.get('refreshes'), REFRESH_TIMING_KEYS)[:8]
        if type(timing.get('recovery')) is dict:
            record['memory_timing']['recovery'] = _fields(timing['recovery'], RECOVERY_TIMING_KEYS)
    if isinstance(snapshot.get('map_timing'), dict):
        record['map_timing'] = _fields(snapshot['map_timing'], MAP_TIMING_KEYS)
    record['utc'] = (utc[:MAX_TEXT] if isinstance(utc, str) and utc else
                     datetime.datetime.now(datetime.timezone.utc).isoformat())
    stamp = time.monotonic() if monotonic is None else monotonic
    record['monotonic'] = (_scalar(stamp) if type(stamp) in (int, float)
                           and _scalar(stamp) is not _SKIP else None)
    player = snapshot.get('player')
    record['player'] = _fields(player, PLAYER_KEYS) if isinstance(player, dict) else None
    record['rows'] = _items(snapshot.get('rows'), ROW_KEYS)
    replay = snapshot.get('replay') is True
    record['replay'] = replay
    map_status = snapshot.get('map_status')
    record.update(_fields(snapshot, ('map_state', 'map_reason', 'display_min_level',
                                    'radar_visible', 'map_visible')))
    if 'map_reason' not in record and type(map_status) is dict:
        reason = _scalar(map_status.get('reason'))
        if reason is not _SKIP:
            record['map_reason'] = reason
    tracker = _map_tracker(map_status, replay=replay)
    if tracker is not None:
        record['map_tracker'] = tracker
    record['map_frame'] = _map_frame(snapshot)
    map_replay = replay or bool(record['map_frame'] and record['map_frame']['replay'])
    record['map_render'] = _render(map_render, replay=map_replay,
                                   map_frame=record['map_frame'], is_map=True)
    if render is not None:
        record['render'] = _render(render, replay=replay)
    return record


class FrameRecorder:
    def __init__(self, path=None, interval=0.5):
        self.path, self.interval = path, interval
        self.file, self.error, self.closed = None, None, False
        self.next_write = 0.0

    def record(self, snapshot, render=None, *, map_render=None):
        if not self.path or self.closed or self.error is not None:
            return False
        now = time.monotonic()
        if now < self.next_write:
            return False
        self.next_write = now + self.interval
        try:
            if self.file is None:
                self.file = open(self.path, 'a', encoding='utf-8')
            self.file.write(json.dumps(display_record(snapshot, render, map_render=map_render, monotonic=now),
                                       ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
            self.file.flush()
            return True
        except Exception as error:
            self.error = f'{type(error).__name__}: {error}'
            self.close()
            return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.file is not None:
            try:
                self.file.flush()
            except Exception:
                pass
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None
