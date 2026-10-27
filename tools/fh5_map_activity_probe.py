"""Single-range, bounded FH5 activity recording, explicitly run by an operator.

This records raw bytes, not a map-open flag. Counter/float activity can belong
to animation, UI, streaming, rendering or an unrelated timer. The same range
must be compared while open, closed, driving while closed and reopened.
Importing this file does not open a process, load DLLs, bind sockets or touch UI.
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import time

if __package__:
    from .fh5_map_probe import package_version, select_regions
else:
    from fh5_map_probe import package_version, select_regions


MAX_BYTES = 4 * 1024 * 1024
TRACKED = (0xB0, 0xC4, 0xD8, 0x194, 0x880)
NOTICE = ("Read-only raw activity evidence; activity is not a map-open flag, "
          "does not verify a camera and must not enable a production overlay.")


def _integer(text):
    return int(text, 0)


def _utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_args(args):
    if type(args.pid) is not int or not 0 < args.pid <= 0xffffffff:
        raise ValueError("PID must be a positive 32-bit integer")
    if (type(args.base) is not int or not 0x10000 <= args.base <= 0x7fffffffffff
            or args.base % 4):
        raise ValueError("base must be a canonical, four-byte aligned address")
    if (type(args.offset) is not int or not -32768 <= args.offset <= 32768
            or args.offset % 4):
        raise ValueError("offset must be four-byte aligned within +/-32768")
    if args.size not in (4096, 8192):
        raise ValueError("size is restricted to 4096 or 8192 bytes")
    if (type(args.samples) is not int or not 50 <= args.samples <= 100
            or not math.isfinite(args.interval) or not .03 <= args.interval <= .05
            or not math.isfinite(args.seconds) or not 0 < args.seconds <= 8):
        raise ValueError("capture is restricted to 50..100 frames, .03..05 s, <=8 s")
    if args.samples * args.size > MAX_BYTES:
        raise ValueError("requested bytes exceed 4 MiB")
    if not isinstance(args.session_key, str) or not args.session_key:
        raise ValueError("current exact session-key is required")
    if not isinstance(args.expect_version, str) or not args.expect_version:
        raise ValueError("exact expected package version is required")
    if not isinstance(args.phase, str) or not args.phase.strip():
        raise ValueError("explicit operator phase is required")


def tracked_fields(blob, *, start_offset):
    """Pure raw/u32/f32 interpretations. None replaces nonfinite float JSON."""
    result = {}
    for offset in TRACKED:
        index = offset - start_offset
        if 0 <= index <= len(blob) - 4:
            raw = blob[index:index + 4]
            value = struct.unpack("<f", raw)[0]
            result[hex(offset)] = {"raw_hex": raw.hex(),
                                   "u32": struct.unpack("<I", raw)[0],
                                   "f32": value if math.isfinite(value) else None}
    return result


def changed_ranges(changes):
    """Condense relative changed-byte offsets into inclusive ranges."""
    result = []
    for offset in sorted(changes):
        if result and offset == result[-1][1] + 1:
            result[-1][1] = offset
        else:
            result.append([offset, offset])
    return result


def capture(process, args, raw_file):
    """Record one range; injectable fake Process supports offline verification.

    Both expected identities are checked before region enumeration/payload
    reads. Failed reads are never zero-padded in the continuous binary stream.
    Every successful frame gives its explicit file offset and exact byte count.
    """
    validate_args(args)
    identity = process.identity
    if identity.get("session_key") != args.session_key or identity.get("pid") != args.pid:
        raise ValueError("process creation identity mismatch; refusing payload reads")
    version = package_version(identity["image_path"])
    if version != args.expect_version:
        raise ValueError("package version mismatch; refusing payload reads")
    started = time.monotonic()
    deadline = started + args.seconds
    raw_address = args.base + args.offset
    selected, excluded = select_regions(
        process.regions(deadline=deadline), minimum=process.min_address,
        maximum=process.max_address, max_region_bytes=32768 * 1024 * 1024)
    containing = next((r for r in selected if r["base"] <= raw_address
                       and raw_address + args.size <= r["base"] + r["size"]), None)
    if containing is None:
        raise ValueError("entire single range must fit one committed writable private region")
    raw_file = Path(raw_file)
    if raw_file.exists():
        raise FileExistsError("refusing to overwrite existing raw activity evidence")
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    frames, attempted_bytes, saved_bytes, failures = [], 0, 0, 0
    consecutive_failures = 0
    previous = None
    changed_counts = Counter()
    field_series = {hex(offset): [] for offset in TRACKED}
    first_due = time.monotonic()
    stop_reason = "requested_frames_finished"
    digest = hashlib.sha256()
    with raw_file.open("xb") as binary:
        for index in range(args.samples):
            due = first_due + index * args.interval
            now = time.monotonic()
            delay = min(max(0.0, due - now), max(0.0, deadline - now))
            if delay:
                time.sleep(delay)
            if time.monotonic() >= deadline or attempted_bytes + args.size > MAX_BYTES:
                stop_reason = "time_or_byte_budget"
                break
            before, utc_before = time.monotonic(), _utc()
            attempted_bytes += args.size
            blob = process.read(raw_address, args.size)
            after = time.monotonic()
            item = {"index": index, "sample_monotonic": before,
                    "read_finished_monotonic": after, "captured_utc": utc_before,
                    "read_finished_utc": _utc(), "requested_monotonic": due,
                    "lateness_seconds": max(0.0, before - due),
                    "virtual_address": raw_address, "base_offset": args.offset,
                    "requested_bytes": args.size, "file_offset": None,
                    "saved_bytes": 0, "sha256": None, "verified": False}
            if blob is None or len(blob) != args.size:
                failures += 1
                consecutive_failures += 1
                item["read_error"] = process.last_error
                previous = None  # Do not imply continuity across an exact-read gap.
            else:
                consecutive_failures = 0
                item.update(file_offset=saved_bytes, saved_bytes=len(blob),
                            sha256=hashlib.sha256(blob).hexdigest(),
                            tracked_fields=tracked_fields(blob, start_offset=args.offset))
                binary.write(blob)
                digest.update(blob)
                saved_bytes += len(blob)
                if previous is not None:
                    changes = [args.offset + i for i, (a, b) in enumerate(zip(previous, blob)) if a != b]
                    item["changed_bytes_from_previous_successful_frame"] = len(changes)
                    changed_counts.update(changes)
                for offset, field in item["tracked_fields"].items():
                    field_series[offset].append(field["raw_hex"])
                previous = blob
            frames.append(item)
            if consecutive_failures >= 3:
                stop_reason = "three_consecutive_exact_read_failures"
                break
    field_summary = {}
    for offset, values in field_series.items():
        if values:
            field_summary[offset] = {"unique_raw_values": len(set(values)),
                                     "transition_count": sum(a != b for a, b in zip(values, values[1:])),
                                     "first_raw_hex": values[0], "last_raw_hex": values[-1]}
    return {"schema": 1, "tool": "fh5_map_activity_probe", "notice": NOTICE,
            "captured_utc": _utc(), "phase": args.phase,
            "identity": identity, "package_version": version,
            "base": args.base, "base_hex": hex(args.base),
            "start_offset": args.offset, "virtual_address": raw_address,
            "range_bytes": args.size, "region": containing,
            "raw": {"file": str(raw_file.resolve()), "size": saved_bytes,
                    "sha256": digest.hexdigest(), "format": "concatenated_successful_exact_reads"},
            "frames": frames, "frame_count": len(frames),
            "successful_frames": sum(f["saved_bytes"] != 0 for f in frames),
            "read_failures": failures, "attempted_bytes": attempted_bytes,
            "budget": {"seconds_limit": args.seconds, "bytes_limit": MAX_BYTES,
                       "elapsed_seconds": time.monotonic() - started,
                       "requested_frames": args.samples, "interval": args.interval},
            "stop_reason": stop_reason,
            "changed_byte_union_count": len(changed_counts),
            "changed_relative_ranges_inclusive": changed_ranges(changed_counts),
            "changed_relative_byte_transition_counts": {str(k): v for k, v in sorted(changed_counts.items())},
            "tracked_field_summary": field_summary,
            "excluded_regions": excluded, "verified": False,
            "map_open": None, "activity_is_map_open_flag": False,
            "projection_verified": False, "session_reuse_allowed": False}


def parser():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--session-key", required=True)
    p.add_argument("--expect-version", required=True)
    p.add_argument("--base", type=_integer, required=True)
    p.add_argument("--offset", type=_integer, default=0)
    p.add_argument("--size", type=int, choices=(4096, 8192), default=4096)
    p.add_argument("--phase", required=True)
    p.add_argument("--samples", type=int, default=80)
    p.add_argument("--interval", type=float, default=.04)
    p.add_argument("--seconds", type=float, default=8)
    p.add_argument("--output", required=True, help="new JSON; same-stem .bin is created beside it")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    validate_args(args)
    output = Path(args.output).resolve()
    raw_file = output.with_suffix(".bin")
    if output == raw_file or output.exists() or raw_file.exists():
        raise FileExistsError("new, distinct JSON and binary evidence paths are required")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from win_readonly import Process
    with Process(args.pid) as process:
        report = capture(process, args, raw_file)
        report["process_still_alive"] = process.is_alive()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(output), "raw_file": str(raw_file),
                      "phase": args.phase, "successful_frames": report["successful_frames"],
                      "attempted_bytes": report["attempted_bytes"],
                      "budget": report["budget"], "stop_reason": report["stop_reason"],
                      "tracked_field_summary": report["tracked_field_summary"],
                      "verified": False}, ensure_ascii=False))
    return 0 if report["successful_frames"] == args.samples else 2


if __name__ == "__main__":
    raise SystemExit(main())
