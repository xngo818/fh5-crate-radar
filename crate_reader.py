"""Pure, experimental FH5 candidate reads; no native APIs or game access.

The FH5 build's LootRowId is uint32 at F-8; F-4 is padding, not identity.
Neither intact strings nor a stable fingerprint proves world membership.
"""
import hashlib
import json
import math
import struct


MARKERS = (b'pickup_position_ref_component', b'flare_group', b'pickup_model')
PTR_MIN, PTR_MAX = 0x10000, 0x7FFFFFFFFFFF
SUCCESS = 'experimental_coordinate_candidate'
FINGERPRINT_VERSION = 2
IDENTITY_FIELDS = ('loot_row_id', 'identity_token_raw', 'position_pointer',
                   'control_pointer', 'tag_backref')


class _Rejected(Exception):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _exact(read, address, size, field):
    if not PTR_MIN < address <= PTR_MAX - size + 1:
        raise _Rejected('invalid_read_range', field=field, address=address)
    try:
        data = read(address, size)
    except (OSError, ValueError) as exc:
        raise _Rejected('read_failed', field=field, error=type(exc).__name__) from exc
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != size:
        raise _Rejected('short_or_failed_read', field=field, address=address,
                        expected_size=size,
                        actual_size=len(data) if isinstance(data, (bytes, bytearray, memoryview)) else None)
    return bytes(data)


def _pointer(value):
    return PTR_MIN < value < PTR_MAX and value % 8 == 0


def _signature(read, first, stage):
    """Return descriptors and exact text, so a replaced allocation is rejected."""
    fields = []
    for i, wanted in enumerate(MARKERS):
        descriptor = _exact(read, first + i * 32, 32, f'{stage}.string[{i}]')
        size, capacity = struct.unpack_from('<QQ', descriptor, 16)
        if not 15 <= capacity <= 4096 or size > capacity:
            raise _Rejected('invalid_string_layout', stage=stage, field_index=i)
        if capacity == 15:
            text = descriptor[:size]
        else:
            address = struct.unpack_from('<Q', descriptor)[0]
            text = _exact(read, address, size, f'{stage}.string[{i}].heap') if size else b''
        if text != wanted:
            raise _Rejected('signature_mismatch', stage=stage, field_index=i)
        fields.append((descriptor, text))
    return tuple(fields)


def _metadata(read, first, stage):
    row_bytes = _exact(read, first - 8, 8, f'{stage}.loot_row_and_padding')
    row, padding = struct.unpack('<II', row_bytes)
    token = struct.unpack('<Q', _exact(read, first - 120, 8, f'{stage}.identity_token_raw'))[0]
    pointer, control, backref = struct.unpack(
        '<QQQ', _exact(read, first + 152, 24, f'{stage}.component_reference'))
    return {'loot_row_id': row, 'row_padding_u32': padding,
            'row_raw': struct.unpack('<Q', row_bytes)[0], 'identity_token_raw': token,
            'position_pointer': pointer, 'control_pointer': control,
            'tag_backref': backref}


def _validate_metadata(meta, first):
    if meta['tag_backref'] != first:
        raise _Rejected('wrong_tag_backref', expected=first, actual=meta['tag_backref'])
    for field in ('position_pointer', 'control_pointer'):
        if not _pointer(meta[field]):
            raise _Rejected('invalid_pointer', field=field, address=meta[field])


def read_candidate(read, first, session_key):
    """Read one F slot, with identity checks surrounding the position read.

    ``read(address, size)`` must return exactly the requested bytes or None.
    Reads are not atomic: an undetected change-and-restore remains possible.
    LootRowId requires a version-matched external table to resolve car and tier.
    The token's high bits have no established generation semantics.
    """
    result = {'status': 'rejected', 'first_string_address': first,
              'session_key': session_key, 'coordinates': None, 'fingerprint': None,
              'coordinates_verified': False, 'membership_verified': False,
              'lifetime_verified': False, 'experimental': True}
    if (not isinstance(first, int) or isinstance(first, bool) or not _pointer(first)
            or not isinstance(session_key, str) or not session_key):
        return dict(result, reason='invalid_candidate_identity')
    try:
        before = _metadata(read, first, 'before')
        _validate_metadata(before, first)
        signature_before = _signature(read, first, 'before')
        raw = _exact(read, before['position_pointer'] + 144, 12, 'position_xyz')
        coordinates = struct.unpack('<3f', raw)
        signature_after = _signature(read, first, 'after')
        after = _metadata(read, first, 'after')
        changed = [key for key in IDENTITY_FIELDS if before[key] != after[key]]
        if changed:
            raise _Rejected('identity_changed_during_read', changed_fields=changed,
                            metadata_before=before, metadata_after=after)
        _validate_metadata(after, first)
        if signature_before != signature_after:
            raise _Rejected('signature_storage_changed_during_read')
        if not all(math.isfinite(value) for value in coordinates):
            raise _Rejected('nonfinite_coordinates')
        if abs(coordinates[0]) >= 12000 or abs(coordinates[2]) >= 12000:
            raise _Rejected('coordinates_out_of_bounds')
        if coordinates[0] == 0 and coordinates[2] == 0:
            raise _Rejected('zero_horizontal_coordinates')
        signature_digest = hashlib.sha256(b''.join(
            descriptor + text for descriptor, text in signature_before)).hexdigest()
        identity = {'fingerprint_version': FINGERPRINT_VERSION,
                    'session_key': session_key, 'first_string_address': first,
                    'signature_sha256': signature_digest,
                    **{key: before[key] for key in IDENTITY_FIELDS}}
        fingerprint = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        return dict(result, status=SUCCESS, coordinates=list(coordinates),
                    fingerprint=fingerprint, fingerprint_fields=identity,
                    fingerprint_version=FINGERPRINT_VERSION,
                    string_stride=32, position_pointer_offset=152, xyz_offset=144,
                    **after)
    except _Rejected as exc:
        return dict(result, reason=exc.reason, details=exc.details)


def _sample_items(sample):
    if isinstance(sample, dict):
        return sample.get('candidates', [sample])
    return sample


def _candidate_map(sample):
    slots, conflicts = {}, set()
    for candidate in _sample_items(sample):
        if not isinstance(candidate, dict) or candidate.get('status') != SUCCESS:
            continue
        first, session = candidate.get('first_string_address'), candidate.get('session_key')
        xyz = candidate.get('coordinates')
        if (not isinstance(first, int) or not isinstance(session, str)
                or not isinstance(candidate.get('fingerprint'), str)
                or not candidate['fingerprint'] or not isinstance(xyz, (list, tuple))
                or len(xyz) != 3 or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in xyz)):
            continue
        slot = session, first
        if slot in slots and slots[slot] != candidate:
            conflicts.add(slot)
        slots[slot] = candidate
    for slot in conflicts:
        slots.pop(slot, None)
    return slots, conflicts


def diff_samples(previous, current):
    """Compare lists (or reports containing ``candidates``), never carry old rows.

    A missing fingerprint is disappearance, not evidence of collection. Reused
    slots are separate identities even if their coordinates happen to agree.
    ``active_candidates`` contains only successful, unambiguous current rows.
    """
    old, old_conflicts = _candidate_map(previous)
    new, new_conflicts = _candidate_map(current)
    events = []
    for session, first in sorted(old.keys() | new.keys() | old_conflicts | new_conflicts):
        slot = session, first
        before, after = old.get(slot), new.get(slot)
        event = {'session_key': session, 'first_string_address': first,
                 'previous_fingerprint': before.get('fingerprint') if before else None,
                 'current_fingerprint': after.get('fingerprint') if after else None}
        if slot in old_conflicts or slot in new_conflicts:
            event['kind'] = 'ambiguous_duplicate_slot'
        elif before is None:
            event['kind'] = 'new_candidate'
        elif after is None:
            event['kind'] = 'disappeared'
        elif before['fingerprint'] != after['fingerprint']:
            event['kind'] = 'same_slot_reused'
        elif before['coordinates'] != after['coordinates']:
            event['kind'] = 'position_changed'
            event['coordinate_delta'] = [b - a for a, b in zip(before['coordinates'], after['coordinates'])]
        else:
            event['kind'] = 'stable'
        events.append(event)
    return {'events': events, 'active_candidates': [new[slot] for slot in sorted(new)],
            'coordinates_verified': False, 'membership_verified': False}
