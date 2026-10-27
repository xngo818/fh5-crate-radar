"""FH5 provider for the recovered project's panel and radar.

Process memory is queried/read only. One provider owns the UDP socket. World
coverage, the reference height and row identities are not inferred from counts.
"""
from __future__ import annotations

from copy import deepcopy
from bisect import bisect_right
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


def recent_ranges(existing, additions, *, byte_limit=8 * 1048576, range_limit=256):
    """Keep recent page-aligned search hints bounded, including in memory."""
    kept, remaining = [], byte_limit
    for low, high in list(reversed(additions)) + list(existing):
        if (type(low) is not int or type(high) is not int
                or not 0x10000 < low < high < 0x7fffffffffff
                or low % 4096 or high % 4096):
            continue
        # Subtract newer coverage before adding old bytes. Expanding an older
        # overlapping range and then truncating its high end could evict the
        # very newest slot when the byte cap is reached.
        fragments = [[low, high]]
        for begin, end in kept:
            fragments = [part for a, b in fragments for part in
                         ([[a, b]] if end <= a or begin >= b else
                          ([[a, begin]] if a < begin else []) + ([[end, b]] if end < b else []))]
        for begin, end in fragments:
            size = min(end - begin, remaining) & ~4095
            if size:
                remaining -= size
                finish = begin + size
                # Existing fragments are disjoint and nonadjacent. Only the
                # two boundaries can join older hints; one linear pass avoids
                # re-sorting and comparing every merged range to every hint.
                touching = [i for i, pair in enumerate(kept)
                            if pair[1] == begin or finish == pair[0]]
                if touching:
                    low = min([begin] + [kept[i][0] for i in touching])
                    high = max([finish] + [kept[i][1] for i in touching])
                    position = min(touching)
                    kept = [pair for i, pair in enumerate(kept) if i not in touching]
                    kept.insert(position, [low, high])
                else:
                    kept.append([begin, finish])
            if len(kept) >= range_limit or remaining < 4096:
                return kept[:range_limit]
    return kept


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

    def checkpoint(self):
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise StepEnded()

    def __call__(self, address, size):
        self.checkpoint()
        if size > self.remaining:
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
        self._clear_chunk()

    def _clear_chunk(self):
        self._chunk_address = None
        self._chunk_data = None
        self._chunk_size = 0
        self._chunk_state = {}
        self._chunk_allocation = None

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
                if self._chunk_address != address:
                    data = reader(address, size)
                    self.attempted += size
                    self._chunk_address, self._chunk_data = address, data
                    self._chunk_size = size
                    self._chunk_state = {}
                    self._chunk_allocation = region.get('allocation_base')
                    if data is None:
                        self.failed += 1
                # Preserve both payload and the current marker/stride on a
                # deadline. Restarting a dense chunk could otherwise never
                # reach its later candidates, regardless of total scan time.
                if self._chunk_data is not None:
                    found.extend(candidates_from_chunk(reader, address, self._chunk_data,
                                                       state=self._chunk_state,
                                                       check_budget=getattr(reader, 'checkpoint', None)))
                self.address = (high if address + self._chunk_size >= high
                                else address + self._chunk_size - 32)
                self._clear_chunk()
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
                'scan_address': (self.address or self.regions[self.index]['base']
                                 if self.regions and self.index < len(self.regions) else None),
                'pending_hit_offset': self._chunk_state.get('position'),
                'pending_stride_index': self._chunk_state.get('stride_index'),
                'complete_world_coverage': False}

    def reconcile(self, regions):
        """Refresh eligible metadata without restarting the global frontier.

        New/changed regions are returned for a separate revisit lane, including
        allocations below the retained frontier. This does not validate rows.
        """
        regions = sorted(regions, key=lambda region: region['base'])
        if self.regions is None:
            self.reset(regions)
            return []
        old = {r['base']: r for r in self.regions}
        changed = [r for r in regions if old.get(r['base']) != r]
        if self.index < len(self.regions):
            frontier = self.address or self.regions[self.index]['base']
        else:
            frontier = max((r['base'] + r['size'] for r in self.regions), default=0)
        self.regions = deepcopy(regions)
        self.index = next((i for i, r in enumerate(self.regions)
                           if r['base'] + r['size'] > frontier), len(self.regions))
        self.address = (max(frontier, self.regions[self.index]['base'])
                        if self.index < len(self.regions) else None)
        self.complete = self.index >= len(self.regions)
        if (self._chunk_address is not None and
                (self.complete or self.regions[self.index].get('allocation_base') != self._chunk_allocation
                 or self._chunk_address < self.regions[self.index]['base']
                 or self._chunk_address + len(self._chunk_data or b'') >
                    self.regions[self.index]['base'] + self.regions[self.index]['size'])):
            self._clear_chunk()
        return changed

    def enqueue(self, regions, *, limit=256, byte_limit=8 * 1048576):
        """Append work without losing a partially scanned current chunk."""
        if self.complete:
            self.reset([])
        pending = self.regions[self.index:] if self.regions else []
        known = {(r['base'], r.get('allocation_base')): r for r in pending}
        additions, accepted = [], {}
        remaining = max(0, byte_limit - sum(r['size'] for r in pending))
        for region in regions:
            if remaining <= 0 or len(pending) + len(additions) >= limit:
                break
            key = region['base'], region.get('allocation_base')
            if key in known:
                accepted[key] = min(region['size'], known[key]['size'])
                continue
            if key not in known and remaining > 0 and len(pending) + len(additions) < limit:
                # The planner retains each owner's remaining extent and
                # supplies bounded consecutive windows, never just its head.
                clipped = deepcopy(region)
                clipped['size'] = min(region['size'], remaining, 2 * 1048576)
                if 'end' in clipped:
                    clipped['end'] = clipped['base'] + clipped['size']
                additions.append(clipped)
                known[key] = clipped
                accepted[key] = clipped['size']
                remaining -= clipped['size']
        self.regions = pending + additions[:max(0, limit - len(pending))]
        self.index = 0
        self.complete = not self.regions
        return accepted

    def prune(self, regions):
        """Discard obsolete priority metadata, retaining an eligible cursor."""
        pending = self.regions[self.index:] if self.regions and not self.complete else []
        bases = [r['base'] for r in regions]
        retained = []
        for region in pending:
            index = bisect_right(bases, region['base']) - 1
            if index < 0:
                continue
            current = regions[index]
            if (region['base'] + region['size'] <= current['base'] + current['size']
                    and region.get('allocation_base') == current.get('allocation_base')):
                retained.append(region)
        if not pending or not retained or retained[0] is not pending[0]:
            self.address = None
            self._clear_chunk()
        self.regions, self.index = retained, 0
        self.complete = not retained


def region_shape(region):
    """Address-free ordering hint; never authorizes a memory read or a row."""
    size, kind, protect = (region.get(key) for key in ('size', 'type', 'protect'))
    if (type(size) is int and 256 * 1024 <= size <= 16 * 1048576
            and size % 4096 == 0 and type(kind) is int and type(protect) is int
            and kind == 0x20000 and protect == 4):
        return size, kind, protect
    return None


class DiscoveryPlanner:
    """Bounded local recovery plus a continuing global sweep.

    All hints are session-local metadata. Only CandidatePool's complete
    before/after read can produce a sample. An in-progress empty-world sweep
    keeps bounded fast slices; cooldown increases only between complete empty
    passes, so a later match cannot inherit a slow unfinished global scan.
    """
    def __init__(self, ranges, now, region_shapes=()):
        self.ranges = recent_ranges([], ranges)
        self.locator = Locator()
        self.priority = Locator([])
        self.preferred = Locator([])
        self._preferred_regions = []
        self.preferred_passes = 0
        self.next_preferred = 0.0
        self._preferred_turn = True
        self.scope = Locator(self._scope_regions()) if self.ranges else None
        self.recent_slots = []
        self.next_metadata = self.next_scope = self.next_global = self.next_priority = 0.0
        self.metadata_at = None
        self.metadata_refreshes = self.full_scan_generation = 0
        self.metadata_refresh_error = None
        self.metadata_refresh_failures = 0
        self._metadata_failure_streak = 0
        self._region_bases = []
        self.last_fresh = None
        self.empty_since = now
        self.burst_until = now + 6.0
        self.reason = 'initial_discovery'
        self.mode = 'recovery_burst'
        self._revisit_recent = False
        self._priority_turn = True
        self._global_steps_since_priority = 0
        self._search_pass_pending = True
        self._empty_passes = 0
        # References only to current eligible metadata, never coordinate
        # samples. Its size is bounded by the native region-query cap and it is
        # pruned on each metadata refresh. Payload work remains <=8 MiB/256.
        self._deferred = {}
        self.region_shapes = list(dict.fromkeys(tuple(s) for s in region_shapes))[:16]
        self.region_shapes_dirty = False
        self.cold_rounds = 0
        self.next_cold = 0.0

    def _scope_regions(self):
        return [{'base': low, 'size': high - low} for low, high in sorted(self.ranges)]

    def request_recovery(self, now, reason):
        self.burst_until = max(self.burst_until, now + 3.0)
        self.next_metadata = self.next_scope = self.next_priority = self.next_global = 0.0
        self.next_preferred = 0.0
        self.reason = reason
        self._revisit_recent = True
        self._search_pass_pending = True
        self._empty_passes = 0

    def _nearby_ranges(self, first):
        regions = self.locator.regions or []
        index = bisect_right(self._region_bases, first) - 1
        if index < 0 or first >= regions[index]['base'] + regions[index]['size']:
            return []
        owner = regions[index]
        allocation = owner.get('allocation_base')
        low, high = (first - 128 * 1024) & ~4095, (first + 128 * 1024 + 4095) & ~4095
        hints = []
        start = max(0, bisect_right(self._region_bases, low) - 1)
        for region in regions[start:]:
            if region['base'] >= high:
                break
            # Only enumerated eligible pages in the same allocation are hints;
            # no guessed object pointers, protected-page fallbacks or writes.
            if region is not owner and (not allocation or region.get('allocation_base') != allocation):
                continue
            begin, end = max(low, region['base']), min(high, region['base'] + region['size'])
            if begin < end:
                hints.append([begin, end])
        return hints

    def observe_rows(self, rows):
        additions = []
        for row in rows:
            first = row.get('first_string_address')
            if type(first) is not int or not 0x10000 < first < 0x7fffffffffff or first % 8:
                continue
            if first in self.recent_slots:
                self.recent_slots.remove(first)
            self.recent_slots.append(first)
            self.recent_slots = self.recent_slots[-256:]
            index = bisect_right(self._region_bases, first) - 1
            if index >= 0:
                owner = self.locator.regions[index]
                shape = region_shape(owner) if first < owner['base'] + owner['size'] else None
                if shape is not None:
                    # Existing members may change LRU order on every refresh.
                    # Only changed membership needs another persistent hint.
                    self.region_shapes_dirty |= shape not in self.region_shapes
                    self.region_shapes = [shape] + [s for s in self.region_shapes if s != shape]
                    self.region_shapes = self.region_shapes[:16]
            additions.extend(self._nearby_ranges(first))
            additions.extend(ranges_for_candidates([row]))
        updated = recent_ranges(self.ranges, additions)
        if updated == self.ranges:
            return False
        self.ranges = updated
        if self.scope is None:
            self.scope = Locator(self._scope_regions())
        else:
            # Retain a current scan's frontier; newly lower hints are picked up
            # on the next bounded local cycle, not by restarting on every row.
            self.scope.reconcile(self._scope_regions())
        return True

    def _recent_regions(self, regions):
        result, seen = [], set()
        for first in reversed(self.recent_slots):
            index = bisect_right(self._region_bases, first) - 1
            if index >= 0:
                region = regions[index]
                if first < region['base'] + region['size'] and region['base'] not in seen:
                    # Revisit around the last valid object, even when its
                    # allocation is gigabytes and the slot was far from base.
                    nearby = deepcopy(region)
                    nearby['base'] = max(region['base'], (first - 1048576) & ~4095)
                    nearby['size'] = min(region['base'] + region['size'] - nearby['base'], 2 * 1048576)
                    if 'end' in nearby:
                        nearby['end'] = nearby['base'] + nearby['size']
                    result.append(nearby)
                    seen.add(region['base'])
            if len(result) >= 32:
                break
        return result

    def _recent_owner_regions(self, regions):
        owners, seen = [], set()
        for first in reversed(self.recent_slots):
            index = bisect_right(self._region_bases, first) - 1
            if index >= 0:
                region = regions[index]
                if first < region['base'] + region['size'] and region['base'] not in seen:
                    owners.append(region)
                    seen.add(region['base'])
            if len(owners) >= 32:
                break
        return owners

    def _defer_region(self, region):
        key = region['base'], region.get('allocation_base')
        task = self._deferred.get(key)
        cursor = max(region['base'], task['cursor']) if task else region['base']
        if cursor < region['base'] + region['size']:
            self._deferred[key] = {'region': region, 'cursor': cursor}

    def _queue_cold_regions(self, regions, now):
        """Fairly search unmatched small owners alongside a complete sweep.

        Profile matches have an independent resumable revisit lane, so an empty
        pass in free roam cannot strand them behind this general queue when a
        later match creates objects. No previous process address is reused.
        """
        shapes = set(self.region_shapes)
        selected = [r for r in regions
                    if 256 * 1024 <= r['size'] <= 16 * 1048576
                    and not r.get('executable', False)
                    and r.get('type', 0x20000) == 0x20000
                    and region_shape(r) not in shapes]
        selected.sort(key=lambda r: (-r['size'], r['base']))
        self.next_cold = now + 5.0
        for region in selected:
            self._defer_region(region)
        self.cold_rounds += 1

    def _fill_priority(self):
        """Round-robin complete owner extents into a bounded payload queue."""
        while self._deferred:
            pending = (self.priority.regions or [])[self.priority.index:]
            remaining = 8 * 1048576 - sum(region['size'] for region in pending)
            if remaining <= 0 or len(pending) >= 256:
                break
            key = next(iter(self._deferred))
            task = self._deferred.pop(key)
            region, cursor = task['region'], task['cursor']
            end = region['base'] + region['size']
            size = min(2 * 1048576, remaining, end - cursor)
            if size <= 0:
                continue
            if size <= 32 and cursor + size < end:
                self._deferred[key] = task
                break
            window = dict(region, base=cursor, size=size)
            if 'end' in window:
                window['end'] = cursor + size
            accepted = self.priority.enqueue([window]).get(
                (cursor, region.get('allocation_base')), 0)
            if not accepted:
                self._deferred[key] = task
                break
            finish = cursor + accepted
            if finish < end:
                # Overlap adjacent windows just like chunks: an inline needle
                # crossing a window boundary must still be considered.
                task['cursor'] = finish - 32 if accepted > 32 else cursor
                self._deferred[key] = task
                if accepted <= 32:
                    break

    def step(self, process, stop, *, fresh_rows, now):
        if self.last_fresh is True and not fresh_rows:
            self.empty_since = now
            self.request_recovery(now, 'fresh_rows_lost')
        elif fresh_rows:
            self.empty_since = None
            self._search_pass_pending = False
            self._empty_passes = 0
            if self.last_fresh is not True:
                self.reason = 'fresh_rows_available'
        self.last_fresh = bool(fresh_rows)
        # Back off between completed empty passes, never part-way through an
        # unvisited heap. The reader may have attached in free roam before the
        # first Eliminator match, with no previous fresh rows to signal a loss.
        if not fresh_rows and not self._search_pass_pending and now >= self.next_global:
            self._search_pass_pending = True
        self.mode = ('tracking' if fresh_rows else
                     'recovery_burst' if self._search_pass_pending and now < self.burst_until else
                     'recovery_sweep' if self._search_pass_pending else 'empty_backoff')
        burst = self.mode in ('recovery_burst', 'recovery_sweep')
        if now >= self.next_metadata:
            try:
                regions = sorted(process.regions(deadline=time.monotonic() +
                                  (2.0 if self.metadata_at is None else 1.0)),
                                 key=lambda region: region['base'])
            except TimeoutError as error:
                # An incomplete metadata refresh is not a new process or a
                # rejected row. Retain the last complete metadata and its scan
                # frontier; every later payload read/row still validates itself.
                if self.locator.regions is None:
                    raise
                self.metadata_refresh_error = str(error)[:512]
                self.metadata_refresh_failures += 1
                self._metadata_failure_streak += 1
                self.next_metadata = time.monotonic() + min(16.0, 2.0 ** min(4, self._metadata_failure_streak))
            else:
                changed = self.locator.reconcile(regions)
                self._region_bases = [r['base'] for r in regions]
                # Shape hints select only this process's freshly enumerated
                # eligible metadata. Whole matching owners remain resumable;
                # a bounded slice never limits a pass to each owner's head.
                shapes = set(self.region_shapes)
                preferred_regions = [r for r in regions
                                     if not r.get('executable', False)
                                     and region_shape(r) in shapes]
                preferred_changed = self.preferred.reconcile(preferred_regions)
                self._preferred_regions = preferred_regions
                if preferred_changed and self.preferred.complete:
                    self.preferred.reset(preferred_regions)
                    self.next_preferred = 0.0
                self.priority.prune(regions)
                eligible = {(r['base'], r.get('allocation_base')): r for r in regions}
                self._deferred = {key: {'region': eligible[key], 'cursor': max(eligible[key]['base'], task['cursor'])}
                                  for key, task in self._deferred.items()
                                  if key in eligible and task['cursor'] < eligible[key]['base'] + eligible[key]['size']}
                # Recent allocations precede metadata deltas after a loss; an
                # initial full region snapshot never floods the priority queue.
                if self._revisit_recent:
                    self.priority.enqueue(self._recent_regions(regions))
                    for region in self._recent_owner_regions(regions):
                        self._defer_region(region)
                    self._revisit_recent = False
                for region in changed:
                    self._defer_region(region)
                if (not fresh_rows and time.monotonic() >= self.next_cold
                        and not self._deferred
                        and (self.priority.complete or not self.priority.regions)):
                    self._queue_cold_regions(regions, time.monotonic())
                self.metadata_at = time.monotonic()
                self.metadata_refreshes += 1
                self.metadata_refresh_error = None
                self._metadata_failure_streak = 0
                self.next_metadata = self.metadata_at + (2.0 if burst else 5.0)
        # Oversized metadata deltas must not vanish just because a bounded
        # payload queue was full during their one 'changed' generation. Keep
        # their metadata references and refill as earlier windows complete.
        self._fill_priority()
        found = []
        if self.scope is not None and now >= self.next_scope:
            rows, _ = self.scope.step(process, stop, seconds=.015, byte_limit=1048576)
            found.extend(rows)
            if self.scope.complete:
                self.scope.reset(self._scope_regions())
                self.next_scope = time.monotonic() + (0.5 if burst else 2.0)
        # Preferred and general work alternate inside the existing auxiliary
        # lane. Both together receive one slice per three global slices during
        # discovery, so recurring preferred passes cannot starve either the
        # general cold queue or the complete global frontier.
        priority_ready = (not self.priority.complete and self.priority.regions
                          and now >= self.next_priority)
        preferred_ready = (not self.preferred.complete and self.preferred.regions
                           and now >= self.next_preferred)
        auxiliary_ready = priority_ready or preferred_ready
        global_ready = now >= self.next_global
        if auxiliary_ready and (not global_ready or
                (self._global_steps_since_priority >= 3 if burst else self._priority_turn)):
            use_preferred = preferred_ready and (not priority_ready or self._preferred_turn)
            auxiliary = self.preferred if use_preferred else self.priority
            rows, _ = auxiliary.step(process, stop,
                seconds=.025 if burst else .015, byte_limit=2 * 1048576)
            found.extend(rows)
            next_slice = time.monotonic() + (.02 if burst else .2 if not fresh_rows else .5)
            if use_preferred:
                self.next_preferred = next_slice
                if self.preferred.complete:
                    self.preferred_passes += 1
                    self.preferred.reset(self._preferred_regions)
                    self.next_preferred = time.monotonic() + (1.0 if not fresh_rows else 5.0)
            else:
                self.next_priority = next_slice
            self._preferred_turn = not use_preferred
            self._priority_turn = False
            self._global_steps_since_priority = 0
        elif global_ready:
            rows, _ = self.locator.step(process, stop,
                seconds=.08 if burst else .02 if not fresh_rows else .01,
                byte_limit=64 * 1048576 if burst else 8 * 1048576 if not fresh_rows else 2 * 1048576)
            found.extend(rows)
            self._priority_turn = True
            self._global_steps_since_priority += 1
            self.next_global = time.monotonic() + (.02 if burst else .2 if not fresh_rows else 1.0)
            if self.locator.complete:
                # A completed sweep may start another pass; metadata changes
                # and row loss never discard an in-progress global frontier.
                self.full_scan_generation += 1
                self._search_pass_pending = False
                self.locator.reset(self.locator.regions)
                if not fresh_rows:
                    self._empty_passes += 1
                self.next_global = time.monotonic() + (min(5.0, 2.0 ** min(3, self._empty_passes - 1))
                                                       if not fresh_rows else 5.0)
        return found

    def status(self, now):
        return {'mode': self.mode, 'reason': self.reason,
                'priority_range_count': max(0, len(self.priority.regions or []) - self.priority.index),
                'priority_bytes': sum(r['size'] for r in (self.priority.regions or [])[self.priority.index:]),
                'deferred_region_count': len(self._deferred),
                'scope_range_count': len(self.ranges),
                'scope_bytes': sum(high - low for low, high in self.ranges),
                'cold_rounds': self.cold_rounds,
                'region_shape_count': len(self.region_shapes),
                'preferred_region_count': len(self._preferred_regions),
                'preferred_passes': self.preferred_passes,
                'preferred_scan_progress': self.preferred.progress(),
                'metadata_refreshes': self.metadata_refreshes,
                'metadata_refresh_error': self.metadata_refresh_error,
                'metadata_refresh_failures': self.metadata_refresh_failures,
                'metadata_age_seconds': now - self.metadata_at if self.metadata_at is not None else None,
                'full_scan_generation': self.full_scan_generation,
                'empty_seconds': now - self.empty_since if self.empty_since is not None else 0.0,
                'burst_remaining_seconds': max(0.0, self.burst_until - now) if self.mode == 'recovery_burst' else 0.0}

    def poll_period(self, normal):
        return normal if self.mode == 'tracking' else .02 if self.mode in ('recovery_burst', 'recovery_sweep') else .2


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
        self._scope_load_status = []
        self._next_scan_status = 0.0

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
                                    ('map_state', 'reason', 'map_frame', 'tracker_status', 'map_timing',
                                     'error', 'alignment_profile_status') if key in sample})
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
        self._scope_load_status = []
        for file in (self.state_dir / 'pool_scope.json', self.seed_scope):
            try:
                cache = json.loads(file.read_text(encoding='utf-8-sig'))
                if cache['session_key'] != identity['session_key'] or cache['package_version'] != version:
                    self._scope_load_status.append({'file': file.name, 'result': 'session_or_version_changed'})
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
                self._scope_load_status.append({'file': file.name, 'result': 'same_session_hint',
                                               'bytes': sum(high - low for low, high in ranges)})
                return merge_ranges(ranges)
            except (OSError, ValueError, KeyError, TypeError):
                self._scope_load_status.append({'file': file.name, 'result': 'missing_or_invalid'})
                continue
        return []

    def _load_region_shapes(self, version):
        shapes = []
        for file in (self.state_dir / 'pool_region_shapes.json',
                     RESOURCE_DIR / 'data' / 'fh5_scan_profile.json'):
            try:
                document = json.loads(file.read_text(encoding='utf-8'))
                if not isinstance(document, dict):
                    continue
                if document.get('package_version') != version:
                    continue
                for item in document.get('region_shapes', [])[:16]:
                    shape = region_shape(item) if isinstance(item, dict) else None
                    if shape is not None and shape not in shapes:
                        shapes.append(shape)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return shapes[:16]

    def _save_region_shapes(self, version, shapes):
        file = self.state_dir / 'pool_region_shapes.json'
        document = {'schema_version': 1, 'package_version': version,
                    'region_shapes': [dict(zip(('size', 'type', 'protect'), shape)) for shape in shapes[:16]],
                    'note': 'Address-free search ordering only. Enumerate current regions and revalidate every row.'}
        try:
            temporary = file.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
            temporary.replace(file)
            return True
        except OSError:
            return False

    def _write_scan_status(self, *, force=False):
        """One overwritten local status file, also for ordinary shortcut launches."""
        now = time.monotonic()
        if not force and now < self._next_scan_status:
            return
        self._next_scan_status = now + 1.0
        snapshot = self.snapshot()
        document = {key: snapshot.get(key) for key in
                    ('session_key', 'game_version', 'process_pid', 'state', 'memory_state',
                      'last_error', 'scan_progress', 'memory_timing', 'map_state', 'map_reason',
                      'map_timing')}
        # Ordinary desktop launches need camera diagnostics too. Keep one small
        # overwritten file, without frame matrices or the candidate payloads.
        map_status = snapshot.get('map_status') or {}
        document.update(map_tracker=map_status.get('tracker_status'),
                        map_error=map_status.get('error'),
                        map_diagnostic_revision='first-map-v10')
        document.update(schema_version=1, revision='cold-start-v5',
                        scope_cache=self._scope_load_status,
                        fresh_row_count=len(snapshot.get('rows') or []))
        file = self.state_dir / 'scan_status.json'
        try:
            temporary = file.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.replace(file)
        except OSError:
            pass

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
        planner = None
        pool = None
        next_version_check = 0.0
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
                            self._write_scan_status()
                            self.stop.wait(1)
                            continue
                        process = self._process_factory(matches[0]['pid'])
                        version = package_version(process.identity['image_path'])
                        self.store.set_session(process.identity, game_version=version)
                        self.store.set_status(last_error=None)
                        pool = CandidatePool(process.identity['session_key'], version)
                        attached_at, first_rows_at = time.monotonic(), None
                        planner = DiscoveryPlanner(self._load_scope(process.identity, version), attached_at,
                                                   self._load_region_shapes(version))
                        discovery_started_at, discovery_first_rows_at = attached_at, None
                        next_version_check = started + 10
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
                        self._write_scan_status()
                        self.stop.wait(1)
                        continue
                    if self._rescan.is_set():
                        self._rescan.clear()
                        planner.request_recovery(time.monotonic(), 'requested_rescan')
                        discovery_started_at, discovery_first_rows_at = time.monotonic(), None
                    refreshes = []
                    scope_changed = False
                    if pool.addresses:
                        fresh_before = bool(self.store.source_snapshot()['fresh_row_count'])
                        refresh_seconds = (.18 if fresh_before else
                            .08 if time.monotonic() < planner.burst_until else .02)
                        refresh = pool.refresh(process, self.stop, self.catalog, seconds=refresh_seconds)
                        refreshes.append(refresh)
                        if not process.is_alive():
                            self.store.set_session(None)
                            continue
                        # Apply removals before selecting search speed. A slot
                        # retained after a deadline is not a fresh source row.
                        self.store.publish_row_updates(process.identity['session_key'], refresh['rows'],
                            removed=refresh['removed'], now=refresh['sample_monotonic'])
                        scope_changed |= planner.observe_rows(refresh['rows'])
                    source = self.store.source_snapshot()
                    discovered = planner.step(process, self.stop,
                        fresh_rows=bool(source['fresh_row_count']), now=time.monotonic())
                    if discovered:
                        pool.register(discovered)
                        # Newly found slots can appear in this iteration; there
                        # is no additional polling-period delay before publish.
                        refresh_seconds = (.18 if source['fresh_row_count'] else
                            .08 if time.monotonic() < planner.burst_until else .02)
                        refresh = pool.refresh(process, self.stop, self.catalog, seconds=refresh_seconds)
                        refreshes.append(refresh)
                        if not process.is_alive():
                            self.store.set_session(None)
                            continue
                        self.store.publish_row_updates(process.identity['session_key'], refresh['rows'],
                            removed=refresh['removed'], now=refresh['sample_monotonic'])
                        scope_changed |= planner.observe_rows(refresh['rows'])
                    if scope_changed:
                        self._save_scope(process.identity, version, planner.ranges)
                    if (planner.region_shapes_dirty
                            and self._save_region_shapes(version, planner.region_shapes)):
                        planner.region_shapes_dirty = False
                    if not process.is_alive():
                        self.store.set_session(None)
                        continue
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
                        memory_state=message if rows else ('观察池无有效候选' if planner.ranges else '正在定位车包池'),
                        scope='observed_pool' if rows else 'partial_discovery',
                        scan_progress=planner.locator.progress(),
                        scope_scan_progress=planner.scope.progress() if planner.scope else None,
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
                            'recovery': planner.status(time.monotonic()),
                            'refreshes': [{key: value for key, value in refresh.items()
                                           if key not in ('rows', 'removed')} for refresh in refreshes]})
                    self._write_scan_status()
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    self.store.clear_rows('read_error')
                    if process is not None:
                        process.close()
                        process = None
                    self.store.set_session(None)
                    self.store.set_status(state=f'读取暂停：{error}', memory_state=f'读取错误：{error}',
                                          last_error=str(error))
                    self._write_scan_status(force=True)
                    self.stop.wait(.5)
                target_period = planner.poll_period(self.period) if planner is not None else .2
                delay = max(.005, target_period - (time.monotonic() - started))
                self.wake.wait(delay)
                self.wake.clear()
        finally:
            if process is not None:
                process.close()
            self.store.set_session(None)
            self.store.set_status(state='已停止', memory_state='已停止')
            self._write_scan_status(force=True)

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
