"""Offline corruption/false-positive tests; never loads a game process."""
import struct
import unittest
from crate_signatures import (
    FH6_MARKER_HYPOTHESIS, Hypothesis, candidates_from_chunk,
    read_string, verify_signature,
)


class Memory:
    def __init__(self):
        self.data = {}

    def put(self, addr, data):
        self.data.update((addr + i, b) for i, b in enumerate(data))

    def read(self, addr, size):
        try:
            return bytes(self.data[addr + i] for i in range(size))
        except KeyError:
            return None


def fixture():
    mem = Memory()
    first = 0x100000
    for i, name in enumerate(FH6_MARKER_HYPOTHESIS.names):
        if len(name) <= 15:
            field = name.ljust(16, b'\0') + struct.pack('<QQ', len(name), 15)
        else:
            pointer = 0x200000 + i * 0x100
            field = struct.pack('<Q', pointer).ljust(16, b'\0') + struct.pack('<QQ', len(name), 31)
            mem.put(pointer, name)
        mem.put(first + i * 32, field)
    return mem, first


class SignatureTests(unittest.TestCase):
    def test_valid_inline_and_heap_fields_yield_no_unverified_coordinates(self):
        mem, first = fixture()
        hits = list(candidates_from_chunk(mem.read, first, mem.read(first, 96)))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['first_string_address'], first)
        self.assertIsNone(hits[0]['coordinates'])
        self.assertFalse(hits[0]['lifetime_verified'])

    def test_keyword_in_unrelated_data_is_not_an_object(self):
        mem = Memory()
        mem.put(0x100000, b'\0' * 128)
        mem.put(0x100040, b'pickup_model\0')
        self.assertEqual(list(candidates_from_chunk(mem.read, 0x100000,
                                                   mem.read(0x100000, 128))), [])

    def test_mismatched_or_replaced_field_rejects_cached_candidate(self):
        mem, first = fixture()
        self.assertTrue(verify_signature(mem.read, first, 32, FH6_MARKER_HYPOTHESIS))
        mem.put(first + 32, b'wrong_group')
        self.assertFalse(verify_signature(mem.read, first, 32, FH6_MARKER_HYPOTHESIS))

    def test_corrupt_length_and_short_heap_read_are_rejected(self):
        mem, first = fixture()
        mem.put(first + 16, struct.pack('<Q', 4097))
        self.assertIsNone(read_string(mem.read, first))
        mem, first = fixture()
        del mem.data[0x200000]
        self.assertIsNone(read_string(mem.read, first))

    def test_empty_or_truncated_profile_never_passes_vacuously(self):
        for names in [(), (b'one',), (b'a', b'b', b''), (b'a', b'a', b'c')]:
            with self.assertRaises(ValueError):
                Hypothesis(names)


if __name__ == '__main__':
    unittest.main()
