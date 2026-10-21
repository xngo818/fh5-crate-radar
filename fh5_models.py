"""Pure FH5 display state and evidence-gated identity mapping.

The registered FH5 uint32 LootRowId at F-8 selects a version-matched loot
mapping. Legacy u64 recordings retain their original exact-match semantics.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re
import threading
import time


ROW_TTL = 1.5
PLAYER_TTL = 1.0
ROW_KIND = 'uint32_at_F_minus_8'
LEGACY_ROW_KIND = 'raw_value_at_F_minus_8'
CORE_SUCCESS = 'experimental_coordinate_candidate'


class StateStore:
    """Publish samples for one process session, then take detached snapshots.

    ``now`` is a monotonic acquisition time, never a wall-clock timestamp.
    Future acquisition times are clamped to the local clock. Out-of-order
    samples within a session and samples from an old session are rejected.
    Expiration is applied when snapshot() is read, including with an idle UI.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._identity = None
        self._rows_at = None
        self._row_samples = None
        self._player_at = None
        self._state = {
            'session_key': None, 'process_pid': None, 'game_version': None,
            'rows': [], 'player': None, 'state': 'waiting_for_process',
            'memory_state': 'waiting_for_process', 'telemetry_state': 'waiting',
            'telemetry_age': None, 'rows_age': None, 'scan_progress': None,
            'scope': 'loaded_candidates_only',
        }

    def _time(self, now=None):
        current = self._clock()
        if not isinstance(current, (int, float)) or not math.isfinite(current):
            raise ValueError('clock must return finite monotonic seconds')
        if now is None:
            return float(current)
        if (not isinstance(now, (int, float)) or isinstance(now, bool)
                or not math.isfinite(now)):
            raise ValueError('now must be finite monotonic seconds')
        return min(float(now), float(current))

    def set_session(self, identity, game_version=None):
        if identity is not None:
            if not isinstance(identity, dict):
                raise ValueError('identity must be a dict or None')
            key = identity.get('session_key')
            if not isinstance(key, str) or not key:
                raise ValueError('identity requires a nonempty session_key')
            pid = identity.get('pid')
        else:
            key = pid = None
        with self._lock:
            changed = (key != self._state['session_key']
                       or pid != self._state['process_pid']
                       or game_version != self._state['game_version'])
            self._identity = deepcopy(identity)
            self._state.update(session_key=key, process_pid=pid,
                               game_version=game_version)
            if changed:
                self._state.update(
                    rows=[], player=None, telemetry_age=None, rows_age=None,
                    state='attached' if key else 'waiting_for_process',
                    memory_state='waiting_for_scan' if key else 'waiting_for_process',
                    telemetry_state='waiting', scan_progress=None,
                    scope_scan_progress=None, memory_timing=None, refresh_limited=False,
                    status_reason='process_session_changed',
                )
                self._rows_at = self._player_at = None
                self._row_samples = None

    def _same_session(self, session_key):
        return bool(session_key) and session_key == self._state['session_key']

    def publish_rows(self, session_key, rows, now=None):
        with self._lock:
            if not self._same_session(session_key):
                return False
            stamp = self._time(now)
            if self._rows_at is not None and stamp < self._rows_at:
                return False
            if not isinstance(rows, (list, tuple)):
                raise ValueError('rows must be a list or tuple')
            self._state['rows'] = deepcopy(list(rows))
            self._rows_at = stamp
            self._row_samples = None
            self._state['memory_state'] = 'ready'
            self._state['status_reason'] = None
            return True

    def publish_row_updates(self, session_key, rows, removed=(), now=None):
        """Merge only rows actually revalidated in a bounded refresh.

        A deadline is not disappearance. Unvisited slots retain their original
        acquisition time and still expire after ROW_TTL; rejected slots are
        removed immediately. This does not renew another row's lease.
        """
        if not isinstance(rows, (list, tuple)) or not isinstance(removed, (list, tuple, set)):
            raise ValueError('row updates and removals must be sequences')
        for row in rows:
            first = row.get('first_string_address') if isinstance(row, dict) else None
            if type(first) is not int or first <= 0:
                raise ValueError('row update requires first_string_address')
        if any(type(first) is not int or first <= 0 for first in removed):
            raise ValueError('removal requires first_string_address')
        with self._lock:
            if not self._same_session(session_key):
                return False
            stamp = self._time(now)
            if self._rows_at is not None and stamp < self._rows_at:
                return False
            if self._row_samples is None:
                # Switching from a complete sample keeps its original age.
                self._row_samples = {
                    row['first_string_address']: (deepcopy(row), self._rows_at)
                    for row in self._state['rows']
                    if type(row.get('first_string_address')) is int and self._rows_at is not None}
            for first in removed:
                self._row_samples.pop(first, None)
            for row in rows:
                self._row_samples[row['first_string_address']] = (deepcopy(row), stamp)
            if rows or removed:
                self._rows_at = stamp
                self._state['memory_state'] = 'ready'
                self._state['status_reason'] = None
            return True

    def publish_player(self, session_key, player, now=None):
        with self._lock:
            if not self._same_session(session_key):
                return False
            stamp = self._time(now)
            if self._player_at is not None and stamp < self._player_at:
                return False
            if player is not None and not isinstance(player, dict):
                raise ValueError('player must be a dict or None')
            inactive = (player is None or player.get('active') is False
                        or ('is_race_on' in player and not player['is_race_on']))
            self._player_at = stamp
            self._state['player'] = None if inactive else deepcopy(player)
            self._state['telemetry_age'] = 0.0
            self._state['telemetry_state'] = 'inactive' if inactive else 'live'
            return True

    def set_status(self, **fields):
        protected = {'rows', 'player', 'session_key', 'process_pid', 'game_version',
                     'telemetry_age'}
        if protected.intersection(fields):
            raise ValueError('sample/session fields must use their publish methods')
        with self._lock:
            self._state.update(deepcopy(fields))

    def clear_rows(self, reason):
        """Fail closed immediately after a scan/read error."""
        with self._lock:
            self._state.update(rows=[], rows_age=None, memory_state=str(reason),
                               status_reason=str(reason))
            self._rows_at = None
            self._row_samples = None

    def source_snapshot(self):
        """Cheap scope/freshness check for telemetry and the camera worker.

        These callers do not consume row contents. Copying their potentially
        large source metadata on every UDP packet needlessly holds the GIL and
        the store lock. Fresh row presence still obeys the exact same TTL.
        """
        with self._lock:
            now = self._time()
            if self._row_samples is not None:
                count = sum(0 <= now - sample[1] < ROW_TTL for sample in self._row_samples.values())
            else:
                count = (len(self._state['rows']) if self._rows_at is not None
                         and 0 <= now - self._rows_at < ROW_TTL else 0)
            return {key: self._state[key] for key in ('session_key', 'process_pid', 'game_version')} | {
                'fresh_row_count': count}

    def snapshot(self):
        with self._lock:
            now = self._time()
            if self._row_samples is not None:
                before = len(self._row_samples)
                self._row_samples = {first: sample for first, sample in self._row_samples.items()
                                     if 0 <= now - sample[1] < ROW_TTL}
                self._state['rows'] = [deepcopy(self._row_samples[first][0])
                                       for first in sorted(self._row_samples)]
                ages = [now - sample[1] for sample in self._row_samples.values()]
                self._state['rows_age'] = max(ages) if ages else None
                if before and not self._row_samples:
                    self._state.update(memory_state='stale_rows', status_reason='rows_ttl_expired')
            elif self._rows_at is not None:
                age = now - self._rows_at
                self._state['rows_age'] = max(0.0, age)
                if age < 0 or age >= ROW_TTL:
                    self._state.update(rows=[], memory_state='stale_rows',
                                       status_reason='rows_ttl_expired', rows_age=None)
                    self._rows_at = None
            if self._player_at is not None:
                age = now - self._player_at
                self._state['telemetry_age'] = max(0.0, age)
                if age < 0 or age >= PLAYER_TTL:
                    self._state['player'] = None
                    self._state['telemetry_state'] = 'stale'
            return deepcopy(self._state)


def _raw_key(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 0 <= value <= 0xFFFFFFFFFFFFFFFF else None
    if (isinstance(value, str) and len(value) <= 20
            and re.fullmatch(r'0|[1-9][0-9]*', value)):
        return value if int(value) <= 0xFFFFFFFFFFFFFFFF else None
    return None


def _evidence_present(value):
    return ((isinstance(value, str) and bool(value.strip()))
            or (isinstance(value, list) and bool(value)
                and all(isinstance(item, str) and item.strip() for item in value)))


def _finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class IdentityCatalog:
    """Read an explicit mapping; invalid or unverified entries remain unknown.

    Schema: {package_version: str, row_kind: 'uint32_at_F_minus_8',
             entries: {'123': {status: 'verified', evidence: 'capture reference',
                              car: 'vehicle name', level: 1..10, value: optional}}}
    Evidence may also be a nonempty list of nonempty reference strings. A car
    name database alone is not this mapping, regardless of coincident keys.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._document = None
        self.row_kind = None
        self.error = None
        try:
            document = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if (not isinstance(document, dict)
                    or not isinstance(document.get('package_version'), str)
                    or not document['package_version'].strip()
                    or document.get('row_kind') not in (ROW_KIND, LEGACY_ROW_KIND)
                    or not isinstance(document.get('entries'), dict)):
                raise ValueError('invalid_identity_catalog_schema')
            self._document = document
            self.row_kind = document['row_kind']
        except (OSError, ValueError, UnicodeError) as exc:
            self.error = str(exc)

    def resolve(self, row_raw, package_version):
        unknown = {'level': 0, 'car': None, 'value': None,
                   'identity_status': 'unknown'}
        document = self._document
        if (document is None or not isinstance(package_version, str)
                or not package_version
                or document['package_version'] != package_version):
            return unknown
        key = _raw_key(row_raw)
        if self.row_kind == ROW_KIND and key is not None and int(key) > 0xffffffff:
            key = None
        entry = document['entries'].get(key) if key is not None else None
        if not isinstance(entry, dict):
            return unknown
        level, car = entry.get('level'), entry.get('car')
        if (entry.get('status') != 'verified'
                or not _evidence_present(entry.get('evidence'))
                or not isinstance(car, str) or not car.strip()
                or not isinstance(level, int) or isinstance(level, bool)
                or not 1 <= level <= 10):
            return unknown
        value = entry.get('value')
        if not isinstance(value, int) or isinstance(value, bool):
            value = None
        car_id = entry.get('car_id')
        if type(car_id) is not int or not 1 <= car_id <= 0xffffffff:
            car_id = None
        if self.row_kind == ROW_KIND and (car_id is None or value is None
                                          or not 1 <= value <= 0xffffffff):
            return unknown
        return {'level': level, 'car': car.strip(), 'value': value,
                'car_id': car_id,
                'identity_status': 'verified', 'identity_evidence': deepcopy(entry['evidence'])}


def decorate_candidate(candidate, catalog, version):
    """Adapt accepted core output to UI rows; unknown identities keep their point."""
    if not isinstance(candidate, dict) or candidate.get('status') != CORE_SUCCESS:
        return None
    xyz = candidate.get('coordinates')
    if (not isinstance(xyz, (list, tuple)) or len(xyz) != 3
            or not all(_finite_number(v) for v in xyz)):
        return None
    raw = candidate.get('row_raw')
    loot_row_id = candidate.get('loot_row_id')
    if catalog.row_kind == ROW_KIND:
        if 'loot_row_id' in candidate:
            key = _raw_key(loot_row_id)
            if key is None or int(key) > 0xffffffff:
                loot_row_id = None
        else:
            # Old captures read 8 bytes, including unrelated four-byte padding.
            # Normalize only for an explicitly selected FH5 uint32 mapping.
            key = _raw_key(raw)
            loot_row_id = int(key) & 0xffffffff if key is not None else None
        lookup = loot_row_id
    else:
        lookup = raw
    identity = catalog.resolve(lookup, version)
    return {
        'x': float(xyz[0]), 'y': float(xyz[1]), 'z': float(xyz[2]),
        **identity, 'row_raw': raw, 'loot_row_id': loot_row_id,
        'row_padding_u32': candidate.get('row_padding_u32'),
        'fingerprint_version': candidate.get('fingerprint_version'),
        'first_string_address': candidate.get('first_string_address'),
        'fingerprint': candidate.get('fingerprint'),
        'session_key': candidate.get('session_key'),
        'status': CORE_SUCCESS, 'experimental': True,
        'coordinates_verified': candidate.get('coordinates_verified') is True,
        'membership_verified': candidate.get('membership_verified') is True,
        'lifetime_verified': candidate.get('lifetime_verified') is True,
        'source_metadata': deepcopy(candidate),
    }
