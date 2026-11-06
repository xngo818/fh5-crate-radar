"""Version-checked normal-tier upgrade selection, independent of radar reads.

Imports and preference editing do not open the game. Enabling the service
allows only four-byte pending row-key changes within the same configured tier.
There are no code patches, injection, or cross-tier modes. An independently
enabled experimental route changes only a current level-10 car's pending
same-level choice, with a separate per-current-car preference map.
"""
from __future__ import annotations
from copy import deepcopy
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time
from fh5_paths import RESOURCE_DIR, STATE_DIR

Q = lambda b, o=0: struct.unpack_from('<Q', b, o)[0]
U = lambda b, o=0: struct.unpack_from('<I', b, o)[0]


class UpgradeRecordUnavailable(ValueError):
    def __init__(self, reason, message, retryable=True):
        super().__init__(message)
        self.reason, self.retryable = reason, retryable


class UpgradeCatalog:
    def __init__(self, path=None):
        self.profile = json.loads(Path(path or RESOURCE_DIR/'data/fh5_upgrade_profile.json').read_text(encoding='utf-8'))
        self.rows = {r['key']: r for r in self.profile['rows']}
        self.levels = {n: [r for r in self.rows.values() if r['level'] == n] for n in range(2, 11)}
        if len(self.rows) != 47 or any(len(v) != 5 for v in self.levels.values()):
            raise ValueError('车型配置不完整')

    def validated(self, selections):
        selections = selections if isinstance(selections, dict) else {}
        return {str(n): key if type(key := selections.get(str(n))) is int and
                any(r['key'] == key for r in self.levels[n]) else None for n in range(2, 11)}

    def name(self, key):
        return self.rows.get(key, {}).get('name', '未知车型')

    def validated_level10(self, rules):
        rules = rules if isinstance(rules, dict) else {}
        keys = {r['key'] for r in self.levels[10]}
        return {str(source): target if type(target := rules.get(str(source))) is int
                and target in keys and target != source else None for source in sorted(keys)}


def load_preferences(catalog, path=None):
    try:
        data = json.loads(Path(path or STATE_DIR/'upgrade_choices.json').read_text(encoding='utf-8'))
        return catalog.validated(data.get('selections'))
    except (OSError, ValueError, TypeError, AttributeError):
        return catalog.validated({})


def save_preferences(catalog, selections, path=None):
    path = Path(path or STATE_DIR/'upgrade_choices.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='upgrade_', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            json.dump({'schema_version': 1, 'selections': catalog.validated(selections)}, f, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_level10_preferences(catalog, path=None):
    try:
        data = json.loads(Path(path or STATE_DIR/'level10_swap_choices.json').read_text(encoding='utf-8'))
        return catalog.validated_level10(data.get('rules'))
    except (OSError, ValueError, TypeError, AttributeError):
        return catalog.validated_level10({})


def save_level10_preferences(catalog, rules, path=None):
    path = Path(path or STATE_DIR/'level10_swap_choices.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='level10_', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            json.dump({'schema_version': 1, 'rules': catalog.validated_level10(rules)}, f, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():temporary.unlink()


def plan_level10_change(catalog, chain, rules):
    """A different level-10 car, only in a game-created pending reference."""
    if not chain or catalog.rows[chain[0]['key']]['level'] != 10:
        return None
    target = catalog.validated_level10(rules)[str(chain[0]['key'])]
    if target is None or len(chain) < 2:
        return None
    current, pending = chain[:2]
    if (len(chain) != 2 or pending['address'] == current['address'] or pending['address'] % 8
            or catalog.rows[pending['key']]['level'] != 10 or pending['key'] == current['key']
            or pending['context'] != current['context']):
        raise ValueError('10级待换记录结构异常，未修改车辆')
    if pending['key'] == target:return None
    return {'address': pending['address'], 'before': pending['hex'], 'old_key': pending['key'],
            'new_key': target, 'level': 10, 'mode': 'level10_swap', 'source_key': current['key']}


def plan_changes(catalog, chain, selections):
    """Pure planner: only future nodes with exact adjacent-tier relationships."""
    changes = []
    selections = catalog.validated(selections)
    if not chain:
        return changes
    if len({n['address'] for n in chain}) != len(chain):
        raise ValueError('升级链存在循环')
    for parent, node in zip(chain, chain[1:]):
        current, future = catalog.rows[parent['key']], catalog.rows[node['key']]
        if current['level'] == 10 and future['level'] == 10:
            break  # A separate opt-in route handles the same-level tail.
        if (future['level'] != current['level'] + 1 or
                node['key'] not in current['children'] or node['address'] % 8):
            raise ValueError('升级链不是正常逐级候选，已停止改选')
        key = selections[str(future['level'])]
        if key is None or key == node['key']:
            continue
        if key not in current['children'] or catalog.rows[key]['level'] != future['level']:
            raise ValueError('目标车型不属于该等级候选')
        changes.append({'address': node['address'], 'before': node['hex'],
                        'old_key': node['key'], 'new_key': key, 'level': future['level']})
    return changes


def _main_module(process):
    # XboxGames and WindowsApps may be aliases of the same image. Pair exact
    # PID/basename Toolhelp metadata with the PE/code fingerprint below.
    if process.main_module:
        return process.main_module
    from win_readonly import MODULEENTRY32W, TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32
    api = process._api
    snapshot = api.snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, process.pid)
    try:
        entry = MODULEENTRY32W(); entry.dwSize = C.sizeof(entry)
        ok = api.Module32FirstW(snapshot, C.byref(entry)); matches = []
        while ok:
            if entry.th32ProcessID == process.pid and str(entry.szModule).casefold() == 'forzahorizon5.exe':
                matches.append({'base': int(entry.modBaseAddr), 'size': int(entry.modBaseSize)})
            ok = api.Module32NextW(snapshot, C.byref(entry))
        if len(matches) != 1:
            raise ValueError('无法确认游戏主模块')
        return matches[0]
    finally:
        api.CloseHandle(snapshot)


class LiveUpgradeSession:
    def __init__(self, source, catalog, process_factory=None):
        from win_readonly import Process
        self.catalog, self.profile = catalog, catalog.profile
        self.process = (process_factory or Process)(source['process_pid'])
        self.component_address = None
        self._recent_components = {}
        self.locate_diagnostics = {}
        self._component_scopes = []
        self._scope_windows = None; self._scope_index = 0; self._scope_address = None
        self.scope_scan_bytes = 0; self.full_scan_passes = 0; self.scan_read_failures = 0
        self._ranges = []; self._range_index = 0; self._scan_address = None
        self.scan_bytes = 0
        self._next_region_refresh = 0.0
        self._priority_ranges = []; self._priority_address = None; self._scan_turn = 0
        self._table_context = None; self.tables = {}; self.journal = {}
        try:
            if self.process.identity['session_key'] != source['session_key']:
                raise ValueError('游戏会话已变化')
            self.base = _main_module(self.process)['base']
            h = self.read(self.base, 2048); nt = U(h, 60)
            num = struct.unpack_from('<H', h, nt+6)[0]; opt = struct.unpack_from('<H', h, nt+20)[0]
            start = nt+24+opt
            if hashlib.sha256(h[nt+8:nt+12]+h[start:start+num*40]).hexdigest() != self.profile['pe_fingerprint']:
                raise ValueError('当前游戏构建未通过选车校验')
            for check in self.profile['code_checks']:
                raw = bytes.fromhex(check['hex'])
                if self.read(self.base+check['rva'], len(raw)) != raw:
                    raise ValueError('当前游戏代码未通过选车校验')
            self.vtable = self.base+self.profile['component_vtable_rva']
            self.secondary = self.base+self.profile['secondary_vtable_rva']
            self.descriptor = self.base+self.profile['cars_descriptor_rva']
        except Exception:
            self.process.close()
            raise

    def read(self, address, size):
        raw = self.process.read(address, size)
        if raw is None:
            raise UpgradeRecordUnavailable('read_failed', '升级记录暂不可读，等待重新定位')
        return raw

    def component(self, address):
        b = self.read(address, 0x1f8)
        if Q(b) != self.vtable or Q(b, 16) != self.secondary:
            raise UpgradeRecordUnavailable('component_invalid', '对局组件已失效', False)
        state, control = Q(b, 0xb0), Q(b, 0xb8)
        if not state or not control or not U(self.read(control, 16), 8):
            return {'address': address, 'state': state, 'chain': []}
        state_raw = self.read(state, 0xa0); node = Q(state_raw, 0x88)
        chain = []; seen = set()
        while node:
            if node in seen or len(chain) >= 12:
                raise UpgradeRecordUnavailable('chain_invalid', '升级记录结构异常', False)
            seen.add(node); raw = self.read(node, 48); key = U(raw, 8)
            if Q(raw) != self.descriptor or not raw[12] & 1 or key not in self.catalog.rows:
                raise UpgradeRecordUnavailable('row_unready', '车型引用尚未就绪')
            row = self.catalog.rows[key]
            chain.append({'address': node, 'key': key, 'level': row['level'],
                          'context': Q(raw, 16), 'hex': raw.hex()})
            node = Q(raw, 32)
        return {'address': address, 'state': state, 'chain': chain}

    def _refresh_ranges(self):
        now = time.monotonic()
        if self._ranges and now < self._next_region_refresh:
            return
        cursor = self._scan_address
        cursor_order = None
        exhausted = self._range_index >= len(self._ranges)
        if not exhausted:
            old = self._ranges[self._range_index]
            cursor_order = (old['end']-old['base'] > 16*1048576, cursor or old['base'])
        if cursor is None and self._range_index < len(self._ranges):
            cursor = self._ranges[self._range_index]['base']
        previous = {(r['base'], r['end']) for r in self._ranges}
        try:
            fresh = [r for r in self.process.regions(deadline=now+2) if not r['executable']]
        except TimeoutError:
            self._next_region_refresh = time.monotonic()+2
            # Metadata may be transiently slow under game/radar load. Retain
            # both the existing ranges and the exact full-scan cursor.
            if not self._ranges:
                raise
            return
        self._next_region_refresh = time.monotonic()+5
        fresh.sort(key=lambda r: (r['end']-r['base'] > 16*1048576, r['base']))
        added = [r for r in fresh if (r['base'], r['end']) not in previous and r['end']-r['base'] <= 16*1048576]
        if previous and added:
            self._priority_ranges.extend(r for r in added if r not in self._priority_ranges)
        if self.component_address:
            for r in fresh:
                if r['base'] <= self.component_address < r['end'] and r['end']-r['base'] <= 16*1048576:
                    if r not in self._priority_ranges:
                        self._priority_ranges.append(r)
                    break
        self._ranges = fresh
        if {(r['base'], r['end']) for r in fresh} != previous:
            self._scope_windows = None
        containing = next((i for i,r in enumerate(fresh) if cursor is not None and r['base'] <= cursor < r['end']), None)
        if containing is not None:
            self._range_index, self._scan_address = containing, cursor
        else:
            # Freed/replaced ranges must not reset a multi-GB sweep to zero.
            self._range_index = (len(fresh) if exhausted and previous else
                next((i for i,r in enumerate(fresh) if cursor_order is None or
                      (r['end']-r['base'] > 16*1048576, r['base']) >= cursor_order), len(fresh)))
            self._scan_address = None

    def _remember_component_scope(self, address):
        center = address & ~0xffff
        scopes = getattr(self, '_component_scopes', [])
        if scopes and scopes[0] == center:
            return
        self._component_scopes = [center] + [c for c in scopes if c != center][:3]
        self._scope_windows = None

    def _scan_component_scopes(self, checked):
        """Look for replacement objects near prior matches, not at old pointers.

        Up to four 2-MiB neighbourhoods, bounded work each poll, clipped to
        readable ranges. These are process-local discovery hints, never writes.
        """
        scopes = getattr(self, '_component_scopes', [])
        if not scopes:
            return None
        if getattr(self, '_scope_windows', None) is None:
            windows = []
            for center in scopes:
                for low, high in [(center, center+0x10000),
                                  (max(0x1000, center-0x100000), center),
                                  (center+0x10000, center+0x100000)]:
                    for r in self._ranges:
                        start, end = max(low, r['base']), min(high, r['end'])
                        if start < end and not r.get('executable', False):
                            windows.append((start, end))
            self._scope_windows = windows
            self._scope_index = 0; self._scope_address = None
        deadline = time.monotonic()+.02; remaining = 4*1048576
        while self._scope_index < len(self._scope_windows) and remaining > 0 and time.monotonic() < deadline:
            low, high = self._scope_windows[self._scope_index]
            address = self._scope_address or low
            size = min(0x10000, high-address, remaining)
            raw = self.process.read(address, size)
            self.scope_scan_bytes = getattr(self, 'scope_scan_bytes', 0)+size
            remaining -= size; self._scope_address = address+size
            if self._scope_address >= high:
                self._scope_index += 1; self._scope_address = None
            if raw is None:
                self.scan_read_failures = getattr(self, 'scan_read_failures', 0)+1
                continue
            for match in re.finditer(re.escape(struct.pack('<Q', self.vtable)), raw):
                candidate = address+match.start()
                if candidate % 8 or candidate in checked:
                    continue
                checked.add(candidate)
                component = self._probe_component(candidate)
                if component and component['chain']:
                    self.last_discovery = 'previous_component_neighbourhood'
                    return component
        if self._scope_index >= len(self._scope_windows):
            self._scope_index = 0; self._scope_address = None
        return None

    def search_snapshot(self):
        return {'full_scan_mib': self.scan_bytes/1048576,
                'scope_scan_mib': getattr(self, 'scope_scan_bytes', 0)/1048576,
                'full_scan_passes': getattr(self, 'full_scan_passes', 0),
                'region_index': getattr(self, '_range_index', 0), 'region_count': len(getattr(self, '_ranges', [])),
                'scan_address': getattr(self, '_scan_address', None),
                'neighbourhood_count': len(getattr(self, '_component_scopes', [])),
                'read_failures': getattr(self, 'scan_read_failures', 0),
                'last_discovery': getattr(self, 'last_discovery', None)}

    def _probe_component(self, address):
        """A hint is never a usable snapshot: revalidate every pointer on retry."""
        try:
            component = self.component(address)
        except ValueError as error:
            if not self.locate_diagnostics:
                self.locate_diagnostics = {'reason': getattr(error, 'reason', 'validation_failed'),
                                           'message': str(error), 'address': address}
            if getattr(error, 'retryable', True):
                # Do not extend expiry on every failed read. Bounded and owned
                # by this process session, never persisted or copied to a new one.
                if address not in self._recent_components:
                    if len(self._recent_components) >= 8:
                        del self._recent_components[next(iter(self._recent_components))]
                    self._recent_components[address] = time.monotonic() + 15
            else:
                self._recent_components.pop(address, None)
            return None
        if component['chain']:
            self.component_address = address
            self._remember_component_scope(address)
            self._recent_components.clear()
            self.locate_diagnostics = {}
        return component

    def locate(self):
        # Also supports lightweight read-only/test sessions constructed without
        # __init__; real sessions initialise these fields above.
        if not hasattr(self, '_recent_components'):
            self._recent_components = {}
        self.locate_diagnostics = {}
        now = time.monotonic()
        self._recent_components = {a:expiry for a,expiry in self._recent_components.items() if expiry > now}
        empty_component = None
        checked = set()
        if self.component_address:
            checked.add(self.component_address)
            component = self._probe_component(self.component_address)
            if component and component['chain']:
                self.last_discovery = 'cached_address'
                return component
            if component:
                empty_component = component
                self.locate_diagnostics = {'reason': 'empty_state', 'address': component['address']}
            else:
                self.component_address = None
        for address in list(self._recent_components):
            if address in checked:
                continue
            checked.add(address)
            component = self._probe_component(address)
            if component and component['chain']:
                self.last_discovery = 'recent_address'
                return component
            if component and empty_component is None:
                empty_component = component
        # Failed/empty hints must never starve discovery of a different object.
        # Scan cached neighbourhoods before a potentially slow metadata refresh.
        recovered = self._scan_component_scopes(checked)
        if recovered:
            return recovered
        self._refresh_ranges()
        if self._range_index >= len(self._ranges):
            self.full_scan_passes = getattr(self, 'full_scan_passes', 0)+1
            self._range_index = 0; self._scan_address = None
            if not self._priority_ranges:
                self._priority_ranges = [r for r in self._ranges if r['end']-r['base'] <= 16*1048576]
        deadline = time.monotonic()+.08; remaining = 16*1048576
        pattern = re.escape(struct.pack('<Q', self.vtable))
        self._scan_turn += 1
        priority = bool(self._priority_ranges and self._scan_turn % 2 == 0)
        while (self._priority_ranges if priority else self._range_index < len(self._ranges)) and remaining > 0 and time.monotonic() < deadline:
            region = self._priority_ranges[0] if priority else self._ranges[self._range_index]
            a = (self._priority_address if priority else self._scan_address) or region['base']
            n = min(1048576, region['end']-a, remaining)
            raw = self.process.read(a, n); remaining -= n
            self.scan_bytes += n
            if raw is None:
                self.scan_read_failures = getattr(self, 'scan_read_failures', 0)+1
            if priority:
                self._priority_address = a+n
                if self._priority_address >= region['end']:
                    self._priority_ranges.pop(0); self._priority_address = None
            else:
                self._scan_address = a+n
                if self._scan_address >= region['end']:
                    self._range_index += 1; self._scan_address = None
            if raw:
                for match in re.finditer(pattern, raw):
                    address = a+match.start()
                    if address % 8:
                        continue
                    if address in checked:
                        continue
                    checked.add(address)
                    component = self._probe_component(address)
                    if component and component['chain']:
                        self.last_discovery = 'priority_scan' if priority else 'global_scan'
                        return component
                    if component and empty_component is None:
                        empty_component = component
        if empty_component is not None:
            self.component_address = empty_component['address']
        return empty_component

    def verify_table(self, context):
        if context == self._table_context:
            return
        h = self.read(context, 80); sentinel, count = Q(h, 24), Q(h, 32)
        if not 1 <= count <= 512:
            raise ValueError('车辆表上下文异常')
        node = Q(self.read(sentinel, 32)); seen = set(); found = None
        while node != sentinel and node not in seen and len(seen) < 512:
            seen.add(node); b = self.read(node, 40)
            if Q(b, 16) == self.descriptor:
                found = Q(b, 24)
            node = Q(b)
        if node != sentinel or len(seen) != count or found is None:
            raise ValueError('车辆表注册校验失败')
        h = self.read(found, 32); lo, hi = Q(h), Q(h, 8)
        if hi-lo != 47*8:
            raise ValueError('车辆候选表已变化')
        pointers = self.read(lo, hi-lo); tables = {}
        for offset in range(0, len(pointers), 8):
            address = Q(self.read(Q(pointers, offset), 40), 32)
            row = self.read(address, 104); key = U(row, 8); expected = self.catalog.rows.get(key)
            if not expected or (U(row, 0x58), U(row, 0x60), U(row, 0x5c)) != (expected['car_id'], expected['level'], expected['preset']):
                raise ValueError('车辆型号、等级或调校表已变化')
            begin, end = Q(row, 0x38), Q(row, 0x40)
            if end-begin != len(expected['children'])*32:
                raise ValueError('升级候选池已变化')
            children = self.read(begin, end-begin) if end > begin else b''
            if [U(children, i+8) for i in range(0, len(children), 32)] != expected['children']:
                raise ValueError('升级候选车型已变化')
            tables[key] = (address, row)
        if set(tables) != set(self.catalog.rows):
            raise ValueError('车辆表键重复或缺失')
        self.tables, self._table_context = tables, context

    def guard(self, component, node_address, expected):
        if not self.process.is_alive():
            raise ValueError('游戏已关闭')
        fresh = self.component(component['address'])
        if not fresh['chain'] or not component['chain'] or fresh['state'] != component['state'] or fresh['chain'][0] != component['chain'][0]:
            raise ValueError('本车已变化，本次改选取消')
        pending = {n['address']: n for n in fresh['chain'][1:]}
        if node_address not in pending or pending[node_address]['hex'] != expected.hex():
            raise ValueError('待升级车辆已变化，本次改选取消')
        return fresh

    def write_key(self, component, address, expected, key, permitted=lambda: True):
        self.guard(component, address, expected)
        old_key = U(expected, 8)
        if (key not in self.catalog.rows or old_key not in self.catalog.rows or
                self.catalog.rows[key]['level'] != self.catalog.rows[old_key]['level'] or
                self.catalog.rows[key]['level'] not in range(2, 11)):
            raise ValueError('仅支持同一候选等级的改选')
        table_address, table_raw = self.tables[key]
        if self.read(table_address, 104) != table_raw:
            raise ValueError('目标车型数据已变化')
        from win_readonly import FILETIME
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [C.c_uint32, C.c_int, C.c_uint32]; kernel.OpenProcess.restype = C.c_void_p
        kernel.CloseHandle.argtypes = [C.c_void_p]; kernel.CloseHandle.restype = C.c_int
        kernel.GetProcessTimes.argtypes = [C.c_void_p]+[C.POINTER(FILETIME)]*4; kernel.GetProcessTimes.restype = C.c_int
        kernel.WriteProcessMemory.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
        kernel.WriteProcessMemory.restype = C.c_int
        handle = kernel.OpenProcess(0x438, False, self.process.pid)
        if not handle:
            raise OSError('无法取得普通写入权限，改选已停止')
        try:
            times = [FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(C.byref(t) for t in times)):
                raise OSError('无法复核游戏会话')
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            if created != self.process.identity['creation_time_100ns']:
                raise ValueError('游戏进程已更换')
            self.guard(component, address, expected)
            if not permitted():
                raise ValueError('选车已暂停或设置已变化')
            value = C.c_uint32(key); count = C.c_size_t()
            if not kernel.WriteProcessMemory(handle, address+8, C.byref(value), 4, C.byref(count)) or count.value != 4:
                raise OSError('待升级车型写入失败')
            desired = bytearray(expected); struct.pack_into('<I', desired, 8, key)
            if self.read(address, 48) != desired:
                raise ValueError('改选后的读回校验失败')
            return bytes(desired)
        finally:
            kernel.CloseHandle(handle)

    def apply(self, component, selections, permitted=lambda: True):
        if not component or not component['chain']:
            return component, 0
        if component['chain'][0]['level'] == 10:
            pending = {n.get('address') for n in component['chain'][1:]}
            self.journal = {a:e for a,e in self.journal.items() if a in pending and e.get('mode') == 'level10_swap'}
            return component, 0
        self.verify_table(component['chain'][0]['context'])
        self.journal = {a:e for a,e in self.journal.items() if e.get('mode') != 'level10_swap'}
        pending = {n['address']: n for n in component['chain'][1:]}
        # Entries consumed as the current car must never be restored.
        for address in list(self.journal):
            if address not in pending:
                del self.journal[address]
        wanted = self.catalog.validated(selections)
        for address, entry in list(self.journal.items()):
            node = pending[address]
            selected = wanted[str(node['level'])]
            if selected == entry['new_key']:
                continue
            expected = bytes.fromhex(entry['after'])
            if node['hex'] != entry['after']:
                raise ValueError('待升级记录已被游戏重建')
            self.write_key(component, address, expected, entry['old_key'], permitted)
            del self.journal[address]
            component = self.component(component['address'])
        count = 0
        for change in plan_changes(self.catalog, component['chain'], wanted):
            before = bytes.fromhex(change['before'])
            # Store recovery info before attempting the write, including a
            # possible successful write followed by a failed readback.
            after = bytearray(before); struct.pack_into('<I', after, 8, change['new_key'])
            self.journal.setdefault(change['address'], dict(change, after=after.hex()))
            self.write_key(component, change['address'], before, change['new_key'], permitted)
            count += 1
            component = self.component(component['address'])
        return component, count

    def apply_level10(self, component, rules, enabled=True, permitted=lambda: True):
        if not component or not component['chain'] or component['chain'][0]['level'] != 10:
            return component, 0, False
        current = component['chain'][0]
        pending = {n['address']:n for n in component['chain'][1:]}
        owner = (component['address'], component['state'], current['address'], current['key'])
        self.journal = {a:e for a,e in self.journal.items() if a in pending
                        and e.get('mode') == 'level10_swap' and e.get('owner') == owner}
        desired = self.catalog.validated_level10(rules)[str(current['key'])] if enabled else None
        if not pending:
            return component, 0, False
        self.verify_table(current['context'])
        for address, entry in list(self.journal.items()):
            node = pending[address]
            if node['hex'] != entry['after']:
                # The game rebuilt this pending reference: never restore old
                # bytes over its new value; validate the fresh record below.
                del self.journal[address]
                continue
            if desired == entry['new_key'] and entry['source_key'] == current['key']:
                continue
            self.write_key(component, address, bytes.fromhex(entry['after']), entry['old_key'], permitted)
            del self.journal[address]
            component = self.component(component['address'])
        if desired is None:return component, 0, False
        change = plan_level10_change(self.catalog, component['chain'], rules)
        if change:
            raw = bytearray.fromhex(change['before']); struct.pack_into('<I',raw,8,change['new_key'])
            self.journal.setdefault(change['address'],dict(change,after=raw.hex(),owner=owner))
            self.write_key(component,change['address'],bytes.fromhex(change['before']),change['new_key'],permitted)
            component = self.component(component['address'])
        ready = bool(len(component['chain']) > 1 and component['chain'][1]['key'] == desired)
        return component, int(change is not None), ready

    def restore(self):
        if not self.journal or not self.component_address:
            return
        component = self.component(self.component_address)
        if not component['chain']:
            self.journal.clear(); return
        self.verify_table(component['chain'][0]['context'])
        pending = {n['address']: n for n in component['chain'][1:]}
        for address, entry in list(self.journal.items()):
            if entry.get('mode') == 'level10_swap':
                head = component['chain'][0]
                owner = (component['address'], component['state'], head['address'], head['key'])
                if entry.get('owner') != owner:
                    self.journal.pop(address, None)
                    continue
            if address in pending and pending[address]['hex'] == entry['after']:
                self.write_key(component, address, bytes.fromhex(entry['after']), entry['old_key'])
                component = self.component(component['address'])
            self.journal.pop(address, None)

    def close(self):
        self.process.close()


class UpgradeService:
    def __init__(self, source_getter=None, catalog=None, state_dir=None, session_factory=LiveUpgradeSession):
        self.catalog = catalog or UpgradeCatalog()
        self.source_getter = source_getter
        self.state_dir = Path(state_dir or STATE_DIR)
        self.session_factory = session_factory
        self._lock = threading.RLock(); self._wake = threading.Event(); self._stop = threading.Event()
        self._thread = None; self._enabled = False; self._generation = 0
        self._normal_enabled = False; self._level10_enabled = False
        self._level10_rules = self.catalog.validated_level10({})
        self._diagnostic_due = 0.0
        self._observed_session = None; self._observed_chain = None; self._lost_at = None
        self._search_log_due = 0.0
        self._selections = self.catalog.validated({})
        self._status = {'text': '未启用；设置会自动保存', 'enabled': False,
                        'normal_enabled': False, 'level10_enabled': False, 'level10_ready': False,
                        'chain': [], 'applied': 0}

    def configure(self, enabled, selections):
        with self._lock:
            self._selections = self.catalog.validated(selections)
            self._normal_enabled = bool(enabled)
            self._configure_worker()

    def configure_level10(self, enabled, rules):
        with self._lock:
            self._level10_rules = self.catalog.validated_level10(rules)
            self._level10_enabled = bool(enabled)
            self._configure_worker()

    def _configure_worker(self):
        allowed = self.source_getter is not None and not self._stop.is_set()
        self._normal_enabled = bool(self._normal_enabled and allowed)
        self._level10_enabled = bool(self._level10_enabled and allowed)
        self._enabled = self._normal_enabled or self._level10_enabled
        self._generation += 1
        self._status.update(enabled=self._enabled, normal_enabled=self._normal_enabled,
                            level10_enabled=self._level10_enabled, level10_ready=False)
        if self._enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name='fh5-upgrade-selection', daemon=True)
            self._thread.start()
        if not self._enabled and self._thread is None:
            self._status['text'] = '离线预览：仅保存设置' if self.source_getter is None else '未启用；设置会自动保存'
        self._wake.set()

    def snapshot(self):
        with self._lock:
            return deepcopy(self._status)

    def _publish(self, text, **extra):
        with self._lock:
            if not extra.get('chain'):
                extra.setdefault('level10_ready', False)
            self._status.update(text=text, **extra)
            status = deepcopy(self._status)
        if time.monotonic() >= self._diagnostic_due:
            self._diagnostic_due = time.monotonic()+1
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                temporary = self.state_dir/'upgrade_status.tmp'
                temporary.write_text(json.dumps(dict(status, time=time.time()), ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(temporary, self.state_dir/'upgrade_status.json')
            except OSError:
                pass

    def _event(self, kind, **data):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir/'upgrade_events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': time.time(), 'event': kind, **data}, ensure_ascii=False)+'\n')

    def _observe_chain(self, sid, component, *, phase='before_apply', reason=None):
        """Record transitions, not every poll. Observations are not match IDs."""
        if sid != self._observed_session:
            self._event('upgrade_source_changed', previous_session=self._observed_session, session=sid)
            self._observed_session = sid
            self._observed_chain = None; self._lost_at = None
        chain = (component or {}).get('chain', [])
        if not chain:
            if self._observed_chain is not None:
                self._lost_at = time.monotonic()
                self._event('upgrade_connection_lost', session=sid, reason=reason or {'reason': 'empty_state'},
                            previous_chain=self._observed_chain)
                self._observed_chain = None
            return
        observed = {'component_address': component.get('address'), 'state_address': component.get('state'),
                    'nodes': [{k:n.get(k) for k in ('address', 'key', 'level')} for n in chain]}
        if observed == self._observed_chain:
            return
        if self._observed_chain is None:
            self._event('upgrade_connection_restored' if self._lost_at is not None else 'upgrade_connected',
                        session=sid, component_address=component.get('address'),
                        outage_seconds=time.monotonic()-self._lost_at if self._lost_at is not None else None)
            self._lost_at = None
        self._event('upgrade_chain_observed', session=sid, phase=phase, **observed)
        self._observed_chain = observed

    def _run(self):
        session = None
        sid = None
        blocked_source = None
        logged_configuration = None
        try:
            while not self._stop.is_set():
                self._wake.clear()
                with self._lock:
                    enabled, selections, generation = self._enabled, dict(self._selections), self._generation
                    normal_enabled, swap_enabled = self._normal_enabled, self._level10_enabled
                    swap_rules = dict(self._level10_rules)
                try:
                    source = self.source_getter() or {}
                    sid = source.get('session_key')
                    if session and sid != session.process.identity['session_key']:
                        session.close(); session = None
                    if sid != self._observed_session:
                        self._observe_chain(sid, None)
                    configuration = (generation, sid)
                    if configuration != logged_configuration:
                        self._event('upgrade_configuration', session=sid, normal_enabled=normal_enabled,
                                    level10_enabled=swap_enabled, selections=selections, level10_rules=swap_rules)
                        logged_configuration = configuration
                    if not enabled:
                        if session:
                            session.restore()
                            session.close(); session = None
                        self._observe_chain(sid, None, reason={'reason': 'disabled'})
                        self._publish('已暂停；仍待升级的改选已恢复', chain=[], applied=0)
                    elif (not sid or str(sid).startswith('replay:') or not source.get('process_pid')):
                        self._publish('等待游戏连接…', chain=[], applied=0, session_key=None,
                                      process_pid=None, component_address=None, scan_mib=0,
                                      locate_diagnostics={}, recent_component_count=0, search_progress={})
                    elif source.get('game_version') != self.catalog.profile['package_version']:
                        raise ValueError('当前游戏版本未验证，选车已暂停')
                    elif blocked_source == sid:
                        self._publish('当前会话校验未通过，请重新启用后重试')
                    else:
                        if session is None:
                            session = self.session_factory(source, self.catalog)
                        component = session.locate()
                        diagnostics = getattr(session, 'locate_diagnostics', {})
                        progress = session.search_snapshot() if hasattr(session, 'search_snapshot') else {}
                        self._observe_chain(sid, component, reason=diagnostics)
                        if not component or not component['chain']:
                            if time.monotonic() >= self._search_log_due:
                                self._event('upgrade_search_progress', session=sid, progress=progress,
                                            diagnostics=diagnostics)
                                self._search_log_due = time.monotonic()+30
                            self._publish(f'定位升级记录…已扫描 {session.scan_bytes/1048576:.0f} MiB',
                                          chain=[], applied=0, session_key=sid,
                                          process_pid=source['process_pid'], scan_mib=session.scan_bytes/1048576,
                                          component_address=component['address'] if component else None,
                                          locate_diagnostics=diagnostics,
                                          search_progress=progress,
                                          recent_component_count=len(getattr(session, '_recent_components', {})))
                        else:
                            self._search_log_due = 0.0
                            def permitted():
                                with self._lock:
                                    return (self._enabled and self._generation == generation and not self._stop.is_set()
                                            and (self.source_getter() or {}).get('session_key') == sid)
                            swap_ready = False
                            current_key = component['chain'][0]['key']
                            if component['chain'][0]['level'] == 10:
                                component, count, swap_ready = session.apply_level10(component, swap_rules, swap_enabled, permitted)
                                target = swap_rules[str(current_key)] if swap_enabled else None
                                message = ('10级目标已准备；获胜后选择换车' if swap_ready else
                                           '已到10级，等待游戏生成待换车辆记录' if target is not None else
                                           '已接入；本车10级换车保留原规则')
                            else:
                                component, count = session.apply(component, selections if normal_enabled else {}, permitted)
                                message = '已接入；按各等级设置准备升级车辆' if normal_enabled else '已接入；10级换车规则将在到达10级后使用'
                            if count:
                                self._event('level10_swap_applied' if component['chain'][0]['level'] == 10 else 'pending_choices_applied',
                                            session=sid, changes=count, selections=selections, level10_rules=swap_rules,
                                            current_key=current_key, next_key=component['chain'][1]['key'] if len(component['chain'])>1 else None)
                            self._observe_chain(sid, component, phase='after_apply')
                            self._publish(message, chain=component['chain'], applied=len(session.journal), level10_ready=swap_ready,
                                          session_key=sid, process_pid=source['process_pid'],
                                          scan_mib=session.scan_bytes/1048576, component_address=component['address'],
                                          locate_diagnostics={}, recent_component_count=0, search_progress=progress)
                except TimeoutError as error:
                    self._observe_chain(sid, None, reason={'reason': 'metadata_timeout', 'message': str(error)})
                    self._publish('定位暂忙，正在自动重试…', chain=[], applied=0)
                    self._event('metadata_retry', message=str(error))
                except Exception as error:
                    self._observe_chain(sid, None, reason={'reason': 'paused_on_error', 'message': str(error)})
                    if session:
                        try:
                            session.restore()
                        except Exception:
                            pass
                        session.close(); session = None
                    with self._lock:
                        self._enabled = self._normal_enabled = self._level10_enabled = False
                        self._status.update(enabled=False, normal_enabled=False, level10_enabled=False,
                                            level10_ready=False, text=f'已暂停：{error}', chain=[], applied=0)
                    self._event('paused_on_error', message=str(error))
                self._wake.wait(.25)
        finally:
            if session:
                try:
                    session.restore()
                except Exception as error:
                    self._event('restore_skipped', message=str(error))
                finally:
                    session.close()

    def close(self):
        with self._lock:
            self._enabled = self._normal_enabled = self._level10_enabled = False; self._generation += 1
            self._status.update(enabled=False, normal_enabled=False, level10_enabled=False, level10_ready=False)
        self._stop.set(); self._wake.set()
        if self._thread:
            self._thread.join(3)
