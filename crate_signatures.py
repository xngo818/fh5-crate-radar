"""Pure candidate inspection. FH6 marker names are UNVERIFIED hypotheses for FH5.

No game-specific coordinate offsets are assumed or installed by this module.
The returned addresses identify string layouts, not proven live C++ objects.
"""
from dataclasses import dataclass
import struct
from typing import Callable

Read = Callable[[int, int], bytes | None]
PTR_MIN, PTR_MAX = 0x10000, 0x7FFFFFFFFFFF


@dataclass(frozen=True)
class Hypothesis:
    names: tuple[bytes, ...]
    strides: tuple[int, ...] = (32, 40, 48, 24, 64)

    def __post_init__(self):
        if len(self.names) != 3 or any(not n or b'\0' in n for n in self.names):
            raise ValueError('Exactly three nonempty, NUL-free marker names are required')
        if len(set(self.names)) != 3:
            raise ValueError('Marker names must be distinct')
        if not self.strides or any(s < 8 or s % 8 for s in self.strides):
            raise ValueError('Candidate strides must be positive multiples of eight')


FH6_MARKER_HYPOTHESIS = Hypothesis((
    b'pickup_position_ref_component', b'flare_group', b'pickup_model',
))


def read_string(read: Read, addr: int) -> bytes | None:
    """Interpret the tested 32-byte MSVC-string hypothesis, strictly."""
    if not PTR_MIN < addr < PTR_MAX:
        return None
    data = read(addr, 32)
    if data is None or len(data) != 32:
        return None
    size, capacity = struct.unpack_from('<QQ', data, 16)
    if not 15 <= capacity <= 4096 or size > capacity:
        return None
    if capacity == 15:
        return data[:size]
    pointer = struct.unpack_from('<Q', data)[0]
    if not PTR_MIN < pointer < PTR_MAX:
        return None
    if not size:
        return b''
    data = read(pointer, size)
    return data if data is not None and len(data) == size else None


def verify_signature(read: Read, first: int, stride: int, hypothesis: Hypothesis) -> bool:
    if stride not in hypothesis.strides:
        return False
    return all(read_string(read, first + i * stride) == wanted
               for i, wanted in enumerate(hypothesis.names))


def candidates_from_chunk(read: Read, base: int, chunk: bytes,
                          hypothesis: Hypothesis = FH6_MARKER_HYPOTHESIS):
    """Find inline third-name hits; independently re-read all three fields."""
    needle = hypothesis.names[2] + b'\0'
    pos = 0
    while True:
        hit = chunk.find(needle, pos)
        if hit < 0:
            return
        pos = hit + 1
        third = base + hit
        for stride in hypothesis.strides:
            first = third - 2 * stride
            if first <= PTR_MIN or first % 8:
                continue
            if verify_signature(read, first, stride, hypothesis):
                yield {
                    'first_string_address': first,
                    'field_addresses': [first + i * stride for i in range(3)],
                    'string_stride': stride,
                    'marker_names': [n.decode('ascii') for n in hypothesis.names],
                    'status': 'string_layout_candidate_only',
                    'position_pointer_offset': None,
                    'xyz_offset': None,
                    'coordinates': None,
                    'lifetime_verified': False,
                }

