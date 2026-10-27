"""Offline discovery filtering: false text, block edges and resumed budgets."""
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crate_signatures import FH6_MARKER_HYPOTHESIS, candidates_from_chunk


class Memory:
    def __init__(self):
        self.base = 0x100000
        self.data = bytearray(8192)
        self.heap = 0x800000
        self.calls = []

    def read(self, address, size):
        self.calls.append((address, size))
        if self.base <= address and address + size <= self.base + len(self.data):
            offset = address - self.base
            return bytes(self.data[offset:offset + size])
        if address == self.heap:
            return FH6_MARKER_HYPOTHESIS.names[0][:size]
        return None

    def install(self, offset=256, stride=32):
        first = self.base + offset
        for index, name in enumerate(FH6_MARKER_HYPOTHESIS.names):
            descriptor = bytearray(32)
            if len(name) > 15:
                struct.pack_into('<Q', descriptor, 0, self.heap)
                capacity = 31
            else:
                descriptor[:len(name)] = name
                capacity = 15
            struct.pack_into('<QQ', descriptor, 16, len(name), capacity)
            begin = offset + index * stride
            self.data[begin:begin + 32] = descriptor
        return first


class ChunkPrefilterTests(unittest.TestCase):
    def test_dense_plaintext_hits_need_no_remote_descriptor_reads(self):
        memory = Memory()
        for offset in range(192, len(memory.data) - 32, 128):
            memory.data[offset:offset + 13] = b'pickup_model\0'
        self.assertEqual(list(candidates_from_chunk(
            memory.read, memory.base, bytes(memory.data))), [])
        self.assertEqual(memory.calls, [])

    def test_supported_valid_strides_still_independently_read_every_field(self):
        self.assertEqual(FH6_MARKER_HYPOTHESIS.strides, (32, 40, 48, 24, 64))
        for stride in (32, 40, 48, 64):
            with self.subTest(stride=stride):
                memory = Memory()
                first = memory.install(stride=stride)
                hits = list(candidates_from_chunk(memory.read, memory.base, bytes(memory.data)))
                self.assertEqual([(hit['first_string_address'], hit['string_stride'])
                                  for hit in hits], [(first, stride)])
                for index in range(3):
                    self.assertIn((first + index * stride, 32), memory.calls)
                self.assertIn((memory.heap, len(FH6_MARKER_HYPOTHESIS.names[0])), memory.calls)

    def test_partial_descriptors_at_either_block_edge_fall_back_to_reader(self):
        for begin, end in ((264, 352), (288, 352), (320, 352), (256, 333), (320, 333)):
            with self.subTest(begin=begin, end=end):
                memory = Memory()
                first = memory.install()
                hits = list(candidates_from_chunk(memory.read, memory.base + begin,
                                                  bytes(memory.data[begin:end])))
                self.assertEqual([hit['first_string_address'] for hit in hits], [first])
                for index in range(3):
                    self.assertIn((first + index * 32, 32), memory.calls)

    def test_plausible_stale_chunk_cannot_accept_replaced_live_descriptor(self):
        memory = Memory()
        first = memory.install()
        chunk = bytes(memory.data)
        struct.pack_into('<Q', memory.data, 256 + 16, 1)
        self.assertEqual(list(candidates_from_chunk(memory.read, memory.base, chunk)), [])
        self.assertIn((first, 32), memory.calls)

    def test_invalid_length_capacity_pointer_and_inline_text_are_local_rejections(self):
        for field, offset, replacement in (
            ('length', 256 + 16, struct.pack('<Q', 4097)),
            ('capacity', 256 + 24, struct.pack('<Q', 0)),
            ('pointer', 256, struct.pack('<Q', 0)),
            ('inline', 288, b'wrong_group'),
        ):
            with self.subTest(field=field):
                memory = Memory()
                memory.install()
                memory.data[offset:offset + len(replacement)] = replacement
                self.assertEqual(list(candidates_from_chunk(
                    memory.read, memory.base, bytes(memory.data))), [])
                self.assertEqual(memory.calls, [])

    def test_budget_exception_resumes_pending_stride_without_rereading_prefix(self):
        class BudgetEnded(Exception):
            pass

        memory = Memory()
        first = memory.install()
        state = {}
        calls_left = 1

        def limited(address, size):
            nonlocal calls_left
            if calls_left == 0:
                raise BudgetEnded()
            calls_left -= 1
            return memory.read(address, size)

        chunk = bytes(memory.data)
        with self.assertRaises(BudgetEnded):
            list(candidates_from_chunk(limited, memory.base, chunk, state=state))
        self.assertEqual(state['position'], 320)
        self.assertEqual(state['stride_index'], 0)
        self.assertEqual(memory.calls, [(first, 32)])
        calls_left = 10
        hits = list(candidates_from_chunk(limited, memory.base, chunk, state=state))
        self.assertEqual([hit['first_string_address'] for hit in hits], [first])
        self.assertEqual(memory.calls.count((first, 32)), 1)
        self.assertTrue(state['finished'])

    def test_reader_failure_or_exception_is_not_swallowed(self):
        memory = Memory()
        memory.install()
        chunk = bytes(memory.data)
        self.assertEqual(list(candidates_from_chunk(lambda *args: None, memory.base, chunk)), [])

        def failing(*args):
            raise OSError('fake read failure')

        with self.assertRaisesRegex(OSError, 'fake read failure'):
            list(candidates_from_chunk(failing, memory.base, chunk))

    def test_local_rejections_checkpoint_every_64_hits_and_resume_pending_hit(self):
        class BudgetEnded(Exception):
            pass

        memory = Memory()
        for offset in range(192, len(memory.data) - 32, 32):
            memory.data[offset:offset + 13] = b'pickup_model\0'
        chunk = bytes(memory.data)
        state, checks = {}, []

        def checkpoint():
            checks.append(state.get('position'))
            if len(checks) == 2:
                raise BudgetEnded()

        with self.assertRaises(BudgetEnded):
            list(candidates_from_chunk(memory.read, memory.base, chunk,
                                       state=state, check_budget=checkpoint))
        self.assertEqual(checks, [None, 192 + 64 * 32])
        self.assertEqual(state['position'], 192 + 64 * 32)
        self.assertEqual(state['stride_index'], 0)
        self.assertFalse(memory.calls)
        self.assertEqual(list(candidates_from_chunk(memory.read, memory.base, chunk,
                                                   state=state, check_budget=checkpoint)), [])
        self.assertEqual(checks[2], 192 + 64 * 32)
        self.assertTrue(state['finished'])
        self.assertFalse(memory.calls)

    def test_entry_checkpoint_keeps_a_pending_remote_read_cache(self):
        memory = Memory()
        first = memory.install()
        state = {'position': 320, 'stride_index': 0,
                 'read_cache': {(first, 32): memory.read(first, 32)}}
        before = dict(state, read_cache=dict(state['read_cache']))

        def stopped():
            raise RuntimeError('stopped before local scan')

        with self.assertRaisesRegex(RuntimeError, 'stopped before local scan'):
            list(candidates_from_chunk(memory.read, memory.base, bytes(memory.data),
                                       state=state, check_budget=stopped))
        self.assertEqual(state, before)


if __name__ == '__main__':
    unittest.main()
