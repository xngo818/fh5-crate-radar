"""Explicitly marked opening observations, never an inferred game-mode counter.

Only independent, healthy live memory samples can become observations. The
observed pool is not a census of every car drop in the game world.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import csv
import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from fh5_models import CORE_SUCCESS, ROW_TTL

SCHEMA_VERSION = 2
MAX_MATCHES = 50
MAX_ATTEMPTS = 500
MAX_ROWS = 1024
MAX_FILE_BYTES = 512 * 1024
STABLE_SECONDS = 3.0
ARM_TIMEOUT = 120.0
EXPECTED_POOL_SIZE = 72
LEVELS = (7, 8, 9, 10)
METRIC = 'mean_count_per_valid_first_wave_72_observation'
PHASE_SOURCE = 'user_marked_new_match_button'
LEGACY_PHASE_SOURCE = 'user_marked_controller_A_or_panel'
SCOPE = 'first_wave_72_observed'
SAMPLE_FILE = 'first_wave_samples.jsonl'
ATTEMPT_FILE = 'first_wave_attempts.jsonl'
SUMMARY_JSON = 'first_wave_summary.json'
SUMMARY_CSV = 'first_wave_summary.csv'
LIMITATION = ('User-marked first-wave observations with exactly 72 unique verified '
              'objects. The requested pool size 72 is a study inclusion rule, not '
              'an independently verified game fact or proof of full-map coverage. '
              'Missing attempts are excluded, never converted to zero counts.')


def _finite(value):
    return (type(value) in (int, float) and math.isfinite(value))


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _row_key(row, session_key):
    if (type(row) is not dict or row.get('session_key') != session_key
            or row.get('status') != CORE_SUCCESS
            or row.get('replay') is True
            or type(row.get('first_string_address')) is not int
            or not 0 < row['first_string_address'] <= (1 << 64) - 1
            or type(row.get('fingerprint')) is not str
            or not 0 < len(row['fingerprint']) <= 2048
            or not all(_finite(row.get(axis)) for axis in ('x', 'y', 'z'))):
        return None
    level = row.get('level')
    verified = (row.get('identity_status') == 'verified' and type(level) is int
                and 1 <= level <= 10)
    loot_id = row.get('loot_row_id')
    if loot_id is not None and (type(loot_id) is not int or not 0 <= loot_id <= 0xffffffff):
        return None
    # Meter-sized quantization catches displaced/reused objects without
    # treating submillimeter float noise as a new identity every UI frame.
    position = tuple(round(float(row[axis]), 1) for axis in ('x', 'y', 'z'))
    return (row['first_string_address'], row['fingerprint'], loot_id,
            level if verified else 0, *position)


def sample_signature(rows, session_key):
    """Return a deterministic identity set, or None for malformed/conflicting rows.

    Unknown rows remain in the identity set so their disappearance does not
    silently masquerade as a stable partial observation.
    """
    if type(rows) not in (list, tuple) or not 0 < len(rows) <= MAX_ROWS:
        return None
    unique = {}
    for row in rows:
        key = _row_key(row, session_key)
        if key is None:
            return None
        address = key[0]
        if address in unique and unique[address] != key:
            return None
        unique[address] = key
    return tuple(sorted(unique.values(), key=lambda item: item[0]))


def classify_frame(snapshot):
    """Strict live eligibility; old logs without health metadata are not upgraded."""
    result = {'eligible': False, 'reason': 'invalid_snapshot'}
    if type(snapshot) is not dict:
        return result
    session = snapshot.get('session_key')
    if snapshot.get('replay') is True or (type(session) is str and session.startswith('replay:')):
        return dict(result, reason='replay_excluded')
    if type(session) is not str or not session or len(session) > 2048:
        return dict(result, reason='no_process_session')
    if type(snapshot.get('game_version')) is not str or not snapshot['game_version']:
        return dict(result, reason='missing_game_version')
    if ('last_error' not in snapshot or snapshot['last_error'] is not None
            or type(snapshot.get('read_gaps')) is not int or snapshot['read_gaps'] != 0
            or snapshot.get('refresh_limited') is not False):
        return dict(result, reason='unhealthy_or_missing_health')
    age = snapshot.get('rows_age')
    if not _finite(age) or not 0 <= age < ROW_TTL:
        return dict(result, reason='stale_or_missing_rows_age')
    timing = snapshot.get('memory_timing')
    refreshes = timing.get('refreshes') if type(timing) is dict else None
    if type(refreshes) not in (list, tuple) or not 0 < len(refreshes) <= 8:
        return dict(result, reason='missing_independent_sample')
    stamps = []
    for refresh in refreshes:
        if (type(refresh) is not dict or refresh.get('limited') is not False
                or type(refresh.get('read_gaps')) is not int or refresh['read_gaps'] != 0
                or not _finite(refresh.get('sample_monotonic'))
                or refresh['sample_monotonic'] < 0):
            return dict(result, reason='unhealthy_refresh')
        stamps.append(float(refresh['sample_monotonic']))
    signature = sample_signature(snapshot.get('rows'), session)
    if signature is None:
        return dict(result, reason='empty_or_invalid_rows')
    levels = [row[3] for row in signature]
    details = {'observed_count': len(signature), 'unknown_count': levels.count(0)}
    if len(signature) != EXPECTED_POOL_SIZE:
        return dict(result, reason='waiting_for_exactly_72_unique_objects', **details)
    if details['unknown_count']:
        return dict(result, reason='waiting_for_all_72_verified_levels', **details)
    counts = {str(level): levels.count(level) for level in LEVELS}
    counts['purple'] = sum(counts[str(level)] for level in (7, 8, 9))
    return {'eligible': True, 'reason': 'healthy_live_observation',
            'session_key': session, 'game_version': snapshot['game_version'],
            'sample_monotonic': max(stamps),
            'signature': signature,
            'counts': counts, **details}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':'),
                                     sort_keys=True).encode('ascii')).hexdigest()


def _coverage_progress(snapshot):
    """Bounded diagnostic flags; scan-policy completion is not world coverage."""
    progress = snapshot.get('scan_progress')
    if type(progress) is not dict:
        return {}
    result = {}
    for key in ('attempted_mib', 'eligible_mib'):
        if _finite(progress.get(key)) and progress[key] >= 0:
            result[key] = float(progress[key])
    for key in ('complete_selected_policy', 'complete_world_coverage'):
        if type(progress.get(key)) is bool:
            result[key] = progress[key]
    return result


def _valid_record(record):
    return (type(record) is dict and record.get('schema_version') == SCHEMA_VERSION
            and record.get('scope') == SCOPE and record.get('user_marked') is True
            and record.get('metric') == METRIC
            and record.get('expected_pool_size') == EXPECTED_POOL_SIZE
            and record.get('phase_source') in (PHASE_SOURCE, LEGACY_PHASE_SOURCE)
            and type(record.get('match_id')) is str and 0 < len(record['match_id']) <= 128
            and type(record.get('snapshot_hash')) is str and len(record['snapshot_hash']) == 64
            and type(record.get('counts')) is dict
            and all(type(record['counts'].get(str(level))) is int
                    and 0 <= record['counts'][str(level)] <= EXPECTED_POOL_SIZE for level in LEVELS)
            and record['counts'].get('purple') == sum(record['counts'][str(level)] for level in (7, 8, 9))
            and type(record.get('observed_count')) is int and record['observed_count'] == EXPECTED_POOL_SIZE
            and type(record.get('unknown_count')) is int and record['unknown_count'] == 0
            and sum(record['counts'][str(level)] for level in LEVELS) <= EXPECTED_POOL_SIZE)


class OpeningStatistics:
    """At most 50 persisted, explicitly armed opening observations.

    Construction reads existing records but never creates paths. reset() cancels
    only the in-progress mark; it never deletes completed observations.
    """

    def __init__(self, directory, target=MAX_MATCHES, *, clock=time.monotonic, utc_clock=_utc_now):
        if type(target) is not int or not 1 <= target <= MAX_MATCHES:
            raise ValueError('target must be an integer in 1..50')
        self.directory = Path(directory)
        self.target = target
        self._clock, self._utc_clock = clock, utc_clock
        self._lock = threading.RLock()
        self._records = []
        self._attempts = []
        self._armed = None
        self._window = None
        self._last_capture_at = None
        self._next_retry_at = 0.0
        self._error = None
        self._reason = 'not_armed'
        self._state = 'idle'
        self._last_observed_count = None
        self._last_unknown_count = None
        try:
            self._records = self._load()
            self._attempts = self._load_attempts()
            if len(self._records) >= self.target:
                self._state, self._reason = 'complete', 'target_reached'
        except (OSError, ValueError, UnicodeError, TypeError) as exc:
            self._error = f'load_failed: {type(exc).__name__}: {str(exc)[:180]}'
            self._state, self._reason = 'storage_error', 'storage_unavailable'

    def _load(self):
        path = self.directory / SAMPLE_FILE
        if not path.exists():
            return []
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('statistics file exceeds bounded size')
        records, ids, hashes = [], set(), set()
        with path.open('r', encoding='utf-8-sig') as stream:
            for line in stream:
                if not line.strip():
                    continue
                item = json.loads(line)
                if not _valid_record(item):
                    raise ValueError('invalid statistics record; existing file preserved')
                if item['match_id'] in ids or item['snapshot_hash'] in hashes:
                    continue
                records.append(item)
                ids.add(item['match_id'])
                hashes.add(item['snapshot_hash'])
                if len(records) > MAX_MATCHES:
                    raise ValueError('statistics file exceeds 50 records')
        return records

    def _load_attempts(self):
        path = self.directory / ATTEMPT_FILE
        if not path.exists():
            return []
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('attempt history exceeds bounded size')
        attempts, ids = [], set()
        with path.open('r', encoding='utf-8-sig') as stream:
            for line in stream:
                if not line.strip():
                    continue
                item = json.loads(line)
                if (type(item) is not dict or item.get('schema_version') != SCHEMA_VERSION
                        or item.get('scope') != SCOPE or type(item.get('match_id')) is not str
                        or not 0 < len(item['match_id']) <= 128
                        or item.get('outcome') not in ('armed', 'captured', 'missing')):
                    raise ValueError('invalid attempt record; existing file preserved')
                if item['match_id'] in ids:
                    raise ValueError('duplicate attempt record')
                attempts.append(item)
                ids.add(item['match_id'])
                if len(attempts) > MAX_ATTEMPTS:
                    raise ValueError('attempt history limit reached')
        return attempts

    def _attempt_summary(self):
        completed = {row['match_id'] for row in self._records}
        attempts = {row['match_id'] for row in self._attempts} | completed
        active = self._armed['match_id'] if self._armed else None
        missing = sum(identity not in completed and identity != active for identity in attempts)
        return {'actual_n': len(self._records), 'attempt_count': len(attempts),
                'missing_count': missing, 'pending_count': int(active is not None and active not in completed),
                'attempt_limit': MAX_ATTEMPTS}

    def _now(self):
        now = self._clock()
        if not _finite(now) or now < 0:
            raise ValueError('clock must be finite monotonic seconds')
        return float(now)

    def status(self):
        with self._lock:
            window = self._window
            return deepcopy({'state': self._state, 'reason': self._reason,
                'count': len(self._records), 'target': self.target,
                **self._attempt_summary(),
                'armed_session': self._armed['session_key'] if self._armed else None,
                'stable_seconds': (window['last'] - window['first']) if window else 0.0,
                'distinct_samples': window['samples'] if window else 0,
                'last_sample': self._records[-1] if self._records else None,
                'observed_count': self._last_observed_count, 'unknown_count': self._last_unknown_count,
                'error': self._error, 'scope': SCOPE, 'metric': METRIC,
                'expected_pool_size': EXPECTED_POOL_SIZE, 'limitation': LIMITATION})

    def arm_match(self, session_key, *, monotonic=None, utc=None, match_id=None):
        with self._lock:
            if self._armed is not None:
                return self.status()
            if len(self._records) >= self.target:
                self._state, self._reason = 'complete', 'target_reached'
                return self.status()
            if (type(session_key) is not str or not session_key or len(session_key) > 2048
                    or session_key.startswith('replay:')):
                self._state, self._reason = 'idle', 'live_session_required'
                return self.status()
            now = self._now()
            if self._last_capture_at is not None and now - self._last_capture_at < 5:
                self._reason = 'capture_cooldown'
                return self.status()
            if monotonic is not None and (not _finite(monotonic) or monotonic > now or monotonic < 0):
                self._reason = 'invalid_mark_time'
                return self.status()
            mark = now if monotonic is None else float(monotonic)
            identity = uuid.uuid4().hex if match_id is None else match_id
            if type(identity) is not str or not 0 < len(identity) <= 128:
                self._reason = 'invalid_match_id'
                return self.status()
            if any(row['match_id'] == identity for row in self._records):
                self._reason = 'duplicate_match_id'
                return self.status()
            self._armed = {'session_key': session_key, 'monotonic': mark,
                           'utc': str(self._utc_clock() if utc is None else utc)[:128],
                           'match_id': identity}
            try:
                with self._disk_lock():
                    self._records = self._load()
                    if len(self._records) >= self.target:
                        self._armed = None
                        self._state, self._reason = 'complete', 'target_reached'
                        return self.status()
                    self._save_attempt_in_lock(self._armed, 'armed', PHASE_SOURCE)
                    self._write_summaries()
            except (OSError, ValueError, UnicodeError, TypeError) as exc:
                self._error = f'arm_save_failed: {type(exc).__name__}: {str(exc)[:180]}'
                self._armed = None
                self._reason = 'attempt_storage_unavailable'
                return self.status()
            self._window = None
            self._last_observed_count = self._last_unknown_count = None
            self._state, self._reason = 'armed', 'waiting_for_stable_scan'
            return self.status()

    def cancel(self, reason='user_cancelled'):
        with self._lock:
            armed = self._armed
            self._armed = self._window = None
            if armed is not None:
                try:
                    with self._disk_lock():
                        self._save_attempt_in_lock(armed, 'missing', str(reason)[:180])
                        self._write_summaries()
                except (OSError, ValueError, UnicodeError, TypeError) as exc:
                    self._error = f'cancel_save_failed: {type(exc).__name__}: {str(exc)[:180]}'
            self._state = 'complete' if len(self._records) >= self.target else 'idle'
            self._reason = str(reason)[:180]
            return self.status()

    def reset(self):
        return self.cancel('reset_pending_mark')

    def update(self, snapshot):
        with self._lock:
            if self._armed is None:
                return self.status()
            now = self._now()
            if now - self._armed['monotonic'] > ARM_TIMEOUT or now < self._armed['monotonic']:
                return self.cancel('opening_mark_expired')
            if type(snapshot) is not dict or snapshot.get('session_key') != self._armed['session_key']:
                return self.cancel('process_session_changed_rearm_required')
            frame = classify_frame(snapshot)
            self._last_observed_count = frame.get('observed_count')
            self._last_unknown_count = frame.get('unknown_count')
            if not frame['eligible']:
                self._window = None
                self._reason = frame['reason']
                return self.status()
            stamp = frame['sample_monotonic']
            if stamp <= self._armed['monotonic'] or stamp > now or now - stamp >= ROW_TTL:
                self._window = None
                self._reason = 'sample_not_after_mark_or_stale'
                return self.status()
            snapshot_hash = _digest([self._armed['session_key'], snapshot.get('game_version'),
                                     frame['signature']])
            if any(row['snapshot_hash'] == snapshot_hash for row in self._records):
                # A fresh complete duplicate consumes the explicit click at
                # once. Waiting for stability here would allow a later object
                # change in the same match to become an unintended new record.
                return self.cancel('duplicate_observation_ignored')
            window = self._window
            if window and stamp == window['last']:
                return self.status()
            if (window is None or frame['signature'] != window['signature']
                    or frame['game_version'] != window['game_version']
                    or stamp < window['last'] or stamp - window['last'] >= ROW_TTL):
                self._window = {'signature': frame['signature'], 'first': stamp,
                                'last': stamp, 'samples': 1, 'game_version': frame['game_version']}
                self._reason = 'waiting_for_stable_scan'
                return self.status()
            window['last'] = stamp
            window['samples'] += 1
            if window['samples'] < 3 or stamp - window['first'] < STABLE_SECONDS:
                return self.status()
            if now < self._next_retry_at:
                return self.status()
            record = {'schema_version': SCHEMA_VERSION, 'scope': SCOPE, 'user_marked': True,
                'metric': METRIC, 'expected_pool_size': EXPECTED_POOL_SIZE,
                'phase_source': PHASE_SOURCE,
                'match_id': self._armed['match_id'], 'marked_utc': self._armed['utc'],
                'captured_utc': str(self._utc_clock())[:128],
                'session_hash': _digest(self._armed['session_key']),
                'game_version': str(snapshot.get('game_version') or '')[:128],
                'mark_monotonic': self._armed['monotonic'],
                'sample_monotonic': stamp, 'stable_seconds': stamp - window['first'],
                'distinct_samples': window['samples'], 'snapshot_hash': snapshot_hash,
                'counts': frame['counts'], 'observed_count': frame['observed_count'],
                'unknown_count': frame['unknown_count'], 'coverage': 'observed_only',
                'scan_progress': _coverage_progress(snapshot), 'limitation': LIMITATION}
            try:
                saved = self._persist(record)
            except (OSError, ValueError, UnicodeError, TypeError) as exc:
                self._error = f'save_failed: {type(exc).__name__}: {str(exc)[:180]}'
                self._reason = 'storage_error_retrying'
                self._next_retry_at = now + 5.0
                return self.status()
            self._armed = self._window = None
            self._last_capture_at = now
            self._state = 'complete' if len(self._records) >= self.target else 'captured'
            self._reason = ('target_reached' if self._state == 'complete'
                            else 'opening_observation_saved' if saved else 'duplicate_observation')
            result = self.status()
            if saved:
                result['captured_sample'] = deepcopy(record)
            return result

    @contextmanager
    def _disk_lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / '.statistics.lock'
        with path.open('a+b') as stream:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _atomic_write(self, name, text, *, encoding='utf-8'):
        destination = self.directory / name
        temporary = self.directory / ('.' + name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('w', encoding=encoding, newline='') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _persist(self, record):
        with self._disk_lock():
            records = self._load()
            duplicate = any(row['match_id'] == record['match_id']
                            or row['snapshot_hash'] == record['snapshot_hash'] for row in records)
            saved = not duplicate and len(records) < self.target
            if saved:
                records.append(record)
                data = ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in records)
                if len(data.encode('utf-8')) > MAX_FILE_BYTES:
                    raise ValueError('statistics file exceeds bounded size')
                self._atomic_write(SAMPLE_FILE, data)
            # The JSONL file is authoritative. A summary failure must not cause
            # a second copy of a successfully saved observation on retry.
            self._records = records
            self._error = None
            try:
                if self._armed is not None:
                    self._save_attempt_in_lock(self._armed,
                        'captured' if saved else 'missing',
                        'valid_first_wave_72_saved' if saved else 'duplicate_or_target_reached')
                self._write_summaries()
            except (OSError, ValueError, UnicodeError, TypeError) as exc:
                self._error = f'summary_failed: {type(exc).__name__}: {str(exc)[:180]}'
            return saved

    def _save_attempt_in_lock(self, armed, outcome, reason):
        attempts = self._load_attempts()
        existing = next((item for item in attempts if item['match_id'] == armed['match_id']), None)
        if existing is None:
            if len(attempts) >= MAX_ATTEMPTS:
                raise ValueError('attempt history limit reached; no new mark accepted')
            existing = {'schema_version': SCHEMA_VERSION, 'scope': SCOPE,
                'match_id': armed['match_id'], 'marked_utc': armed['utc'],
                'session_hash': _digest(armed['session_key']),
                'phase_source': PHASE_SOURCE, 'expected_pool_size': EXPECTED_POOL_SIZE}
            attempts.append(existing)
        existing.update(outcome=outcome, reason=reason,
                        updated_utc=str(self._utc_clock())[:128],
                        last_observed_count=self._last_observed_count,
                        last_unknown_count=self._last_unknown_count)
        data = ''.join(json.dumps(item, ensure_ascii=False, sort_keys=True) + '\n' for item in attempts)
        if len(data.encode('utf-8')) > MAX_FILE_BYTES:
            raise ValueError('attempt history exceeds bounded size')
        self._atomic_write(ATTEMPT_FILE, data)
        self._attempts = attempts

    def _write_summaries(self):
        records = self._records
        keys = [str(level) for level in LEVELS] + ['purple']
        totals = {key: sum(row['counts'][key] for row in records) for key in keys}
        expected = {key: totals[key] / len(records) if records else None for key in keys}
        summary = {'schema_version': SCHEMA_VERSION, 'scope': SCOPE, 'metric': METRIC,
            'sample_count': len(records), **self._attempt_summary(), 'target': self.target,
            'target_reached': len(records) >= self.target, 'expected_pool_size': EXPECTED_POOL_SIZE,
            'phase_source': PHASE_SOURCE, 'limitation': LIMITATION,
            'coverage': 'observed_only', 'totals': totals, 'means': expected,
            'expectations': expected, 'expected_counts': expected,
            'samples': records, 'attempts': self._attempts}
        self._atomic_write(SUMMARY_JSON, json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['sample', 'match_id', 'marked_utc', 'captured_utc', 'level_7', 'level_8',
            'level_9', 'level_10', 'purple_7_8_9', 'observed_count', 'unknown_count', 'user_marked',
            'scope', 'metric', 'expected_pool_size', 'phase_source', 'coverage', 'attempted_mib',
            'eligible_mib', 'complete_selected_policy', 'complete_world_coverage'])
        for index, row in enumerate(records, 1):
            progress = row.get('scan_progress', {})
            writer.writerow([index, row['match_id'], row.get('marked_utc'), row.get('captured_utc'),
                *(row['counts'][key] for key in keys), row['observed_count'], row['unknown_count'],
                True, SCOPE, METRIC, EXPECTED_POOL_SIZE, row['phase_source'], 'observed_only',
                *(progress.get(key) for key in ('attempted_mib', 'eligible_mib',
                    'complete_selected_policy', 'complete_world_coverage'))])
        self._atomic_write(SUMMARY_CSV, output.getvalue(), encoding='utf-8-sig')
