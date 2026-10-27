"""Offline lifetime/corruption tests; no game process or native APIs."""
import struct
import unittest

from crate_reader import MARKERS, SUCCESS, diff_samples, read_candidate


class Memory:
    def __init__(self):
        self.data = {}
        self.on_read = None

    def put(self, address, data):
        self.data.update((address + i, value) for i, value in enumerate(data))

    def read(self, address, size):
        try:
            data = bytes(self.data[address + i] for i in range(size))
        except KeyError:
            return None
        if self.on_read:
            self.on_read(address, size)
        return data


def fixture():
    memory = Memory()
    first, pointer, control = 0x100000, 0x300000, 0x400000
    for i, text in enumerate(MARKERS):
        if len(text) <= 15:
            descriptor = text.ljust(16, b'\0') + struct.pack('<QQ', len(text), 15)
        else:
            address = 0x200000 + i * 0x100
            descriptor = struct.pack('<Q', address).ljust(16, b'\0') + struct.pack('<QQ', len(text), 31)
            memory.put(address, text)
        memory.put(first + i * 32, descriptor)
    memory.put(first - 8, struct.pack('<Q', 9))
    memory.put(first - 120, struct.pack('<Q', 0x123456))
    memory.put(first + 152, struct.pack('<QQQ', pointer, control, first))
    memory.put(pointer + 144, struct.pack('<3f', 1234.5, 200.25, -321.5))
    return memory, first, pointer, control


class ReaderTests(unittest.TestCase):
    def test_valid_read_is_only_experimental_and_raw_row_has_no_mapping(self):
        memory, first, pointer, control = fixture()
        row = read_candidate(memory.read, first, 'session-one')
        self.assertEqual(row['status'], SUCCESS)
        self.assertEqual(row['coordinates'], [1234.5, 200.25, -321.5])
        self.assertEqual(row['row_raw'], 9)
        self.assertEqual(row['loot_row_id'], 9)
        self.assertEqual(row['row_padding_u32'], 0)
        self.assertEqual(row['fingerprint_version'], 2)
        self.assertNotIn('level', row)
        self.assertFalse(row['coordinates_verified'])
        self.assertFalse(row['membership_verified'])
        self.assertFalse(row['lifetime_verified'])

    def test_mid_read_pointer_replacement_rejects_readable_old_coordinates(self):
        memory, first, pointer, control = fixture()
        def mutate(address, size):
            if address == pointer + 144:
                memory.put(first + 152, struct.pack('<Q', pointer + 0x1000))
        memory.on_read = mutate
        row = read_candidate(memory.read, first, 'session-one')
        self.assertEqual(row['reason'], 'identity_changed_during_read')
        self.assertIn('position_pointer', row['details']['changed_fields'])
        self.assertIsNone(row['coordinates'])
        self.assertIsNone(row['fingerprint'])

    def test_readable_slot_reuse_is_new_identity_even_at_same_position(self):
        memory, first, pointer, control = fixture()
        old = read_candidate(memory.read, first, 'session-one')
        memory.put(first + 152, struct.pack('<Q', pointer + 0x1000))
        memory.put(pointer + 0x1000 + 144, struct.pack('<3f', *old['coordinates']))
        new = read_candidate(memory.read, first, 'session-one')
        diff = diff_samples([old], [new])
        self.assertEqual(diff['events'][0]['kind'], 'same_slot_reused')
        self.assertEqual(diff['active_candidates'], [new])
        self.assertNotEqual(old['fingerprint'], new['fingerprint'])

    def test_nonfinite_bounds_and_zero_horizontal_are_rejected(self):
        for xyz, reason in [((float('nan'), 2, 3), 'nonfinite_coordinates'),
                            ((1, float('inf'), 3), 'nonfinite_coordinates'),
                            ((12000, 2, 3), 'coordinates_out_of_bounds'),
                            ((1, 2, -12000), 'coordinates_out_of_bounds'),
                            ((0, 8, 0), 'zero_horizontal_coordinates')]:
            with self.subTest(xyz=xyz):
                memory, first, pointer, control = fixture()
                memory.put(pointer + 144, struct.pack('<3f', *xyz))
                self.assertEqual(read_candidate(memory.read, first, 'session-one')['reason'], reason)

    def test_wrong_tag_backref_and_short_exact_read_reject(self):
        memory, first, pointer, control = fixture()
        memory.put(first + 168, struct.pack('<Q', first + 32))
        self.assertEqual(read_candidate(memory.read, first, 'session-one')['reason'], 'wrong_tag_backref')
        memory, first, pointer, control = fixture()
        def short_read(address, size):
            data = memory.read(address, size)
            return data[:-1] if address == pointer + 144 else data
        self.assertEqual(read_candidate(short_read, first, 'session-one')['reason'], 'short_or_failed_read')

    def test_after_read_string_mutation_is_rejected(self):
        memory, first, pointer, control = fixture()
        def mutate(address, size):
            if address == pointer + 144:
                memory.put(first + 32, b'wrong_group')
        memory.on_read = mutate
        self.assertEqual(read_candidate(memory.read, first, 'session-one')['reason'], 'signature_mismatch')

    def test_each_identity_field_is_checked_again(self):
        for address_offset in [-8, -120, 160, 168]:
            with self.subTest(offset=address_offset):
                memory, first, pointer, control = fixture()
                def mutate(address, size):
                    if address == pointer + 144:
                        memory.put(first + address_offset, struct.pack('<Q', 0x800000))
                memory.on_read = mutate
                self.assertEqual(read_candidate(memory.read, first, 'session-one')['reason'],
                                 'identity_changed_during_read')

    def test_padding_changes_neither_reject_nor_replace_identity(self):
        memory, first, pointer, control = fixture()
        old = read_candidate(memory.read, first, 'session-one')
        def change_padding(address, size):
            if address == pointer + 144:
                memory.put(first - 4, b'9180')
        memory.on_read = change_padding
        after = read_candidate(memory.read, first, 'session-one')
        self.assertEqual(after['status'], SUCCESS)
        self.assertEqual(after['loot_row_id'], 9)
        self.assertEqual(after['row_padding_u32'], int.from_bytes(b'9180', 'little'))
        self.assertNotEqual(after['row_raw'], old['row_raw'])
        self.assertEqual(after['fingerprint'], old['fingerprint'])
        self.assertNotIn('row_raw', after['fingerprint_fields'])
        self.assertNotIn('row_padding_u32', after['fingerprint_fields'])
        self.assertEqual(diff_samples([old], [after])['events'][0]['kind'], 'stable')

    def test_real_row32_change_is_new_identity_even_with_same_padding(self):
        memory, first, pointer, control = fixture()
        memory.put(first - 4, b'9180')
        old = read_candidate(memory.read, first, 'session-one')
        memory.put(first - 8, struct.pack('<I', 15))
        new = read_candidate(memory.read, first, 'session-one')
        self.assertEqual(new['loot_row_id'], 15)
        self.assertNotEqual(new['fingerprint'], old['fingerprint'])
        self.assertEqual(diff_samples([old], [new])['events'][0]['kind'], 'same_slot_reused')

    def test_diffs_track_motion_missing_fingerprints_and_sessions(self):
        memory, first, pointer, control = fixture()
        old = read_candidate(memory.read, first, 'session-one')
        stable = read_candidate(memory.read, first, 'session-one')
        self.assertEqual(diff_samples([old], [stable])['events'][0]['kind'], 'stable')
        memory.put(pointer + 144, struct.pack('<3f', 1235.5, 202.25, -318.5))
        moved = read_candidate(memory.read, first, 'session-one')
        diff = diff_samples([old], [moved])
        self.assertEqual(diff['events'][0]['kind'], 'position_changed')
        self.assertEqual(diff['events'][0]['coordinate_delta'], [1, 2, 3])
        rejected = dict(moved, status='rejected', coordinates=None, fingerprint=None)
        diff = diff_samples([old], [rejected])
        self.assertEqual(diff['events'][0]['kind'], 'disappeared')
        self.assertEqual(diff['active_candidates'], [])
        another_session = read_candidate(memory.read, first, 'session-two')
        self.assertEqual({e['kind'] for e in diff_samples([old], [another_session])['events']},
                         {'disappeared', 'new_candidate'})


if __name__ == '__main__':
    unittest.main()
