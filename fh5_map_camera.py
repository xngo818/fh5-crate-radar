"""Build-scoped, injected read-only FH5 map-camera discovery and observation.

No DLL, process handle, thread, socket or UI is created here. The caller owns
the read-only Process. Geometry and narrow clock activity are lifecycle
heuristics measured on 3.688.109.0, NOT an independently recovered map-open
flag. Every returned candidate remains projection_verified=False. The caller
must separately authorize a measured viewport/alignment profile for drawing.
"""
from __future__ import annotations

import math
import struct
import time


SUPPORTED_VERSION = '3.688.109.0'
MATRIX_OFFSET = -672
MATRIX_COPY_OFFSET = -1712
READ_BEFORE = 1712
READ_AFTER = 0x198
READ_SIZE = READ_BEFORE + READ_AFTER
MAX_STEP_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 32
WRITABLE = frozenset((4, 8, 0x40, 0x80))
ONE = struct.pack('<f', 1.0)
RESCAN_BACKOFF = .5
HOT_REVISIT_SECONDS = 1.0
HOT_RADIUS = 4 * 1024 * 1024
MAX_INACTIVE_HINT_REGIONS = 2
MAX_HOT_REGIONS = 16
# Observed in two independently created 3.688.109.0 processes: the persistent
# geometry-only map camera is this far into its eligible private allocation.
# This is a search-order optimization, never a pointer or an accepted frame.
# Every probe is covered by current metadata and fully decoded; a mismatch
# simply falls back to the ordinary resumable scan. No absolute VA is reused.
ALLOCATION_ANCHOR_OFFSETS = (0x2333D0,)
MAX_ANCHOR_PROBES_PER_STEP = 64
ACQUISITION_TARGET_SECONDS = 20.0
METADATA_REFRESH_SECONDS = 2.0
COLD_REVISIT_SECONDS = 1.0
COLD_WINDOW_BYTES = 4 * 1024 * 1024


def _finite(values):
    return all(math.isfinite(v) for v in values)


def _head(blob, offset):
    if offset < 0 or offset + 44 > len(blob):
        return None
    v = struct.unpack_from('<11f', blob, offset)
    if not _finite(v):
        return None
    x, y, z, w, tx, ty, tz, tw, width, height, aspect = v
    if (abs(w - 1) > .001 or abs(tw - 1) > .001
            or abs(x-tx) > .05 or abs(z-tz) > .05
            or max(abs(x), abs(z), abs(tx), abs(tz)) > 60000
            or max(abs(y), abs(ty)) > 100000 or not y > ty
            or not 320 <= width <= 16384 or not 240 <= height <= 16384
            or not .5 <= aspect <= 5 or abs(width/height-aspect) > .002):
        return None
    return v


def decode_camera(blob, start, address, now, *, allow_inactive_hint=False):
    """Pure coherent decode. ``start`` is the virtual address of blob[0].

    One read must cover [camera-1712,camera+0x198). The record's position and
    target are measured near/far plane points, not the eye. Raw projection is
    retained; flattened_matrix removes Y dependence to project map-plane Y=0.
    A structurally valid result still requires ClockActivity before use.
    ``allow_inactive_hint`` permits missing/invalid activity fields ONLY for a
    bounded search hint. That geometry-only result is never an active frame.
    """
    try:
        if (type(start) is not int or type(address) is not int or address % 4
                or not isinstance(blob, (bytes, bytearray, memoryview))
                or type(now) not in (int, float) or not math.isfinite(now) or now < 0):
            return None
        index = address-start
        if index < READ_BEFORE or index+READ_AFTER > len(blob):
            return None
        head = _head(blob, index)
        if head is None:
            return None
        x, y, z, w, tx, ty, tz, tw, width, height, aspect = head
        primary = bytes(blob[index+MATRIX_OFFSET:index+MATRIX_OFFSET+64])
        copy = bytes(blob[index+MATRIX_COPY_OFFSET:index+MATRIX_COPY_OFFSET+64])
        if primary != copy:
            return None
        m = struct.unpack('<16f', primary)
        if not _finite(m) or max(map(abs, m)) > 1e6:
            return None
        # Measured downward perspective family; no screen-capture size guess.
        if (abs(m[3]) > 1e-7 or abs(m[11]) > 1e-7 or m[7] >= -1e-7
                or abs(m[4]) > 1e-7 or abs(m[5]) > 1e-7
                or abs(m[6]) < 1e-7 or abs(m[6]-m[7]) < 1e-7):
            return None
        eye = -m[15]/m[7]
        near_y = -m[14]/m[6]
        far_y = (m[15]-m[14])/(m[6]-m[7])
        near, far = eye-near_y, eye-far_y
        if (not _finite((eye, near_y, far_y, near, far))
                or not 200 <= eye <= 40000
                or abs(near-10) > .05 or abs(far-10000) > 2
                or abs(y-near_y) > .05 or abs(ty-far_y) > 2):
            return None
        clip = [x*m[i]+z*m[8+i]+m[12+i] for i in range(4)]
        if (not _finite(clip) or clip[3] <= 0 or not 0 <= clip[2] <= clip[3]
                or abs(clip[0]/clip[3])*width/2 > 1
                or abs(clip[1]/clip[3])*height/2 > 1):
            return None
        # Rotation-tolerant, square-pixel basis. The map can rotate, while the
        # measured Y-only perspective denominator remains unchanged.
        east = (m[0]/clip[3]*width/2, m[1]/clip[3]*height/2)
        north = (m[8]/clip[3]*width/2, m[9]/clip[3]*height/2)
        le, ln = math.hypot(*east), math.hypot(*north)
        if (not .001 <= le <= 20 or not .001 <= ln <= 20
                or abs(le/ln-1) > .01
                or abs((east[0]*north[0]+east[1]*north[1])/(le*ln)) > .01):
            return None
        delta = [struct.unpack_from('<f', blob, index+offset)[0]
                 for offset in (0xB0, 0xC4, 0xD8)]
        clock = struct.unpack_from('<f', blob, index+0x194)[0]
        activity_fields_valid = bool(_finite(delta) and delta[0] == delta[1] == delta[2]
                                     and 0 < delta[0] < .25 and math.isfinite(clock)
                                     and 0 <= clock <= 1e9)
        if not activity_fields_valid and allow_inactive_hint is not True:
            return None
        flattened = list(m)
        flattened[4:8] = [0.0]*4
        return {'camera_address': address, 'camera_address_hex': hex(address),
                'raw_matrix': list(m), 'flattened_matrix': flattened,
                'render_width': width, 'render_height': height,
                'position': [x, y, z], 'target': [tx, ty, tz],
                'eye_y': eye, 'near_distance': near, 'far_distance': far,
                'pixel_scale': (le+ln)/2,
                'activity_clock': clock if activity_fields_valid else None,
                'activity_delta': delta[0] if activity_fields_valid else None,
                'activity_fields_valid': activity_fields_valid,
                'search_hint_only': not activity_fields_valid,
                'sample_monotonic': float(now),
                'activity_sample_monotonic': None,
                'projection_verified': False, 'independent_map_open_flag': False,
                'reason': ('structure_valid_activity_pending' if activity_fields_valid
                           else 'geometry_only_inactive_search_hint')}
    except (ValueError, TypeError, struct.error, OverflowError, ZeroDivisionError):
        return None


class ClockActivity:
    """Two distinct consecutive clock samples establish narrow-field activity."""
    def __init__(self, max_age=.25):
        if not math.isfinite(max_age) or not 0 < max_age <= 1:
            raise ValueError('invalid activity max_age')
        self.max_age = max_age
        self.reset()

    def reset(self):
        self.clock = self.sample_time = self.last_change = None
        self.proven = False
        self.reason = 'activity_first_sample_required'

    def observe(self, clock, now):
        if (type(clock) not in (int, float) or type(now) not in (int, float)
                or not _finite((clock, now)) or clock < 0 or now < 0):
            self.reset()
            self.reason = 'activity_invalid_clock_or_time'
            return False
        if (self.clock is None or now < self.sample_time
                or now-self.sample_time > self.max_age or clock < self.clock):
            if self.clock is not None and now-self.sample_time > self.max_age:
                reason = 'activity_sample_gap'
            elif self.clock is not None and now < self.sample_time:
                reason = 'activity_time_reversed'
            else:
                reason = 'activity_waiting_second_distinct_sample'
            self.clock, self.sample_time, self.last_change = clock, now, now
            self.proven = False
            self.reason = reason
            return False
        if clock > self.clock:
            self.last_change = now
            self.proven = True
        self.clock, self.sample_time = clock, now
        if now-self.last_change > self.max_age:
            # Require another distinct sample after a freeze, not old history.
            self.proven = False
            self.reason = 'activity_frozen'
            return False
        self.reason = 'activity_current' if self.proven else 'activity_waiting_second_distinct_sample'
        return self.proven


def candidate_addresses(blob, start, *, limit=64):
    """Cheap fixed-layout heads. bytes.find avoids numpy or whole-heap unpack."""
    result, cursor = [], 12
    while len(result) < limit:
        match = blob.find(ONE, cursor)
        if match < 0:
            break
        cursor = match+4
        offset = match-12
        if ((start+offset) % 4 == 0 and offset >= 0 and offset+44 <= len(blob)
                and blob[offset+28:offset+32] == ONE
                and _head(blob, offset) is not None):
            result.append(start+offset)
    return result


class CameraLocator:
    """Session-bound full scan plus periodic local revisits while seeking.

    Region bases can change when committed ranges split/merge. Hints therefore
    retain learned intervals and nearby camera *search anchors*, never accepted
    addresses. Every discovery is decoded and clock-proved again by Tracker.
    A small periodic hot pass does not discard full-scan progress: a camera
    allocated behind its cursor can be found before a multi-GiB sweep finishes.
    """
    def __init__(self, session_key, hot_regions=()):
        self.session_key = session_key
        # Persisted hints arrive in priority order. Keep that bounded order
        # separately from the public membership set, then move confirmed
        # active regions to the front as allocations change within a session.
        self._hot_region_order = []
        for base in hot_regions:
            base = int(base)
            if base not in self._hot_region_order:
                self._hot_region_order.append(base)
                if len(self._hot_region_order) >= MAX_HOT_REGIONS:
                    break
        self.hot_regions = set(self._hot_region_order)
        self._has_active_hint = bool(self.hot_regions)
        self.hot_ranges = []
        self.hot_anchors = []
        self.inactive_hint_regions = {}
        self.reset()

    def reset(self, drop_hot=False):
        if drop_hot:
            self.hot_regions.clear()
            self._hot_region_order.clear()
            self.hot_ranges.clear()
            self.hot_anchors.clear()
            self.inactive_hint_regions.clear()
            self._has_active_hint = False
        self.regions = None
        self._scan_regions = None
        self.index = 0
        self.address = None
        self.tail = b''
        self._hot_regions = []
        self._hot_index = 0
        self._hot_address = None
        self._hot_tail = b''
        self._next_hot_visit = 0.0
        self.complete = False
        self.read_bytes = 0
        self.hot_read_bytes = 0
        self.hot_passes = 0
        self.metadata_queries = 0
        self.read_failures = 0
        self.last_read_failure_address = None
        self._anchor_probes = None
        self._anchor_probe_index = 0
        self.anchor_probe_reads = self.anchor_probe_read_bytes = 0
        self.anchor_probe_hits = 0
        self._next_metadata_refresh = 0.0
        self.metadata_refresh_error = None
        self.metadata_refresh_failures = 0
        self._metadata_failure_streak = 0
        self._urgent_regions = []
        self._urgent_index = 0
        self._urgent_address = None
        self._urgent_tail = b''
        self._urgent_full_turn = False
        self._cold_cursor = 0
        self._cold_offsets = {}
        self._next_cold_visit = 0.0
        self.cold_passes = 0
        self.reason = 'locator_not_started'

    def _metadata(self, process):
        self.metadata_queries += 1
        # An early game process can briefly exceed a one-second complete VA
        # enumeration. Retry with a bounded larger deadline instead of making
        # that initial timeout a permanent inability to discover any camera.
        allowance = min(4.0, 1.0+self._metadata_failure_streak)
        metadata = process.regions(deadline=time.monotonic()+allowance)
        return [r for r in metadata if (
            r.get('state') == 0x1000 and r.get('type') == 0x20000
            and not r.get('protect', 0) & 0x101
            and r.get('protect', 0) & 0xff in WRITABLE
            and type(r.get('base')) is int and type(r.get('size')) is int
            and r['size'] > 0 and r['base'] >= process.min_address
            and r['base']+r['size']-1 <= process.max_address)]

    def _priority(self, region):
        low, high = region['base'], region['base']+region['size']
        def distance(point):
            return low-point if point < low else point-high+1 if point >= high else 0
        anchor_distance = min((distance(a) for a in self.hot_anchors), default=1 << 64)
        hint_distance = min((distance(a) for a in self.hot_regions), default=1 << 64)
        overlaps = any(low < end and start < high for start, end in self.hot_ranges)
        near_range = any(low < end+HOT_RADIUS and start-HOT_RADIUS < high
                         for start, end in self.hot_ranges)
        rank = (0 if anchor_distance == 0 else 1 if overlaps else
                2 if hint_distance == 0 else 3 if min(anchor_distance, hint_distance) <= HOT_RADIUS
                else 4 if near_range else 5)
        return rank, min(anchor_distance, hint_distance), low

    def _prepare_anchor_probes(self):
        ordered = sorted(self.regions, key=lambda r: (
            not 16*1024*1024 <= r['size'] <= 128*1024*1024,
            self._priority(r)))
        self._anchor_probes = [r['base']+offset for r in ordered
                               for offset in ALLOCATION_ANCHOR_OFFSETS
                               if offset >= READ_BEFORE and offset+READ_AFTER <= r['size']]
        self._anchor_probe_index = 0

    def _refresh_metadata(self, process, now):
        # A timeout is a failed observation, not an empty address space or a
        # reason to throw away a multi-GiB sweep's already completed prefix.
        started = time.monotonic()
        try:
            regions = self._metadata(process)
        except (OSError, TimeoutError, ProcessLookupError, ValueError) as error:
            self.metadata_refresh_error = str(error)
            self.metadata_refresh_failures += 1
            self._metadata_failure_streak += 1
            self.complete = False
            return False
        finally:
            # Back off from completion, including a slow native enumeration;
            # a 4 s timeout must not make the next 2 s retry immediately due.
            self._next_metadata_refresh = (now+max(0.0, time.monotonic()-started)
                                           +METADATA_REFRESH_SECONDS)
        previous, self.regions = self.regions, regions
        self.metadata_refresh_error = None
        self._metadata_failure_streak = 0
        if previous is None:
            self._scan_regions = sorted(regions, key=self._priority)
            self._prepare_anchor_probes()
        else:
            # Newly committed or expanded portions must not wait for the old
            # immutable full-scan frontier to finish. Diff coverage, not just
            # bases: VirtualQueryEx may split or merge the same allocation.
            previous = sorted(previous, key=lambda r: r['base'])
            previous_index = 0
            additions = []
            for region in sorted(regions, key=lambda r: r['base']):
                cursor, end = region['base'], region['base']+region['size']
                while (previous_index < len(previous)
                       and previous[previous_index]['base']+previous[previous_index]['size'] <= cursor):
                    previous_index += 1
                overlap = previous_index
                while overlap < len(previous):
                    old = previous[overlap]
                    overlap += 1
                    low, high = old['base'], old['base']+old['size']
                    if high <= cursor:
                        continue
                    if low >= end:
                        break
                    if cursor < low:
                        additions.append({'base': cursor, 'size': min(low, end)-cursor})
                    cursor = max(cursor, high)
                    if cursor >= end:
                        break
                if cursor < end:
                    additions.append({'base': cursor, 'size': end-cursor})
            if additions:
                # Small camera allocations must not queue behind a freshly
                # committed multi-GiB resource heap, even if that heap's local
                # scan was already underway. Preserve its remaining suffix;
                # only the 44-byte head overlap is replayed when resuming it.
                pending = [dict(r) for r in self._urgent_regions[self._urgent_index:]]
                if pending and self._urgent_address is not None:
                    first = pending[0]
                    finish = first['base']+first['size']
                    first['base'] = max(first['base'], self._urgent_address-44)
                    first['size'] = finish-first['base']
                additions.sort(key=lambda r: (r['size'] > 128*1024*1024,
                                               not 16*1024*1024 <= r['size'] <= 128*1024*1024,
                                               r['size'], r['base']))
                self._urgent_regions = additions+pending
                self._urgent_index = 0
                self._urgent_address, self._urgent_tail = None, b''
            if self._anchor_probe_index >= len(self._anchor_probes or ()):
                self._prepare_anchor_probes()
            if self._urgent_index < len(self._urgent_regions):
                self.complete = False
        return True

    def _begin_cold_pass(self, now):
        # Before the first proven active camera there are no useful same-
        # session camera addresses. Revisit one bounded allocation window so a
        # camera written behind the full cursor can be found on the first map.
        # A prefix plus rotating slice also covers late camera construction at
        # offsets other than the optional allocation-relative anchor probe.
        if self._has_active_hint or not self.regions:
            return
        if self._urgent_index < len(self._urgent_regions) or now < self._next_cold_visit:
            return
        regions = sorted((r for r in self.regions if r['size'] <= 128*1024*1024), key=lambda r: (
            not 16*1024*1024 <= r['size'] <= 128*1024*1024,
            self._priority(r)))
        if not regions:
            return
        region = regions[self._cold_cursor % len(regions)]
        self._cold_cursor += 1
        base, size = region['base'], region['size']
        prefix = min(size, COLD_WINDOW_BYTES)
        windows = [(base, prefix)]
        if prefix < size <= 128*1024*1024:
            offset = self._cold_offsets.get(base, prefix)
            windows.append((base+offset, min(COLD_WINDOW_BYTES, size-offset)))
            self._cold_offsets[base] = (prefix if offset+COLD_WINDOW_BYTES >= size
                                        else offset+COLD_WINDOW_BYTES)
        self._urgent_regions = [{'base': address, 'size': length} for address, length in windows]
        self._urgent_index = 0
        self._urgent_address, self._urgent_tail = None, b''
        self._next_cold_visit = now+COLD_REVISIT_SECONDS
        self.cold_passes += 1

    def _begin_hot_pass(self, process, now, *, metadata_is_fresh=False):
        if not (self.hot_anchors or self.hot_ranges or self.hot_regions):
            return
        if not metadata_is_fresh:
            self.regions = self._metadata(process)
        # Anchor windows are narrow, independent of the historical region's
        # current base/size. Before learning an anchor, the explicit base hint
        # gets a bounded forward window containing the observed camera pools.
        # Learned geometry anchors refine a search but must never suppress an
        # independent same-session region hint. In particular the persistent
        # *inactive* camera can be 60 MiB from the later allocated live camera.
        # Replacing all region windows with that inactive anchor made the live
        # allocation invisible until a multi-GiB full sweep happened to reach
        # it. Revisit the union, using current metadata to include new commits.
        windows = ([(a-HOT_RADIUS, a+HOT_RADIUS) for a in reversed(self.hot_anchors)]
                   + [(a-HOT_RADIUS, a+4*HOT_RADIUS) for a in self._hot_region_order])
        # A long-lived process can have thousands of eligible metadata records.
        # The ordering is identical for every window; evaluating its hint-based
        # priority for each window multiplied Python work without adding reads.
        ordered_regions = sorted(self.regions, key=self._priority)
        intervals = []
        for start, end in windows:
            for region in ordered_regions:
                low, high = max(start, region['base']), min(end, region['base']+region['size'])
                if low < high:
                    intervals.append((low, high))
        # Avoid rereading overlapping windows in one hot pass. Retain the
        # closest/latest anchor's interval order for the first local probes.
        unique, covered = [], []
        for low, high in intervals:
            parts = [(low, high)]
            for start, end in covered:
                next_parts = []
                for a, b in parts:
                    if b <= start or end <= a:
                        next_parts.append((a, b))
                    else:
                        if a < start:
                            next_parts.append((a, start))
                        if end < b:
                            next_parts.append((end, b))
                parts = next_parts
            unique.extend({'base': a, 'size': b-a} for a, b in parts)
            covered.append((low, high))
        self._hot_regions = unique
        self._hot_index, self._hot_address, self._hot_tail = 0, None, b''
        self._next_hot_visit = now+HOT_REVISIT_SECONDS
        self.hot_passes += 1

    def contains(self, address, size):
        """The whole coherent read must be covered by current eligible ranges.

        Adjacent eligible VirtualQueryEx records are allowed after a split;
        gaps, guards and non-private ranges remain forbidden.
        """
        cursor, end = address, address+size
        for region in sorted(self.regions or (), key=lambda r: r['base']):
            low, high = region['base'], region['base']+region['size']
            if high <= cursor:
                continue
            if low > cursor:
                return False
            cursor = max(cursor, high)
            if cursor >= end:
                return True
        return False

    def step(self, process, now=None, byte_limit=MAX_STEP_BYTES):
        now = time.monotonic() if now is None else now
        if process.identity.get('session_key') != self.session_key:
            self.reset(drop_hot=True)
            self.reason = 'session_mismatch'
            return []
        if type(byte_limit) is not int or not 0 < byte_limit <= MAX_STEP_BYTES:
            raise ValueError('locator payload step exceeds 2MiB')
        if now >= self._next_metadata_refresh:
            self._refresh_metadata(process, now)
        if self.regions is None:
            self.reason = 'locator_metadata_retry_pending'
            return []
        if self.complete:
            return []
        found, consumed = [], 0
        started = time.monotonic()
        hot_pending = self._hot_index < len(self._hot_regions)
        if not hot_pending and now >= self._next_hot_visit:
            self._begin_hot_pass(process, now, metadata_is_fresh=True)
        hot_pending = self._hot_index < len(self._hot_regions)
        # Probe a bounded set of allocation-relative anchors before streaming
        # multi-GiB regions. Successful inactive geometry only narrows search.
        for _ in range(MAX_ANCHOR_PROBES_PER_STEP):
            if (time.monotonic()-started > .04 or hot_pending
                    or self._anchor_probe_index >= len(self._anchor_probes or ())
                    or consumed+READ_SIZE > byte_limit):
                break
            address = self._anchor_probes[self._anchor_probe_index]
            self._anchor_probe_index += 1
            raw = process.read(address-READ_BEFORE, READ_SIZE)
            consumed += READ_SIZE
            self.read_bytes += READ_SIZE
            self.anchor_probe_reads += 1
            self.anchor_probe_read_bytes += READ_SIZE
            if raw is None or len(raw) != READ_SIZE:
                self.read_failures += 1
                self.last_read_failure_address = address-READ_BEFORE
                continue
            hint = decode_camera(raw, address-READ_BEFORE, address, now,
                                 allow_inactive_hint=True)
            if hint is not None:
                self.anchor_probe_hits += 1
                self.remember(address, active=False)
                found.append(address)
                self._next_hot_visit = 0.0
        hot_pending = self._hot_index < len(self._hot_regions)
        if not hot_pending and now >= self._next_hot_visit:
            self._begin_hot_pass(process, now, metadata_is_fresh=True)
        if self._hot_index >= len(self._hot_regions):
            self._begin_cold_pass(now)
        while consumed < byte_limit:
            if time.monotonic()-started > .04:
                break
            urgent = self._urgent_index < len(self._urgent_regions)
            full = self.index < len(self._scan_regions)
            if urgent and not (full and self._urgent_full_turn):
                kind, prefix = 'urgent', '_urgent'
            elif self._hot_index < len(self._hot_regions) and not urgent:
                kind, prefix = 'hot', '_hot'
            elif full:
                kind, prefix = 'full', None
            else:
                break
            if prefix:
                region = getattr(self, prefix+'_regions')[getattr(self, prefix+'_index')]
                address = getattr(self, prefix+'_address')
                address = region['base'] if address is None else address
                tail = getattr(self, prefix+'_tail')
            else:
                region = self._scan_regions[self.index]
                address = self.address if self.address is not None else region['base']
                tail = self.tail
            end = region['base']+region['size']
            size = min(512*1024, byte_limit-consumed, end-address)
            if size <= 0:
                if prefix:
                    setattr(self, prefix+'_index', getattr(self, prefix+'_index')+1)
                    setattr(self, prefix+'_address', None)
                    setattr(self, prefix+'_tail', b'')
                else:
                    self.index += 1
                    self.address, self.tail = None, b''
                continue
            blob = process.read(address, size)
            consumed += size
            self.read_bytes += size
            if kind == 'hot':
                self.hot_read_bytes += size
            if blob is not None and len(blob) == size:
                data = tail+blob
                found.extend(candidate_addresses(data, address-len(tail)))
                tail = data[-44:]
            else:
                tail = b''
                self.read_failures += 1
                self.last_read_failure_address = address
            next_address = address+size
            if prefix:
                setattr(self, prefix+'_address', next_address)
                setattr(self, prefix+'_tail', tail)
                if next_address >= end:
                    setattr(self, prefix+'_index', getattr(self, prefix+'_index')+1)
                    setattr(self, prefix+'_address', None)
                    setattr(self, prefix+'_tail', b'')
                    if kind == 'hot' and self._hot_index >= len(self._hot_regions):
                        # A multi-step hot pass can take longer than its
                        # nominal period. Leave a full-scan progress interval
                        # after it finishes, preventing hint flood starvation.
                        self._next_hot_visit = max(self._next_hot_visit, now+HOT_REVISIT_SECONDS)
            else:
                self.address, self.tail = next_address, tail
                if next_address >= end:
                    self.index += 1
                    self.address, self.tail = None, b''
            if urgent:
                self._urgent_full_turn = kind != 'full'
        self.complete = (self._anchor_probe_index >= len(self._anchor_probes or ())
                         and self.index >= len(self._scan_regions)
                         and self._hot_index >= len(self._hot_regions)
                         and self._urgent_index >= len(self._urgent_regions)
                         and self.metadata_refresh_error is None)
        self.reason = 'locator_complete' if self.complete else 'locator_scanning'
        return list(dict.fromkeys(found))[:MAX_CANDIDATES]

    def remember(self, address, *, active=True):
        containing = next((region for region in self.regions or ()
                           if region['base'] <= address < region['base']+region['size']), None)
        if containing is None:
            return False
        if not active:
            if address in self.hot_anchors:
                return False
            if (len(self.inactive_hint_regions) >= MAX_INACTIVE_HINT_REGIONS
                    or containing['base'] in self.inactive_hint_regions.values()):
                return False
            self.inactive_hint_regions[address] = containing['base']
        else:
            self.inactive_hint_regions.pop(address, None)
            self._has_active_hint = True
        if address in self.hot_anchors:
            self.hot_anchors.remove(address)
        if active:
            self.hot_anchors.append(address)
        else:
            self.hot_anchors.insert(0, address)  # Confirmed active anchors win.
        self.hot_anchors = self.hot_anchors[-4:]
        base = containing['base']
        if active:
            if base in self._hot_region_order:
                self._hot_region_order.remove(base)
            self._hot_region_order.insert(0, base)
            self._hot_region_order = self._hot_region_order[:MAX_HOT_REGIONS]
            self.hot_regions = set(self._hot_region_order)
        elif base not in self.hot_regions and len(self._hot_region_order) < MAX_HOT_REGIONS:
            # Geometry-only hints may fill spare slots, but cannot evict an
            # independently cached region. Their narrow anchor window remains
            # available even when the region hint cache is full.
            self._hot_region_order.append(base)
            self.hot_regions.add(base)
        interval = (containing['base'], containing['base']+containing['size'])
        if interval not in self.hot_ranges:
            self.hot_ranges.append(interval)
            self.hot_ranges = self.hot_ranges[-8:]
        return True

    def diagnostics(self):
        return {'hot_passes': self.hot_passes, 'hot_read_bytes': self.hot_read_bytes,
                'hot_pass_interval_count': len(self._hot_regions),
                'hot_pass_interval_index': self._hot_index,
                'hot_pass_remaining_bytes': sum(
                    max(0, interval['base']+interval['size']-(self._hot_address
                        if i == self._hot_index and self._hot_address is not None else interval['base']))
                    for i, interval in enumerate(self._hot_regions) if i >= self._hot_index),
                'hot_region_bases': sorted(self.hot_regions),
                'hot_ranges': [list(r) for r in self.hot_ranges],
                'hot_search_anchors': list(self.hot_anchors),
                'inactive_geometry_hint_regions': dict(self.inactive_hint_regions),
                'metadata_queries': self.metadata_queries,
                'metadata_refresh_error': self.metadata_refresh_error,
                'metadata_refresh_failures': self.metadata_refresh_failures,
                'metadata_failure_streak': self._metadata_failure_streak,
                'metadata_query_budget_seconds': min(4.0, 1.0+self._metadata_failure_streak),
                'next_metadata_refresh_monotonic': self._next_metadata_refresh,
                'incremental_interval_count': len(self._urgent_regions),
                'incremental_interval_index': self._urgent_index,
                'cold_passes': self.cold_passes,
                'allocation_anchor_probe_reads': self.anchor_probe_reads,
                'allocation_anchor_probe_read_bytes': self.anchor_probe_read_bytes,
                'allocation_anchor_probe_hits': self.anchor_probe_hits,
                'allocation_anchor_probes_remaining': max(0, len(self._anchor_probes or ())-self._anchor_probe_index),
                'scan_address': self.address, 'hot_scan_address': self._hot_address,
                'read_failures': self.read_failures,
                'last_read_failure_address': self.last_read_failure_address}


class CameraTracker:
    """``step(injected_process, now) -> unverified candidate | None``.

    All payload reads in a step, including discovery and coherent observations,
    total <=2MiB. Session hints are never accepted across creation identities.
    Multiple structurally valid AND active candidates produce ambiguity/None.
    """
    def __init__(self, session_key, package_version=SUPPORTED_VERSION, hot_regions=()):
        if not isinstance(session_key, str) or not session_key:
            raise ValueError('session_key required')
        self.session_key, self.package_version = session_key, package_version
        self.locator = CameraLocator(session_key, hot_regions)
        self.pending = {}
        self.camera_address = None
        self._next_rescan = None
        self._started = self._search_started = self._first_structure = None
        self._first_active = self._last_active = None
        self._sample_now = None
        self.reason = 'camera_not_started'
        self.status = {'reason': self.reason, 'projection_verified': False}

    def _status(self, reason, **fields):
        self.reason = reason
        self.status = {'reason': reason, 'projection_verified': False,
                       'candidate_count': len(self.pending),
                       'locator_complete': self.locator.complete,
                       'locator_read_bytes': self.locator.read_bytes,
                       'timing': {'basis': 'sampler session start or last active-camera loss; map-open time is not independently known',
                                  'session_search_started_monotonic': self._started,
                                  'current_search_started_monotonic': self._search_started,
                                  'first_structure_monotonic': self._first_structure,
                                  'first_active_camera_monotonic': self._first_active,
                                  'last_active_camera_monotonic': self._last_active,
                                  'search_elapsed_seconds': (None if self._search_started is None or self._sample_now is None
                                                             else max(0.0, self._sample_now-self._search_started)),
                                  'target_seconds': ACQUISITION_TARGET_SECONDS},
                       'locator_diagnostics': self.locator.diagnostics(), **fields}

    def step(self, process, now=None):
        now = time.monotonic() if now is None else now
        if self.package_version != SUPPORTED_VERSION:
            self.pending.clear()
            self.camera_address = None
            self._status('unsupported_build')
            return None
        if process.identity.get('session_key') != self.session_key:
            self.pending.clear()
            self.camera_address = None
            self.locator.reset(drop_hot=True)
            self._status('session_mismatch')
            return None
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            self.pending.clear()
            self.camera_address = None
            self.locator.reset()
            self._next_rescan = None
            self._status('invalid_sample_time')
            return None
        self._sample_now = now
        if self._started is None:
            self._started = self._search_started = now
        try:
            # A completed scan while the map is closed cannot be permanent:
            # opening the map later allocates a new camera. Re-enumerate after
            # a short backoff, retaining only same-session hot region hints.
            if self.camera_address is None and self.locator.complete:
                if self._next_rescan is None:
                    self._next_rescan = now+RESCAN_BACKOFF
                elif now >= self._next_rescan:
                    self.locator.reset()
                    self._next_rescan = None
            # Once a structural candidate exists, its second activity sample
            # has a strict 250 ms deadline. Sampling it must precede another
            # potentially slow metadata query or a heap discovery step.
            if self.camera_address is None and not self.pending:
                reserve = MAX_CANDIDATES*READ_SIZE
                for address in self.locator.step(process, now, MAX_STEP_BYTES-reserve):
                    if len(self.pending) < MAX_CANDIDATES:
                        self.pending.setdefault(address, ClockActivity())
            active, invalid, frozen, restarting = [], [], [], []
            for address, activity in list(self.pending.items()):
                if not self.locator.contains(address-READ_BEFORE, READ_SIZE):
                    invalid.append(address)
                    continue
                blob = process.read(address-READ_BEFORE, READ_SIZE)
                sample = decode_camera(blob, address-READ_BEFORE, address, now) if blob is not None else None
                if sample is None:
                    hint = (decode_camera(blob, address-READ_BEFORE, address, now,
                                          allow_inactive_hint=True) if blob is not None else None)
                    if hint is not None:
                        self.locator.remember(address, active=False)
                    invalid.append(address)
                    continue
                self.locator.remember(address, active=False)
                if self._first_structure is None:
                    self._first_structure = now
                if activity.observe(sample['activity_clock'], now):
                    sample.update(activity_sample_monotonic=activity.last_change,
                                  session_key=self.session_key,
                                  package_version=self.package_version,
                                  reason='camera_geometry_and_clock_active')
                    active.append(sample)
                elif activity.reason == 'activity_frozen':
                    frozen.append(address)
                elif activity.reason in ('activity_sample_gap', 'activity_time_reversed'):
                    # observe() has already established a new first sample.
                    # Retain the coherent candidate for a prompt second read,
                    # rather than throwing it back into slow heap discovery.
                    restarting.append(address)
            for address in invalid:
                self.pending.pop(address, None)
            for address in frozen:
                self.pending.pop(address, None)
            if self.camera_address in invalid:
                self.camera_address = None
                self._search_started = now
                self.locator.reset()  # Same-session hot metadata hints only.
                self._next_rescan = None
            if self.camera_address in frozen:
                self.camera_address = None
                self._search_started = now
                self.locator.reset()
                self._next_rescan = None
            if self.camera_address in restarting:
                self.camera_address = None
                self._search_started = now
            if len(active) > 1:
                self.camera_address = None
                self._status('ambiguous_active_cameras', active_addresses=[s['camera_address'] for s in active])
                return None
            if len(active) == 1:
                sample = active[0]
                self.camera_address = sample['camera_address']
                if self._first_active is None:
                    self._first_active = now
                self._last_active = now
                self._next_rescan = None
                self.locator.remember(self.camera_address)
                self._status('camera_geometry_and_clock_active', camera_address=self.camera_address)
                return sample
            if self.camera_address is not None:
                self._status(self.pending[self.camera_address].reason, camera_address=self.camera_address)
            elif frozen:
                self._status('activity_frozen_relocating')
            elif invalid:
                self._status('camera_structure_or_exact_read_invalid')
            elif self.pending:
                self._status('activity_waiting_second_distinct_sample')
            else:
                self._status('camera_not_found' if self.locator.complete else 'camera_discovery_in_progress')
            return None
        except (OSError, TimeoutError, ProcessLookupError, ValueError) as error:
            self.pending.clear()
            self.camera_address = None
            # A transient exact-read failure invalidates the frame, but does
            # not erase completed full-scan progress or bounded retry state.
            self.locator.complete = False
            self._next_rescan = None
            self._status('camera_read_error', error=str(error))
            return None
