"""Pure map projection and per-frame marker layout for the FH5 frontend.

This module does not discover a camera or read a game process. An engine camera
frame is an input contract, not proof that its matrix matches FH5. The caller
must obtain and verify that frame using a build-specific reader before setting
``projection_verified``. Recorded and synthetic frames only work in explicit
preview mode, whose output is always marked unverified and replayed.

Coordinates use Direct3D row vectors: ``clip = [x, y, z, 1] * matrix``. The
viewport is the map's drawable client area expressed in screen pixels; it is
not assumed to start at the desktop origin. Direct3D clip depth is 0 <= z <= w.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math


SUPPORTED_PACKAGE_VERSIONS = frozenset({'3.688.109.0'})
SOURCES = frozenset({'engine_camera', 'recorded', 'synthetic_preview'})
W_EPSILON = 1e-9
ROWS_TTL = 1.5  # Matches StateStore; callers pass its already-expired snapshot.


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _matrix(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 16
            or not all(_number(item) for item in value)):
        raise ValueError('matrix_requires_16_finite_numbers')
    return tuple(float(item) for item in value)


@dataclass(frozen=True)
class Viewport:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        if (not all(_number(value) for value in
                    (self.x, self.y, self.width, self.height))
                or self.width <= 0 or self.height <= 0):
            raise ValueError('invalid_viewport')
        if not all(math.isfinite(value) for value in
                   (self.x + self.width, self.y + self.height)):
            raise ValueError('invalid_viewport_bounds')

    @classmethod
    def from_mapping(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError('viewport_must_be_mapping')
        try:
            return cls(*(value[key] for key in ('x', 'y', 'width', 'height')))
        except KeyError as error:
            raise ValueError('viewport_requires_screen_origin_and_size') from error


@dataclass(frozen=True)
class MapFrame:
    map_open: bool
    matrix: tuple
    viewport: Viewport
    session_key: str
    package_version: str
    sample_monotonic: float
    source: str
    projection_verified: bool
    game_foreground: bool = False

    def __post_init__(self):
        if type(self.map_open) is not bool:
            raise ValueError('map_open_must_be_bool')
        if type(self.projection_verified) is not bool:
            raise ValueError('projection_verified_must_be_bool')
        if type(self.game_foreground) is not bool:
            raise ValueError('game_foreground_must_be_bool')
        if not _text(self.session_key) or not _text(self.package_version):
            raise ValueError('camera_requires_session_and_package_version')
        if not _number(self.sample_monotonic) or self.sample_monotonic < 0:
            raise ValueError('invalid_camera_sample_time')
        if self.source not in SOURCES:
            raise ValueError('unknown_camera_source')
        object.__setattr__(self, 'matrix', _matrix(self.matrix))
        object.__setattr__(self, 'viewport', Viewport.from_mapping(self.viewport))

    @classmethod
    def from_mapping(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError('map_frame_must_be_mapping')
        try:
            matrix = value['matrix'] if 'matrix' in value else value['matrix16']
            return cls(
                map_open=value['map_open'], matrix=matrix,
                viewport=value['viewport'], session_key=value['session_key'],
                package_version=value['package_version'],
                sample_monotonic=value['sample_monotonic'], source=value['source'],
                projection_verified=value['projection_verified'],
                game_foreground=value.get('game_foreground', False))
        except KeyError as error:
            raise ValueError('incomplete_map_frame') from error


def project_world(matrix, xyz, viewport):
    """Return screen-pixel (x, y), or None for invalid/clipped input.

    A point on an edge is retained. Behind-camera, zero-w and offscreen points
    are discarded rather than clamped into fake markers at the viewport edge.
    """
    try:
        matrix = _matrix(matrix)
        viewport = Viewport.from_mapping(viewport)
        if (not isinstance(xyz, (list, tuple)) or len(xyz) != 3
                or not all(_number(value) for value in xyz)):
            return None
        x, y, z = xyz
        clip = tuple(x * matrix[column] + y * matrix[4 + column]
                     + z * matrix[8 + column] + matrix[12 + column]
                     for column in range(4))
        if not all(math.isfinite(value) for value in clip):
            return None
        cx, cy, cz, cw = clip
        if (cw <= W_EPSILON or not -cw <= cx <= cw
                or not -cw <= cy <= cw or not 0 <= cz <= cw):
            return None
        pixel = (viewport.x + (cx / cw + 1.0) * viewport.width / 2.0,
                 viewport.y + (1.0 - cy / cw) * viewport.height / 2.0)
        return pixel if all(math.isfinite(value) for value in pixel) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _address(value):
    if type(value) is int:
        return value if 0 < value <= 0xffffffffffffffff else None
    if isinstance(value, str) and value.startswith(('0x', '0X')):
        try:
            return value if 0 < int(value, 16) <= 0xffffffffffffffff else None
        except ValueError:
            pass
    return None


def map_markers(snapshot, map_frame, now, max_age=.25, allow_preview=False,
                min_level=0):
    """Return a complete new map layout; never retain a previous marker list.

    ``snapshot`` is the current StateStore.snapshot() result, whose row TTL is
    independent of player telemetry. Optional rows_sample_monotonic or rows_age
    metadata, when supplied, is checked too. A map does not need a player sample.
    ``allow_preview=True`` always yields replay=True and verified=False, even
    if an input happens to label itself as verified. These flags are provenance
    declarations, not a mechanism for authenticating arbitrary external input.
    """
    preview = allow_preview is True
    result = {'markers': [], 'visible': False, 'reason': 'invalid_input',
              'preview': preview, 'replay': preview,
              'verified': False, 'projection_verified': False, 'source': None,
              'min_level': min_level}

    def reject(reason):
        result['reason'] = reason
        return result

    if (type(allow_preview) is not bool or not _number(now) or now < 0
            or not _number(max_age) or max_age <= 0
            or type(min_level) is not int or not 0 <= min_level <= 10):
        return reject('invalid_options')
    if not isinstance(snapshot, Mapping):
        return reject('missing_snapshot')
    try:
        frame = MapFrame.from_mapping(map_frame)
    except (ValueError, TypeError, OverflowError):
        return reject('invalid_map_frame')
    result['source'] = frame.source
    if not frame.map_open:
        return reject('map_closed')
    age = now - frame.sample_monotonic
    if age < 0 or age >= max_age:
        return reject('stale_map_frame')
    if (frame.package_version not in SUPPORTED_PACKAGE_VERSIONS
            or snapshot.get('game_version') != frame.package_version):
        return reject('package_version_mismatch')
    if snapshot.get('session_key') != frame.session_key:
        return reject('session_mismatch')
    if not preview:
        if (frame.source != 'engine_camera' or not frame.projection_verified
                or snapshot.get('replay') is True):
            return reject('projection_not_verified')
        if not frame.game_foreground:
            return reject('game_not_foreground')
    if 'rows_sample_monotonic' in snapshot:
        stamp = snapshot['rows_sample_monotonic']
        if not _number(stamp) or not 0 <= now - stamp < ROWS_TTL:
            return reject('stale_rows')
    if 'rows_age' in snapshot:
        age = snapshot['rows_age']
        if not _number(age) or not 0 <= age < ROWS_TTL:
            return reject('stale_rows')
    rows = snapshot.get('rows')
    if not isinstance(rows, (list, tuple)):
        return reject('missing_rows')

    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_session = row.get('session_key')
        if row_session is not None and row_session != frame.session_key:
            continue
        address = _address(row.get('F', row.get('first_string_address')))
        fingerprint = row.get('fingerprint')
        if address is None or not _text(fingerprint):
            continue
        key = (str(address), fingerprint)
        if key in seen:
            continue
        pixel = project_world(frame.matrix,
                              (row.get('x'), row.get('y'), row.get('z')),
                              frame.viewport)
        if pixel is None:
            continue
        verified_identity = row.get('identity_status') == 'verified'
        level = row.get('level') if verified_identity else 0
        if type(level) is not int or not 1 <= level <= 10:
            level = 0
        if level < min_level:
            continue
        name = row.get('car') if verified_identity else None
        name = name.strip() if _text(name) else None
        car_id = row.get('car_id') if verified_identity else None
        if type(car_id) is not int or not 1 <= car_id <= 0xffffffff:
            car_id = None
        grade = str(level) if level else '?'
        result['markers'].append({
            'F': address, 'fingerprint': fingerprint,
            'level': level, 'grade': grade, 'name': name,
            'car_id': car_id, 'pixel': list(pixel),
            'label': f'{grade}级 {name or "未知车型"}',
            'identity_status': 'verified' if verified_identity else 'unknown',
            'replay': preview, 'verified': not preview,
        })
        seen.add(key)
    result.update(visible=bool(result['markers']),
                  reason='ready' if result['markers'] else 'no_visible_markers',
                  verified=not preview, projection_verified=not preview)
    return result
