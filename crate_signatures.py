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


def _chunk_descriptor_mismatch(chunk: bytes, offset: int, wanted: bytes) -> bool:
    """Reject an impossible descriptor only when all 32 bytes were copied.

    The block is a discovery hint, never a fresh object sample. A plausible
    descriptor must still go through verify_signature's independent reads.
    Fields crossing either block edge deliberately fall back to that reader.
    """
    if offset < 0 or offset + 32 > len(chunk):
        return False
    size, capacity = struct.unpack_from('<QQ', chunk, offset + 16)
    if size != len(wanted) or not 15 <= capacity <= 4096 or size > capacity:
        return True
    if capacity == 15:
        return chunk[offset:offset + size] != wanted
    pointer = struct.unpack_from('<Q', chunk, offset)[0]
    return not PTR_MIN < pointer < PTR_MAX


def candidates_from_chunk(read: Read, base: int, chunk: bytes,
                          hypothesis: Hypothesis = FH6_MARKER_HYPOTHESIS,
                          *, state: dict | None = None,
                          check_budget: Callable[[], None] | None = None):
    """Find inline third-name hits; independently read all three fields.

    An optional caller-owned state resumes at the current hit and stride after
    the reader raises a budget exception. Only the current stride's exact read
    results are retained, so even a signature slower than one slice can finish.
    Such results are discovery hints, not current coordinate samples: live
    callers must independently revalidate every returned address before use.
    An optional no-I/O budget check also bounds pure local rejection work,
    once on entry and then every 64 hits. Exceptions retain the pending hit.
    The default call remains a stateless generator.
    """
    state = {} if state is None else state
    needle = hypothesis.names[2] + b'\0'
    pos = state.get('position', 0)
    if check_budget is not None:
        check_budget()
    hits_since_check = 0
    while True:
        hit = chunk.find(needle, pos)
        if hit < 0:
            state.clear()
            state['finished'] = True
            return
        state['position'] = hit
        if check_budget is not None and hits_since_check >= 64:
            check_budget()
            hits_since_check = 0
        hits_since_check += 1
        third = base + hit
        for index in range(state.get('stride_index', 0), len(hypothesis.strides)):
            stride = hypothesis.strides[index]
            state['stride_index'] = index
            first = third - 2 * stride
            if first <= PTR_MIN or first % 8:
                state['stride_index'] = index + 1
                continue
            # Most inline marker hits are unrelated text, not three MSVC
            # string descriptors. Reject those from the already copied bytes
            # before making up to five sets of tiny cross-process reads. This
            # never accepts a layout and cannot replace live verification.
            if any(_chunk_descriptor_mismatch(chunk, first + i * stride - base, wanted)
                   for i, wanted in enumerate(hypothesis.names)):
                state['stride_index'] = index + 1
                continue
            cache = state.setdefault('read_cache', {})
            def resumed_read(address, size):
                key = address, size
                if key not in cache:
                    cache[key] = read(address, size)
                return cache[key]
            matched = verify_signature(resumed_read, first, stride, hypothesis)
            state.pop('read_cache', None)
            state['stride_index'] = index + 1
            if matched:
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
        pos = hit + 1
        state['position'] = pos
        state['stride_index'] = 0

