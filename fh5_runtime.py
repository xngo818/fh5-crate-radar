"""FH5 provider for the recovered project's panel and radar.

Process memory is queried/read only. One provider owns the UDP socket. World
coverage, the reference height and row identities are not inferred from counts.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import socket
import threading
import time
import xml.etree.ElementTree as ET

from crate_reader import read_candidate, SUCCESS
from crate_signatures import candidates_from_chunk
from telemetry_capture import parse_packet
from win_readonly import Process, find_processes
from fh5_models import StateStore, IdentityCatalog, decorate_candidate
from fh5_paths import STATE_DIR, RESOURCE_DIR

BASE = Path(__file__).resolve().parent
SUPPORTED_VERSION = '3.688.109.0'
DEFAULT_IMAGE = Path(r'C:\XboxGames\Forza Horizon 5\Content\ForzaHorizon5.exe')


def official_car_names():
    """CarOrdinal lookup only. These keys are never applied to crate row_raw."""
    try:
        data = json.loads((BASE / 'data' / 'identity_research' / 'official_car_names.json').read_text(encoding='utf-8'))
        return {key: item['name'] for key, item in data['cars'].items()
                if isinstance(item, dict) and isinstance(item.get('name'), str)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


class StepEnded(Exception):
    pass


def merge_ranges(ranges):
    result = []
    for low, high in sorted(ranges):
        if result and low <= result[-1][1]:
            result[-1][1] = max(high, result[-1][1])
        else:
            result.append([low, high])
    return result


def ranges_for_candidates(candidates):
    return merge_ranges([[(c['first_string_address'] - 4096) & ~4095,
                          (c['first_string_address'] + 8191) & ~4095]
                         for c in candidates])


def package_version(image_path):
    """Read an accessible manifest, never the protected executable payload."""
    # A manifest from another installed copy must never authorize this image.
    parents = [Path(image_path).parent]
    for parent in parents:
        try:
            root = ET.parse(parent / 'appxmanifest.xml').getroot()
            for item in root.iter():
                if item.tag.rsplit('}', 1)[-1] == 'Identity':
                    if item.attrib.get('Name') == 'Microsoft.624F8B84B80':
                        return item.attrib.get('Version')
        except (OSError, ET.ParseError):
            continue
    return None


class LimitedRead:
    def __init__(self, process, stop, seconds=.25, byte_limit=32 * 1024 * 1024):
        self.process, self.stop = process, stop
        self.deadline = time.monotonic() + seconds
        self.remaining = byte_limit
        self.read_calls = self.read_bytes = 0

    def __call__(self, address, size):
        if self.stop.is_set() or time.monotonic() >= self.deadline or size > self.remaining:
            raise StepEnded()
        self.remaining -= size
        self.read_calls += 1
        self.read_bytes += size
        return self.process.read(address, size)


class Locator:
    """Bounded, resumable discovery. Completion is not proof of world coverage."""
    def __init__(self, regions=None):
        self.reset(regions)

    def reset(self, regions=None):
        self.regions = deepcopy(regions)
        self.index = 0
        self.address = None
        self.attempted = 0
        self.failed = 0
        self.complete = False

    def step(self, process, stop, *, seconds=.12, byte_limit=128 * 1024 * 1024):
        if self.complete:
            return [], self.progress()
        if self.regions is None:
            self.regions = process.regions(deadline=time.monotonic() + 1.0)
        reader = LimitedRead(process, stop, seconds=seconds, byte_limit=byte_limit)
        found = []
        try:
            while self.index < len(self.regions):
                region = self.regions[self.index]
                low, high = region['base'], region['base'] + region['size']
                address = max(low, self.address or low)
                if address >= high:
                    self.index += 1
                    self.address = None
                    continue
                size = min(512 * 1024, high - address)
                data = reader(address, size)
                self.attempted += size
                # Keep this cursor until the whole chunk has been considered.
                # A deadline during validation causes a retry, not a false gap.
                if data is not None:
                    found.extend(candidates_from_chunk(reader, address, data))
                else:
                    self.failed += 1
                self.address = high if address + size >= high else address + size - 32
                # Keep the telemetry/UI workers responsive during discovery.
                time.sleep(0)
            self.complete = True
        except StepEnded:
            pass
        return found, self.progress()

    def progress(self):
        return {'attempted_mib': round(self.attempted / 1048576, 1),
                'region_index': self.index,
                'region_count': len(self.regions or []),
                'eligible_mib': round(sum(r['size'] for r in self.regions or []) / 1048576, 1),
                'failed_chunks': self.failed, 'complete_selected_policy': self.complete,
                'complete_world_coverage': False}


class CandidatePool:
    """Session-local F slots; no cached coordinate can become a fresh sample.

    Discovery only registers addresses. Each displayed row still comes from
    read_candidate's before/after signature and identity checks. A limited
    refresh resumes at the unvisited slot, rather than replacing the pool with
    an empty list or renewing the last snapshot's acquisition time.
    """
    def __init__(self, session_key, version):
        self.session_key, self.version = session_key, version
        self.addresses = set()
        self.cursor = None
        self._unstable = {}

    def register(self, candidates):
        for candidate in candidates:
            first = candidate.get('first_string_address')
            if type(first) is int and 0x10000 < first < 0x7fffffffffff and first % 8 == 0:
                if len(self.addresses) < 4096:
                    self.addresses.add(first)

    def refresh(self, process, stop, catalog, *, seconds=.18, byte_limit=1024 * 1024):
        started = time.monotonic()
        if (process.identity.get('session_key') != self.session_key
                or self.version != SUPPORTED_VERSION):
            raise ValueError('candidate_pool_session_or_version_mismatch')
        reader = LimitedRead(process, stop, seconds=seconds, byte_limit=byte_limit)
        addresses = sorted(self.addresses)
        if self.cursor is not None:
            addresses = ([first for first in addresses if first > self.cursor]
                         + [first for first in addresses if first <= self.cursor])
        updates, removed, limited, rejections = [], [], False, {}
        try:
            for first in addresses:
                raw = read_candidate(reader, first, self.session_key)
                unstable_reasons = {'identity_changed_during_read', 'signature_storage_changed_during_read'}
                if raw.get('reason') in unstable_reasons:
                    # A replaced allocation can race our before/after reads.
                    # Retry once, but never display the rejected older sample.
                    raw = read_candidate(reader, first, self.session_key)
                row = decorate_candidate(raw, catalog, self.version)
                if row is None:
                    removed.append(first)
                    reason = str(raw.get('reason') or 'rejected_candidate')
                    rejections[reason] = rejections.get(reason, 0) + 1
                    self._unstable[first] = self._unstable.get(first, 0) + 1
                    if raw.get('reason') not in unstable_reasons or self._unstable[first] >= 3:
                        self.addresses.discard(first)
                        self._unstable.pop(first, None)
                else:
                    updates.append(row)
                    self._unstable.pop(first, None)
                self.cursor = first
        except StepEnded:
            limited = True
        return {'rows': updates, 'removed': removed, 'limited': limited,
                'sample_monotonic': started,
                'elapsed_seconds': time.monotonic() - started,
                'known_slots': len(self.addresses), 'visited_slots': len(updates) + len(removed),
                'read_calls': getattr(reader, 'read_calls', None),
                'read_bytes': getattr(reader, 'read_bytes', None), 'rejections': rejections,
                'read_gaps': sum(rejections.get(reason, 0) for reason in ('read_failed', 'short_or_failed_read'))}


class Runtime:
    def __init__(self, port=53005, period=.5, seed_scope=None, mapping_path=None,
                 state_dir=None, process_factory=Process, process_finder=find_processes,
                 map_profile_path=None, map_service_factory=None):
        if not 49152 <= int(port) <= 65535 or not .2 <= float(period) <= 5:
            raise ValueError('Use high UDP port and period 0.2..5 seconds')
        self.port, self.period = int(port), float(period)
        self.state_dir = Path(state_dir) if state_dir else STATE_DIR
        self.seed_scope = Path(seed_scope) if seed_scope else BASE / 'data' / 'seed_scope.json'
        self.catalog = IdentityCatalog(Path(mapping_path) if mapping_path else BASE / 'data' / 'fh5_loot_mapping.json')
        self.car_names = official_car_names()
        self.store = StateStore()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._finder, self._process_factory = process_finder, process_factory
        self._threads = []
        self._socket = None
        self._socket_lock = threading.Lock()
        self._rescan = threading.Event()
        self._lifecycle = threading.Lock()
        self.map_profile_path = Path(map_profile_path) if map_profile_path is not None else None
        self._map_service_factory = map_service_factory
        self._map_service = None
        self._map_start_error = None
        self._map_close_completed = None
        self._closed = False
        self._car_lock = threading.RLock()
        self._car_session = self._previous_car_ordinal = self._last_car_rescan = None

    def _start_map_service(self):
        # The factory and native sampler are not touched by construction,
        # replay, or offline map preview. The raw store getter avoids recursion.
        service = None
        try:
            factory = self._map_service_factory
            if factory is None:
                from fh5_map_service import MapService
                factory = MapService
            service = factory(self.store.source_snapshot, process_factory=self._process_factory,
                              state_dir=self.state_dir, profile_path=self.map_profile_path)
            if service.start() is False:
                raise RuntimeError('Map sampler did not start')
            self._map_service = service
        except Exception as error:
            self._map_start_error = f'{type(error).__name__}: {error}'
            if service is not None:
                try:
                    service.close()
                except Exception:
                    pass

    def start(self):
        with self._lifecycle:
            if self._threads or self._closed:
                return False
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.stop.clear()
            self._start_map_service()
            for target in (self._memory_loop, self._telemetry_loop):
                thread = threading.Thread(target=target, daemon=True,
                                          name='fh5-' + target.__name__.strip('_'))
                self._threads.append(thread)
                thread.start()
            return True

    def snapshot(self):
        result = self.store.snapshot()
        map_status = {'map_state': 'inactive', 'reason': 'map_service_not_started', 'map_frame': None}
        if self._map_service is not None:
            try:
                sample = self._map_service.snapshot(result.get('session_key'))
                if not isinstance(sample, dict):
                    raise ValueError('Map service snapshot must be a dict')
                map_status.update({key: sample[key] for key in
                                   ('map_state', 'reason', 'map_frame', 'tracker_status', 'map_timing') if key in sample})
            except Exception as error:
                map_status.update(reason='map_service_snapshot_failed', error=str(error), map_frame=None)
        elif self._map_start_error:
            map_status.update(reason='map_service_start_failed', error=self._map_start_error)
        # The memory worker may replace the session while the map snapshot is
        # copied. Never combine the earlier session's camera with the new rows.
        latest_scope = self.store.source_snapshot()
        scope = lambda value: tuple(value.get(key) for key in
                                    ('session_key', 'process_pid', 'game_version'))
        if scope(latest_scope) != scope(result):
            result = self.store.snapshot()
            map_status.update(map_state='inactive', reason='source_changed_while_merging_map', map_frame=None)
        frame = map_status.get('map_frame')
        if frame is not None:
            now = time.monotonic()
            valid_time = isinstance(frame, dict)
            for key in ('sample_monotonic', 'activity_sample_monotonic'):
                stamp = frame.get(key) if isinstance(frame, dict) else None
                try:
                    valid_time = bool(valid_time and isinstance(stamp, (int, float))
                                      and not isinstance(stamp, bool) and math.isfinite(stamp)
                                      and 0 <= now - stamp < .25)
                except OverflowError:
                    valid_time = False
            if (not isinstance(frame, dict) or not valid_time
                    or not result.get('session_key')
                    or result.get('game_version') != SUPPORTED_VERSION
                    or frame.get('session_key') != result.get('session_key')
                    or frame.get('package_version') != SUPPORTED_VERSION):
                map_status.update(map_state='inactive', reason='map_frame_stale_or_wrong_session', map_frame=None)
        if self._closed:
            map_status.update(map_state='inactive', reason='map_service_closed', map_frame=None)
        result.update(map_state=map_status['map_state'], map_reason=map_status['reason'],
                      map_frame=deepcopy(map_status['map_frame']), map_status=deepcopy(map_status),
                      map_timing=deepcopy(map_status.get('map_timing')),
                      map_shutdown_complete=self._map_close_completed)
        result.update(replay=False, complete_world_coverage=False,
                      profile_version=SUPPORTED_VERSION)
        return result

    def request_rescan(self):
        self._rescan.set()
        self.wake.set()

    def _reset_car_observer(self, session_key=None):
        with self._car_lock:
            self._car_session = session_key
            self._previous_car_ordinal = self._last_car_rescan = None

    def _observe_car_ordinal(self, session_key, ordinal, now=None):
        """A model transition can justify restarting search, never a mode claim."""
        instant = time.monotonic() if now is None else now
        try:
            valid_time = (type(instant) in (int, float) and math.isfinite(instant) and instant >= 0)
        except OverflowError:
            valid_time = False
        if not valid_time or self.stop.is_set():
            return False
        current = self.store.source_snapshot()
        with self._car_lock:
            current_key = current.get('session_key')
            if current_key != self._car_session:
                self._reset_car_observer(current_key)
            if (not isinstance(current_key, str) or not current_key
                    or current_key != session_key or current.get('game_version') != SUPPORTED_VERSION
                    or type(ordinal) is not int or not 0 < ordinal <= 0x7fffffff):
                return False
            previous, self._previous_car_ordinal = self._previous_car_ordinal, ordinal
            if previous is None or previous == ordinal or current.get('fresh_row_count'):
                return False
            if self._last_car_rescan is not None and instant - self._last_car_rescan < 10.0:
                return False
            # Recheck the raw source, not Runtime.snapshot(), before waking the
            # existing locator. This never reads memory or identifies a mode.
            latest = self.store.source_snapshot()
            if (latest.get('session_key') != current_key
                    or latest.get('game_version') != SUPPORTED_VERSION or latest.get('fresh_row_count')):
                return False
            self._last_car_rescan = instant
            self.request_rescan()
            return True

    def close(self):
        with self._lifecycle:
            if self._closed:
                return
            self._closed = True
            self.stop.set()
            self.wake.set()
            if self._map_service is not None:
                try:
                    self._map_close_completed = self._map_service.close() is not False
                except Exception:
                    self._map_close_completed = False
            with self._socket_lock:
                listener = self._socket
                if listener is not None:
                    try:
                        listener.close()
                    except OSError:
                        pass
            for thread in self._threads:
                thread.join(timeout=2)
            self.store.set_session(None)
            self._reset_car_observer()
            self.store.set_status(state='已停止', memory_state='已停止', telemetry_state='已停止')

    def _load_scope(self, identity, version):
        for file in (self.state_dir / 'pool_scope.json', self.seed_scope):
            try:
                cache = json.loads(file.read_text(encoding='utf-8-sig'))
                if cache['session_key'] != identity['session_key'] or cache['package_version'] != version:
                    continue
                ranges = cache['ranges']
                if not isinstance(ranges, list) or not 1 <= len(ranges) <= 256:
                    continue
                if any(not isinstance(pair, list) or len(pair) != 2 or
                       any(type(v) is not int for v in pair) or
                       not 0x10000 < pair[0] < pair[1] < 0x7fffffffffff or
                       pair[0] % 4096 or pair[1] % 4096 for pair in ranges):
                    continue
                if sum(high - low for low, high in ranges) > 8 * 1048576:
                    continue
                return merge_ranges(ranges)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return []

    def _save_scope(self, identity, version, ranges):
        if sum(high - low for low, high in ranges) > 8 * 1048576:
            return
        file = self.state_dir / 'pool_scope.json'
        try:
            data = {'session_key': identity['session_key'], 'identity': identity,
                    'package_version': version, 'ranges': ranges,
                    'note': 'Search hint only; revalidate every candidate'}
            temporary = file.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(file)
        except OSError:
            pass

    def _read_pool(self, process, ranges, version):
        """Coarse diagnostic baseline; live refresh uses CandidatePool instead.

        The legacy limited result is not an authoritative empty sample and must
        never be passed to publish_rows. Kept for capture/benchmark comparison.
        """
        reader = LimitedRead(process, self.stop, seconds=.4, byte_limit=12 * 1048576)
        candidates, gaps = {}, 0
        try:
            for low, high in ranges:
                address = low
                while address < high:
                    size = min(65536, high - address)
                    data = reader(address, size)
                    if data is not None:
                        chunks = [(address, data)]
                    else:
                        gaps += 1
                        chunks = []
                        for offset in range(0, size, 4096):
                            part = reader(address + offset, min(4096, size - offset))
                            if part is not None:
                                chunks.append((address + offset, part))
                    for base, chunk in chunks:
                        for candidate in candidates_from_chunk(reader, base, chunk):
                            candidates[candidate['first_string_address']] = candidate
                    if address + size >= high:
                        break
                    address += size - 32
            rows = []
            for first in sorted(candidates):
                raw = read_candidate(reader, first, process.identity['session_key'])
                row = decorate_candidate(raw, self.catalog, version)
                if row is not None:
                    rows.append(row)
            return rows, gaps, False
        except StepEnded:
            # Do not publish an old or half-refreshed list as current.
            return [], gaps, True

    def _memory_loop(self):
        process = None
        locator = Locator()
        scope_locator = None
        pool = None
        ranges, next_search, next_scope, next_background, next_version_check = [], 0.0, 0.0, 0.0, 0.0
        version, attached_at, first_rows_at = None, None, None
        discovery_started_at, discovery_first_rows_at = None, None
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    if process is None or not process.is_alive():
                        if process is not None:
                            process.close()
                            process = None
                        self.store.set_session(None)
                        self.store.set_status(state='等待 FH5 启动', memory_state='等待 FH5 启动')
                        matches = self._finder()
                        if len(matches) != 1:
                            if len(matches) > 1:
                                self.store.set_status(state='存在多个 FH5 进程，暂停读取')
                            self.stop.wait(1)
                            continue
                        process = self._process_factory(matches[0]['pid'])
                        version = package_version(process.identity['image_path'])
                        self.store.set_session(process.identity, game_version=version)
                        self.store.set_status(last_error=None)
                        ranges = self._load_scope(process.identity, version)
                        scope_locator = Locator([{'base': low, 'size': high - low} for low, high in ranges]) if ranges else None
                        pool = CandidatePool(process.identity['session_key'], version)
                        locator.reset()
                        attached_at, first_rows_at = time.monotonic(), None
                        discovery_started_at, discovery_first_rows_at = attached_at, None
                        next_search, next_scope = 0.0, 0.0
                        next_background, next_version_check = started + 60, started + 10
                    if started >= next_version_check:
                        current = package_version(process.identity['image_path'])
                        if current != version:
                            self.store.set_session(None)
                            process.close()
                            process = None
                            continue
                        next_version_check = started + 10
                    if version != SUPPORTED_VERSION:
                        self.store.clear_rows('unsupported_build')
                        self.store.set_status(state=f'当前版本 {version or "未知"} 未验证，暂停坐标读取',
                                              memory_state='版本未匹配')
                        self.stop.wait(1)
                        continue
                    if self._rescan.is_set():
                        self._rescan.clear()
                        locator.reset()
                        scope_locator = Locator([{'base': low, 'size': high - low} for low, high in ranges]) if ranges else None
                        next_search = next_scope = 0
                        discovery_started_at, discovery_first_rows_at = time.monotonic(), None
                    refreshes = []
                    if pool.addresses:
                        refreshes.append(pool.refresh(process, self.stop, self.catalog))
                    discovered = []
                    # A cached scope is session-bound and only a discovery hint.
                    # Its cursor survives deadlines, unlike the old full rescan.
                    if scope_locator is not None and started >= next_scope:
                        found, progress = scope_locator.step(process, self.stop, seconds=.025,
                                                             byte_limit=2 * 1048576)
                        discovered.extend(found)
                        self.store.set_status(scope_scan_progress=progress)
                        if scope_locator.complete:
                            scope_locator.reset([{'base': low, 'size': high - low} for low, high in ranges])
                            next_scope = time.monotonic() + 2.0
                    # Empty-pool discovery runs short resumable steps without a
                    # fixed half-second wait after every 32 MiB. Background
                    # expansion is deliberately slower once live slots exist.
                    searching = not pool.addresses and not discovered and started >= next_search
                    if searching or started >= next_background:
                        found, progress = locator.step(process, self.stop,
                            seconds=.12 if searching else .015,
                            byte_limit=128 * 1048576 if searching else 2 * 1048576)
                        discovered.extend(found)
                        self.store.set_status(scan_progress=progress)
                        if locator.complete:
                            locator.reset()
                            next_search = time.monotonic() + 1.0
                            next_background = time.monotonic() + 60.0
                        elif not searching:
                            next_background = time.monotonic() + 1.0
                    if discovered:
                        pool.register(discovered)
                        combined = merge_ranges(ranges + ranges_for_candidates(discovered))
                        if combined != ranges:
                            ranges = combined
                            self._save_scope(process.identity, version, ranges)
                            scope_locator = Locator([{'base': low, 'size': high - low} for low, high in ranges])
                            next_scope = time.monotonic() + 2.0
                        # Newly found slots can appear in this iteration; there
                        # is no additional polling-period delay before publish.
                        refreshes.append(pool.refresh(process, self.stop, self.catalog))
                    if not process.is_alive():
                        self.store.set_session(None)
                        continue
                    for refresh in refreshes:
                        self.store.publish_row_updates(process.identity['session_key'], refresh['rows'],
                            removed=refresh['removed'], now=refresh['sample_monotonic'])
                    rows = self.store.snapshot()['rows']
                    if rows and first_rows_at is None:
                        first_rows_at = time.monotonic()
                    if rows and discovery_first_rows_at is None:
                        discovery_first_rows_at = time.monotonic()
                    elif not rows and discovery_first_rows_at is not None:
                        discovery_started_at, discovery_first_rows_at = time.monotonic(), None
                    limited = any(refresh['limited'] for refresh in refreshes)
                    message = f'已读取 {len(rows)} 个候选（观察池）' if rows else '等待车包 / 正在定位'
                    self.store.set_status(state=message,
                        memory_state=message if rows else ('观察池无有效候选' if ranges else '正在定位车包池'),
                        scope='observed_pool' if rows else 'partial_discovery',
                        read_gaps=sum(refresh['read_gaps'] for refresh in refreshes),
                        refresh_limited=limited,
                        memory_timing={'session_started_monotonic': attached_at,
                            'first_rows_monotonic': first_rows_at,
                            'first_rows_elapsed_seconds': first_rows_at - attached_at if first_rows_at is not None else None,
                            'discovery_started_monotonic': discovery_started_at,
                            'discovery_first_rows_monotonic': discovery_first_rows_at,
                            'discovery_elapsed_seconds': discovery_first_rows_at - discovery_started_at if discovery_first_rows_at is not None else None,
                            'iteration_elapsed_seconds': time.monotonic() - started,
                            'known_slots': len(pool.addresses),
                            'refreshes': [{key: value for key, value in refresh.items()
                                           if key not in ('rows', 'removed')} for refresh in refreshes]})
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    self.store.clear_rows('read_error')
                    if process is not None:
                        process.close()
                        process = None
                    self.store.set_session(None)
                    self.store.set_status(state=f'读取暂停：{error}', memory_state=f'读取错误：{error}',
                                          last_error=str(error))
                    self.stop.wait(.5)
                target_period = self.period if pool is not None and pool.addresses else .02
                delay = max(.005, target_period - (time.monotonic() - started))
                self.wake.wait(delay)
                self.wake.clear()
        finally:
            if process is not None:
                process.close()
            self.store.set_session(None)

    def _telemetry_loop(self):
        peer_key, last_timestamp, session = None, None, None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(('127.0.0.1', self.port))
                listener.settimeout(.15)
                with self._socket_lock:
                    self._socket = listener
                self.store.set_status(telemetry_state=f'等待 Data Out：127.0.0.1:{self.port}')
                while not self.stop.is_set():
                    try:
                        data, peer = listener.recvfrom(65535)
                    except socket.timeout:
                        continue
                    current = self.store.source_snapshot()['session_key']
                    if current != session:
                        session, peer_key, last_timestamp = current, None, None
                    if session is None or peer[0] != '127.0.0.1' or (peer_key and peer != peer_key):
                        continue
                    try:
                        frame = parse_packet(data)
                    except ValueError:
                        self.store.set_status(telemetry_state='收到不兼容的遥测数据')
                        continue
                    if not frame['is_race_on']:
                        self.store.publish_player(session, None)
                        self.store.set_status(telemetry_state='菜单 / 非活动遥测')
                        continue
                    timestamp = frame['timestamp_ms']
                    if last_timestamp is not None:
                        delta = (timestamp - last_timestamp) & 0xffffffff
                        if delta == 0 or delta > 0x80000000:
                            continue
                    peer_key, last_timestamp = peer, timestamp
                    player = {'x': frame['position_x'], 'y': frame['position_y'], 'z': frame['position_z'],
                              'yaw': frame['yaw'], 'speed': frame['speed_m_per_s'],
                              'car_ordinal': frame['car_ordinal'], 'timestamp_ms': timestamp,
                              'car': self.car_names.get(str(frame['car_ordinal'])),
                              'warnings': frame['warnings']}
                    self.store.publish_player(session, player)
                    self._observe_car_ordinal(session, frame['car_ordinal'])
                    self.store.set_status(telemetry_state='本车遥测已连接')
        except OSError as error:
            if not self.stop.is_set():
                self.store.set_status(telemetry_state=f'遥测接收失败：{error}')
        finally:
            with self._socket_lock:
                self._socket = None


class ReplayRuntime:
    """Offline UI exercise using a saved live_sync report; no native I/O."""
    def __init__(self, report_path):
        self.report = json.loads(Path(report_path).read_text(encoding='utf-8-sig'))
        self.frames = self.report.get('samples', [])
        if not self.frames:
            raise ValueError('Replay report has no samples')
        self.catalog = IdentityCatalog(BASE / 'data' / 'fh5_loot_mapping.json')
        self.car_names = official_car_names()
        self.started = None
        self.closed = False

    def start(self):
        self.started = time.monotonic()
        self.closed = False

    def close(self):
        self.closed = True

    def request_rescan(self):
        pass

    def _source_version(self, frame):
        """Use explicit capture versions; profile defaults are not evidence.

        A frame must not override a conflicting report version. Missing,
        invalid or inconsistent source metadata leaves identity unresolved.
        """
        versions = set()
        for source in (self.report, frame):
            for key in ('package_version', 'game_version'):
                if key not in source:
                    continue
                value = source[key]
                if not isinstance(value, str) or not value.strip():
                    return None
                versions.add(value)
        return next(iter(versions)) if len(versions) == 1 else None

    def snapshot(self):
        if self.started is None:
            self.start()
        position = int((time.monotonic() - self.started) / .5)
        index, cycle = position % len(self.frames), position // len(self.frames)
        frame = self.frames[index]
        version = self._source_version(frame)
        rows = [decorate_candidate(c, self.catalog, version)
                for c in frame.get('candidates', [])]
        rows = [r for r in rows if r is not None]
        telemetry = frame.get('telemetry', {})
        player = {'x': telemetry.get('position_x'), 'y': telemetry.get('position_y'),
                  'z': telemetry.get('position_z'), 'yaw': telemetry.get('yaw'),
                  'speed': telemetry.get('speed_m_per_s'), 'car_ordinal': telemetry.get('car_ordinal'),
                  'car': self.car_names.get(str(telemetry.get('car_ordinal')))}
        if self.closed:
            rows, player = [], None
        identity = self.report.get('identity') or self.report.get('source_identity', {})
        replay_session = f"replay:{identity.get('session_key', 'recording')}:{cycle}"
        for row in rows:
            row['recorded_session_key'] = row.get('session_key')
            row['session_key'] = replay_session
        return deepcopy({'session_key': replay_session,
                         'source_session_key': identity.get('session_key'),
                         'process_pid': identity.get('pid'), 'game_version': version,
                         'rows': rows, 'player': player, 'telemetry_age': 0,
                         'state': '离线数据回放（非实时）', 'memory_state': '回放数据',
                         'telemetry_state': '回放本车位置', 'scan_progress': {},
                         'scope': 'replay_observed_pool', 'replay': True,
                         'complete_world_coverage': False, 'frame_index': index,
                         'replay_cycle': cycle})
