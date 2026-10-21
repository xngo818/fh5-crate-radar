"""Independent, evidence-gated map sampler for Runtime.

Construction/import does not query Windows, read files, open a process, create
a thread, or bind a socket. Only start() runs the sampler. Its process handle is
owned exclusively by that worker and closed when the session changes or exits.
CameraTracker bounds every discovery/sample step to 2 MiB; the service never
performs an additional heap scan. No camera addresses are persisted here.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import threading
import time

from fh5_map_model import MapFrame
from fh5_window_geometry import WindowGeometry
from win_readonly import Process

SUPPORTED_VERSION = '3.688.109.0'
FRAME_TTL = .25
PERIOD = .05
ACTIVE_PERIOD = .016
DISCOVERY_PERIOD = .005
DISCOVERY_BURST_SECONDS = 20.0
SCOPE_MAX_BYTES = 65536
SCOPE_MAX_REGIONS = 16
SCOPE_WRITE_PERIOD = 1.0
PROFILE_SCHEMA_VERSION = 1
PROFILE_MODE = 'client_equals_render'
COORDINATE_SPACE = 'physical_screen_pixels'


def _finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _fresh(stamp, now):
    return bool(_finite(stamp) and stamp >= 0 and 0 <= now - stamp < FRAME_TTL)


def _evidence(value):
    return bool((isinstance(value, str) and value.strip())
                or (isinstance(value, list) and value
                    and all(isinstance(v, str) and v.strip() for v in value)))


def _region_base(value):
    return bool(type(value) is int and 0x10000 <= value < 0x7fffffffffff
                and value % 4096 == 0)


def _scope_regions(value):
    if (not isinstance(value, list) or len(value) > SCOPE_MAX_REGIONS
            or not all(_region_base(base) for base in value)):
        return None
    return list(dict.fromkeys(value))


def authorize_alignment_profile(profile, version, candidate, geometry):
    """Pure authorization for an evidenced, exact physical display mode.

    Schema 1 deliberately supports only measured client==render 1:1 geometry.
    Screen origin may move, but dimensions, DPI and coordinate convention must
    match the evidence. No scale factor, pixel correction or guessed bars are
    authorized by this profile. Other modes require separate measured support.
    """
    if not isinstance(profile, dict):
        return False, 'alignment_profile_missing_or_invalid'
    if type(profile.get('schema_version')) is not int or profile['schema_version'] != PROFILE_SCHEMA_VERSION:
        return False, 'alignment_profile_schema_unsupported'
    if profile.get('alignment_verified') is not True:
        return False, 'alignment_profile_not_verified'
    if profile.get('package_version') != version or version != SUPPORTED_VERSION:
        return False, 'alignment_profile_package_mismatch'
    if not _evidence(profile.get('evidence')):
        return False, 'alignment_profile_evidence_missing'
    if profile.get('mode') != PROFILE_MODE:
        return False, 'alignment_profile_mode_unsupported'
    if profile.get('coordinate_space') != COORDINATE_SPACE:
        return False, 'alignment_profile_coordinate_space_unsupported'
    dimensions = ('render_width', 'render_height', 'client_width', 'client_height')
    if any(type(profile.get(k)) is not int or not 1 <= profile[k] <= 32768 for k in dimensions):
        return False, 'alignment_profile_dimensions_invalid'
    if (profile['render_width'] != profile['client_width']
            or profile['render_height'] != profile['client_height']):
        return False, 'alignment_profile_mode_dimensions_inconsistent'
    if type(profile.get('dpi')) is not int or not 48 <= profile['dpi'] <= 960:
        return False, 'alignment_profile_dpi_invalid'
    if not isinstance(candidate, dict) or not isinstance(geometry, dict):
        return False, 'alignment_geometry_unavailable'
    if geometry.get('coordinate_space') != COORDINATE_SPACE:
        return False, 'alignment_geometry_coordinate_space_mismatch'
    if type(geometry.get('dpi')) is not int or geometry['dpi'] != profile['dpi']:
        return False, 'alignment_profile_dpi_mismatch'
    render = (candidate.get('render_width'), candidate.get('render_height'))
    if (not all(_finite(value) for value in render)
            or render != (profile['render_width'], profile['render_height'])):
        return False, 'alignment_profile_render_size_mismatch'
    if (geometry.get('render_width'), geometry.get('render_height')) != render:
        return False, 'alignment_geometry_render_size_mismatch'
    client, viewport = geometry.get('client_rect'), geometry.get('render_viewport')
    if not isinstance(client, dict) or not isinstance(viewport, dict):
        return False, 'alignment_geometry_client_or_viewport_unavailable'
    keys = ('x', 'y', 'width', 'height')
    if (not all(_finite(rect.get(k)) for rect in (client, viewport) for k in keys)
            or max(abs(client['x']), abs(client['y'])) > 65536):
        return False, 'alignment_geometry_rect_invalid'
    if (client['width'], client['height']) != (profile['client_width'], profile['client_height']):
        return False, 'alignment_profile_client_size_mismatch'
    if any(abs(client[k]-viewport[k]) > .01 for k in keys):
        return False, 'alignment_profile_viewport_not_client_1to1'
    return True, 'alignment_profile_mode_verified'


class MapService:
    def __init__(self, snapshot_getter, process_factory=Process,
                 geometry_factory=WindowGeometry, state_dir=None, profile_path=None):
        if not callable(snapshot_getter) or not callable(process_factory) or not callable(geometry_factory):
            raise TypeError('snapshot getter and factories must be callable')
        self._get_snapshot = snapshot_getter
        self._process_factory, self._geometry_factory = process_factory, geometry_factory
        # Disk I/O is deferred until a worker has verified the process identity.
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.profile_path = Path(profile_path) if profile_path is not None else None
        self._clock = time.monotonic
        self._lock, self._lifecycle = threading.RLock(), threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._process = self._tracker = self._geometry = None
        self._session = self._pid = None
        self._hot_regions = []
        self._saved_regions = None
        self._scope_last_attempt = None
        self._timing = {}
        self._state = {'map_state': 'inactive', 'reason': 'not_started',
                       'map_frame': None, 'session_key': None}

    def _new_tracker(self, session_key):
        from fh5_map_camera import CameraTracker
        return CameraTracker(session_key, package_version=SUPPORTED_VERSION,
                             hot_regions=tuple(self._hot_regions))

    def _load_hot_regions(self, source):
        """Same-process-instance region bases only; never valid camera pointers."""
        if self.state_dir is None:
            return []
        try:
            path = self.state_dir / 'map_camera_scope.json'
            size = path.stat().st_size
            if not 2 <= size <= SCOPE_MAX_BYTES:
                return []
            with path.open('rb') as file:
                raw = file.read(SCOPE_MAX_BYTES)
            if len(raw) != size or path.stat().st_size != size:
                return []
            data = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(data, dict):
                return []
            schema = data.get('schema_version', 1)
            if (type(schema) is not int or schema != 1
                    or data.get('session_key') != source['session_key']
                    or data.get('package_version') != source['game_version']
                    or data.get('package_version') != SUPPORTED_VERSION):
                return []
            regions = _scope_regions(data.get('hot_regions'))
            return regions if regions is not None else []
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
            return []

    def _learn_hot_regions(self, candidate, now):
        """Persist bounded search-order hints only after an active exact sample."""
        if self.state_dir is None or self._session is None:
            return False
        if (candidate.get('session_key') != self._session
                or candidate.get('package_version') != SUPPORTED_VERSION
                or not _fresh(candidate.get('sample_monotonic'), now)
                or not _fresh(candidate.get('activity_sample_monotonic'), now)):
            return False
        matrix = candidate.get('flattened_matrix')
        camera = candidate.get('camera_address')
        if (not isinstance(matrix, (list, tuple)) or len(matrix) != 16
                or not all(_finite(value) for value in matrix)
                or type(camera) is not int or not 0x10000 < camera < 0x7fffffffffff):
            return False
        locator = getattr(self._tracker, 'locator', None)
        raw_regions = getattr(locator, 'hot_regions', None)
        if not isinstance(raw_regions, (set, frozenset, list, tuple)):
            return False
        regions = {base for base in raw_regions if _region_base(base)}
        if not regions:
            return False
        # Prefer the active camera's already-enumerated containing region.
        # No additional region enumeration or payload read is performed here.
        active = []
        metadata = getattr(locator, 'regions', None)
        if isinstance(metadata, (list, tuple)):
            for region in metadata:
                if not isinstance(region, dict):
                    continue
                base, size = region.get('base'), region.get('size')
                if (type(base) is int and base in regions and type(size) is int and size > 0
                        and base <= camera < base + size):
                    active.append(base)
        new = sorted(regions - set(self._hot_regions))
        ordering = active + new + self._hot_regions + sorted(regions)
        selected = list(dict.fromkeys(base for base in ordering if base in regions))[:SCOPE_MAX_REGIONS]
        self._hot_regions = selected
        if tuple(selected) == self._saved_regions:
            return False
        if (self._scope_last_attempt is not None
                and now - self._scope_last_attempt < SCOPE_WRITE_PERIOD):
            return False
        self._scope_last_attempt = now
        temporary = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            path = self.state_dir / 'map_camera_scope.json'
            temporary = path.with_name(f'.map_camera_scope.{os.getpid()}.{id(self)}.tmp')
            data = {'schema_version': 1, 'session_key': self._session,
                    'package_version': SUPPORTED_VERSION, 'hot_regions': selected,
                    'note': 'Search-order hints only; exact camera and activity revalidation required.'}
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
            self._saved_regions = tuple(selected)
            return True
        except (OSError, ValueError, TypeError):
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _source(self):
        source = self._get_snapshot()
        if not isinstance(source, dict):
            return None, 'source_snapshot_invalid'
        session, pid, version = (source.get(k) for k in ('session_key', 'process_pid', 'game_version'))
        if not isinstance(session, str) or not session.strip():
            return None, 'waiting_for_process_session'
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            return None, 'source_pid_invalid'
        if version != SUPPORTED_VERSION:
            return None, 'unsupported_package_version'
        return {'session_key': session, 'process_pid': pid, 'game_version': version}, None

    def _publish(self, state, reason, frame=None, **fields):
        with self._lock:
            self._state = {'map_state': state, 'reason': str(reason),
                           'map_frame': deepcopy(frame), 'session_key': self._session,
                           'map_timing': deepcopy(self._timing),
                           **deepcopy(fields)}

    def _drop_process(self):
        process, self._process = self._process, None
        self._tracker = self._geometry = None
        self._session = self._pid = None
        self._hot_regions = []
        self._saved_regions = None
        self._scope_last_attempt = None
        self._timing = {}
        if process is not None:
            try:
                process.close()
            except Exception:
                # Frame is already invalidated and the detached object is never
                # used again. A failed close is not an authorization to reopen.
                pass

    def _identity_matches(self, source):
        identity = self._process.identity
        return bool(isinstance(identity, dict)
                    and identity.get('session_key') == source['session_key']
                    and identity.get('pid') == source['process_pid']
                    and self._process.is_alive())

    def _authorized(self, version, candidate, geometry):
        if self.profile_path is None:
            return False, 'alignment_profile_not_configured'
        try:
            if self.profile_path.stat().st_size > 65536:
                return False, 'alignment_profile_too_large'
            profile = json.loads(self.profile_path.read_text(encoding='utf-8-sig'))
            return authorize_alignment_profile(profile, version, candidate, geometry)
        except (OSError, ValueError, TypeError, OverflowError):
            return False, 'alignment_profile_unreadable'

    def _cycle(self, now=None):
        """One bounded step; private entry also permits deterministic fake tests."""
        if self._stop.is_set() or self._closed:
            return
        instant = self._clock() if now is None else now
        if not _finite(instant) or instant < 0:
            self._publish('inactive', 'invalid_sample_time')
            return
        source, reason = self._source()
        if source is None:
            self._publish('inactive', reason)
            self._drop_process()
            return
        key, pid = source['session_key'], source['process_pid']
        if self._session != key or self._pid != pid or self._process is None:
            self._publish('inactive', 'process_session_changed')
            self._drop_process()
            process = self._process_factory(pid)
            self._process = process
            if not self._identity_matches(source):
                self._publish('inactive', 'opened_process_identity_mismatch')
                self._drop_process()
                return
            self._session, self._pid = key, pid
            self._timing = {'basis': 'verified process-session sampler start; this is not a measured map-open time',
                            'session_started_monotonic': instant,
                            'first_camera_ready_monotonic': None,
                            'first_authorized_foreground_frame_monotonic': None,
                            'last_authorized_foreground_frame_monotonic': None,
                            'target_seconds': DISCOVERY_BURST_SECONDS,
                            'actual_marker_visibility_measured': False}
            self._hot_regions = self._load_hot_regions(source)
            self._saved_regions = tuple(self._hot_regions)
            self._tracker = self._new_tracker(key)
            self._geometry = self._geometry_factory(pid)
        if not self._identity_matches(source):
            self._publish('inactive', 'process_session_lost')
            self._drop_process()
            return
        candidate = self._tracker.step(self._process, now=instant)
        diagnostics = deepcopy(getattr(self._tracker, 'status', {}))
        self._timing['sample_monotonic'] = instant
        self._timing['session_elapsed_seconds'] = max(0.0, instant-self._timing['session_started_monotonic'])
        # The provider may switch sessions while discovery is in progress.
        again, reason = self._source()
        if again != source or not self._identity_matches(source):
            self._publish('inactive', reason or 'source_changed_during_sample')
            self._drop_process()
            return
        if candidate is None:
            self._publish('seeking', getattr(self._tracker, 'reason', 'camera_unavailable'),
                          tracker_status=diagnostics)
            return
        if (not isinstance(candidate, dict) or candidate.get('session_key') != key
                or candidate.get('package_version') != SUPPORTED_VERSION):
            self._publish('inactive', 'camera_candidate_scope_mismatch', tracker_status=diagnostics)
            return
        if not (_fresh(candidate.get('sample_monotonic'), instant)
                and _fresh(candidate.get('activity_sample_monotonic'), instant)):
            self._publish('inactive', 'camera_sample_or_activity_stale', tracker_status=diagnostics)
            return
        self._learn_hot_regions(candidate, instant)
        if self._timing['first_camera_ready_monotonic'] is None:
            self._timing['first_camera_ready_monotonic'] = instant
        geometry = self._geometry.sample(candidate.get('render_width'), candidate.get('render_height'))
        if not isinstance(geometry, dict) or type(geometry.get('game_foreground')) is not bool:
            self._publish('inactive', 'window_geometry_unavailable', tracker_status=diagnostics)
            return
        finished = self._clock() if now is None else instant
        again, reason = self._source()
        if again != source or not self._identity_matches(source):
            self._publish('inactive', reason or 'source_changed_during_geometry')
            self._drop_process()
            return
        if not (_fresh(candidate.get('sample_monotonic'), finished)
                and _fresh(candidate.get('activity_sample_monotonic'), finished)):
            self._publish('inactive', 'camera_sample_or_activity_stale', tracker_status=diagnostics)
            return
        if not _fresh(geometry.get('sample_monotonic'), finished):
            self._publish('inactive', 'window_geometry_stale', tracker_status=diagnostics)
            return
        authorized, authorization_reason = self._authorized(source['game_version'], candidate, geometry)
        frame = {'map_open': True, 'matrix': candidate.get('flattened_matrix'),
                 'viewport': geometry.get('render_viewport'), 'session_key': key,
                 'package_version': SUPPORTED_VERSION,
                 'sample_monotonic': candidate['sample_monotonic'],
                 'source': 'engine_camera', 'projection_verified': authorized,
                 'game_foreground': geometry['game_foreground'],
                 'activity_sample_monotonic': candidate['activity_sample_monotonic'],
                 'camera_address': candidate.get('camera_address')}
        try:
            MapFrame.from_mapping(frame)
        except (ValueError, TypeError, KeyError, OverflowError):
            self._publish('inactive', 'invalid_map_frame', tracker_status=diagnostics)
            return
        state, reason = ('active', 'map_camera_ready') if authorized else ('calibrating', authorization_reason)
        if not frame['game_foreground']:
            state, reason = 'background', 'game_not_foreground'
        elif authorized:
            if self._timing['first_authorized_foreground_frame_monotonic'] is None:
                self._timing['first_authorized_foreground_frame_monotonic'] = finished
            self._timing['last_authorized_foreground_frame_monotonic'] = finished
        if not self._stop.is_set():
            self._publish(state, reason, frame, tracker_status=diagnostics,
                          alignment_profile_status=authorization_reason)

    def _sample_interval(self, now=None):
        instant = self._clock() if now is None else now
        if not _finite(instant) or instant < 0:
            return PERIOD
        with self._lock:
            state, frame = self._state.get('map_state'), self._state.get('map_frame')
            if (state == 'active' and isinstance(frame, dict)
                    and frame.get('projection_verified') is True
                    and frame.get('game_foreground') is True
                    and _fresh(frame.get('sample_monotonic'), instant)
                    and _fresh(frame.get('activity_sample_monotonic'), instant)):
                return ACTIVE_PERIOD
            # A short bounded cold/recovery burst removes the old unconditional
            # 50 ms sleep between 2 MiB scan steps. Large-heap fallback remains
            # resumable and slows after 20 s; no unbounded synchronous sweep.
            status = self._state.get('tracker_status')
            if state == 'seeking' and isinstance(status, dict):
                timing = status.get('timing')
                elapsed = timing.get('search_elapsed_seconds') if isinstance(timing, dict) else None
                if (_finite(elapsed) and 0 <= elapsed < DISCOVERY_BURST_SECONDS
                        and status.get('reason') in ('camera_discovery_in_progress',
                                                     'camera_structure_or_exact_read_invalid',
                                                     'activity_frozen_relocating')):
                    return DISCOVERY_PERIOD
        return PERIOD

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self._cycle()
                except Exception as error:
                    self._publish('inactive', 'map_sampler_error', error=str(error))
                    self._drop_process()
                self._stop.wait(self._sample_interval())
        finally:
            self._publish('inactive', 'map_sampler_stopped')
            self._drop_process()

    def start(self):
        with self._lifecycle:
            if self._closed or self._thread is not None:
                return False
            self._thread = threading.Thread(target=self._run, name='FH5MapSampler', daemon=True)
            self._thread.start()
            return True

    def snapshot(self, session_key, now=None):
        instant = self._clock() if now is None else now
        with self._lock:
            result = deepcopy(self._state)
        if self._closed:
            result.update(map_state='inactive', reason='map_service_closed', map_frame=None)
            return result
        if not _finite(instant) or instant < 0:
            result.update(map_state='inactive', reason='invalid_snapshot_time', map_frame=None)
            return result
        try:
            source, source_reason = self._source()
        except Exception:
            source, source_reason = None, 'source_snapshot_failed'
        if (source is None or source['session_key'] != session_key
                or source['process_pid'] != self._pid):
            result.update(map_state='inactive', reason=source_reason or 'requested_session_mismatch', map_frame=None)
            return result
        frame = result.get('map_frame')
        if frame is not None:
            if (frame.get('session_key') != session_key
                    or not _fresh(frame.get('sample_monotonic'), instant)
                    or not _fresh(frame.get('activity_sample_monotonic'), instant)):
                result.update(map_state='inactive', reason='map_frame_stale_or_wrong_session', map_frame=None)
        return result

    def close(self):
        with self._lifecycle:
            self._closed = True
            self._stop.set()
            thread = self._thread
            self._publish('inactive', 'map_service_closed')
            if thread is not None and thread is not threading.current_thread():
                # Each tracker step is bounded. Avoid freezing application exit
                # if a native query nevertheless stalls; finally owns cleanup.
                thread.join(timeout=2.0)
            if thread is None or not thread.is_alive():
                self._drop_process()
                return True
            return False
