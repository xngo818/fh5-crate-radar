"""Explicit, bounded, read-only FH5 map-camera investigation.

Importing this module only defines pure byte filters and helpers. It does not
load native DLLs, enumerate processes, open a game, bind a socket or touch UI.
Only the ``scan`` and ``snapshot`` CLI commands instantiate win_readonly.Process,
whose sole access mask is 0x410. Every result is an UNVERIFIED hypothesis.

Examples (the session key must come from the CURRENT process identity):
  python tools/fh5_map_probe.py scan --pid 123 --session-key "123:...:c:\\..." \
    --width 1920 --height 1080 --phase map_open --output state/map_open.json
  python tools/fh5_map_probe.py snapshot --pid 123 --session-key "123:...:c:\\..." \
    --width 1920 --height 1080 --phase pan_right --from-report state/map_open.json \
    --candidate-index 0 --samples 8 --output state/pan_right.json

No address is accepted across process creation identities. A camera shape,
matching viewport or locally plausible matrix never proves map-open state or
screen alignment. This tool intentionally cannot publish production MapFrame.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import time
import xml.etree.ElementTree as ET


MEM_PRIVATE = 0x20000
WRITABLE = frozenset((0x04, 0x08, 0x40, 0x80))
MIB = 1024 * 1024
NOTICE = ("Unverified camera/matrix hypotheses only. No map-open flag, no live "
          "screen alignment, no production overlay authorization.")


def _numpy():
    """Optional existing dependency; never install anything or require numpy."""
    try:
        import numpy
        return numpy
    except ImportError:
        return None


def _finite(values):
    return all(math.isfinite(v) for v in values)


def _viewport(width, height):
    if (not _finite((width, height)) or not 320 <= width <= 16384
            or not 240 <= height <= 16384):
        raise ValueError("viewport dimensions must be finite 320..16384 x 240..16384")
    return {"width": float(width), "height": float(height)}


def _position_pair(values, limit=60000.0):
    if len(values) != 8 or not _finite(values):
        return False
    x, y, z, w, tx, ty, tz, tw = values
    return (abs(w - 1.0) <= .001 and abs(tw - 1.0) <= .001
            and abs(x - tx) <= .05 and abs(z - tz) <= .05
            and abs(x) <= limit and abs(z) <= limit
            and abs(y) <= 1_000_000 and abs(ty) <= 1_000_000
            and .1 < y - ty <= 1_000_000)


def _view_triple(values, viewport, tolerance=2.0, any_viewports=False):
    if len(values) != 3 or not _finite(values):
        return False
    width, height, aspect = values
    if not (320 <= width <= 16384 and 240 <= height <= 16384
            and .5 <= aspect <= 5.0 and abs(width / height - aspect) <= .02):
        return False
    return any_viewports or (abs(width - viewport["width"]) <= tolerance
                             and abs(height - viewport["height"]) <= tolerance)


def candidates_from_chunk(blob, base, viewport, *, back=512, limit=128,
                          tolerance=2.0, any_viewports=False, use_numpy=True):
    """Find position/target vec4 pairs preceding a viewport float triple.

    This is a *shape* hypothesis inherited from the recovered algorithm, not
    evidence that FH5 has this layout. Search is 4-byte aligned relative to the
    supplied base. The caller should preserve ``back+12`` bytes at chunk edges.
    Returned bytes are from this one immutable read, avoiding a false claim of
    coherent camera fields assembled from separate ReadProcessMemory calls.
    """
    if type(base) is not int or base < 0 or base % 4:
        raise ValueError("base must be a nonnegative four-byte aligned integer")
    if not 32 <= back <= 2048 or not 1 <= limit <= 512:
        raise ValueError("back/limit outside bounded candidate limits")
    viewport = _viewport(viewport["width"], viewport["height"])
    if not math.isfinite(tolerance) or not 0 <= tolerance <= 128:
        raise ValueError("invalid viewport tolerance")
    data = bytes(blob[:len(blob) // 4 * 4])
    if len(data) < 44:
        return []
    np = _numpy() if use_numpy else None
    pairs, views = [], []
    if np is None:
        for offset in range(0, len(data) - 31, 4):
            if _position_pair(struct.unpack_from("<8f", data, offset)):
                pairs.append(offset)
        for offset in range(0, len(data) - 11, 4):
            if _view_triple(struct.unpack_from("<3f", data, offset), viewport,
                            tolerance, any_viewports):
                views.append(offset)
    else:
        a = np.frombuffer(data, dtype="<f4")
        with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
            # Cheap homogeneous-coordinate gate first. Expanding every float
            # to an (N,8) finite mask made a 15 s live scan process only ~464
            # MiB on this machine. Full checks now touch only rare gate hits.
            n = len(a) - 7
            ok = ((np.abs(a[3:n + 3] - 1) <= .001)
                  & (np.abs(a[7:n + 7] - 1) <= .001))
            indexes = np.flatnonzero(ok)
            if len(indexes):
                # Pages of repeated 1.0 also pass both w tests. Reject those
                # in vector code instead of unpacking millions of Python rows.
                gap = a[indexes + 1].astype(np.float64) - a[indexes + 5]
                indexes = indexes[(gap > .1) & (gap <= 1_000_000)]
            if len(indexes):
                dx = a[indexes].astype(np.float64) - a[indexes + 4]
                dz = a[indexes + 2].astype(np.float64) - a[indexes + 6]
                indexes = indexes[(np.abs(dx) <= .05) & (np.abs(dz) <= .05)]
            pairs = [int(i) * 4 for i in indexes
                     if _position_pair(struct.unpack_from("<8f", data, int(i) * 4))]
            n = len(a) - 2
            w, h, aspect = a[:n], a[1:n + 1], a[2:n + 2]
            ok = ((w >= 320) & (w <= 16384)
                  & (h >= 240) & (h <= 16384)
                  & (aspect >= .5) & (aspect <= 5.0))
            if not any_viewports:
                ok &= (np.abs(w - viewport["width"]) <= tolerance)
                ok &= (np.abs(h - viewport["height"]) <= tolerance)
            views = [int(i) * 4 for i in np.flatnonzero(ok)
                     if _view_triple(struct.unpack_from("<3f", data, int(i) * 4),
                                     viewport, tolerance, any_viewports)]
    out, seen = [], set()
    for view_offset in views:
        first = bisect_left(pairs, max(0, view_offset - back))
        last = bisect_right(pairs, view_offset - 32)
        # The closest eight shapes are retained; list truncation is explicit.
        for offset in reversed(pairs[max(first, last - 8):last]):
            key = (offset, view_offset)
            if key in seen:
                continue
            seen.add(key)
            values = struct.unpack_from("<8f", data, offset)
            view = struct.unpack_from("<3f", data, view_offset)
            end = min(len(data), max(view_offset + 12, offset + 192))
            raw = data[offset:end]
            out.append({
                "base": base + offset, "base_hex": hex(base + offset),
                "position": list(values[:4]), "target": list(values[4:]),
                "layout": {"position": 0, "target": 16,
                           "viewport": view_offset - offset},
                "viewport_address": base + view_offset,
                "viewport_values": list(view),
                "viewport_exact_match": (abs(view[0] - viewport["width"]) <= tolerance
                                           and abs(view[1] - viewport["height"]) <= tolerance),
                "raw_address": base + offset, "raw_hex": raw.hex(),
                "raw_size": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "shape_only": True, "verified": False,
                "map_open": None, "projection_verified": False,
            })
            if len(out) >= limit:
                return out
    return out


def _project(matrix, xyz):
    x, y, z = xyz
    w = x * matrix[3] + y * matrix[7] + z * matrix[11] + matrix[15]
    if not math.isfinite(w) or abs(w) <= 1e-8:
        return None
    result = ((x * matrix[0] + y * matrix[4] + z * matrix[8] + matrix[12]) / w,
              (x * matrix[1] + y * matrix[5] + z * matrix[9] + matrix[13]) / w)
    return result if _finite(result) else None


def matrix_shape(matrix, centre, viewport, *, probe=1000.0):
    """Rotation-tolerant top-down geometric score, never a verification."""
    if len(matrix) != 16 or not _finite(matrix) or max(map(abs, matrix)) > 1e6:
        return None
    x, y, z = centre
    points = [_project(matrix, p) for p in ((x, y, z), (x + probe, y, z),
                                           (x, y, z + probe), (x, y + 200, z))]
    if any(p is None for p in points):
        return None
    origin, east, north, up = points
    if max(map(abs, origin)) > .2:
        return None
    # Compare in pixels so rotated axes can still be orthogonal and isotropic.
    sx, sy = viewport["width"] / 2, viewport["height"] / 2
    e = ((east[0] - origin[0]) * sx, (east[1] - origin[1]) * sy)
    n = ((north[0] - origin[0]) * sx, (north[1] - origin[1]) * sy)
    le, ln = math.hypot(*e), math.hypot(*n)
    if not (.001 <= le / probe <= 100 and .001 <= ln / probe <= 100):
        return None
    cosine = abs((e[0] * n[0] + e[1] * n[1]) / (le * ln))
    ratio = le / ln
    if cosine > .04 or not .96 <= ratio <= 1.04:
        return None
    if math.hypot(up[0] - origin[0], up[1] - origin[1]) > .04:
        return None
    return {"centre_ndc": list(origin), "centre_error": math.hypot(*origin),
            "east_pixels_per_m": [v / probe for v in e],
            "north_pixels_per_m": [v / probe for v in n],
            "pixel_scale_x": le / probe, "pixel_scale_z": ln / probe,
            "basis_abs_cosine": cosine, "basis_scale_ratio": ratio,
            "verified": False, "projection_verified": False}


def nearby_matrices(blob, base, candidate, viewport, *, limit=16):
    """Return locally plausible 16-float windows under BOTH storage hypotheses.

    The complete raw window is persisted by the caller. This intentionally
    performs scalar work over at most 64 KiB and caps retained alternatives.
    A matrix may fit this geometry yet be stale or unrelated UI/streaming data.
    """
    if len(blob) > 65536 or not 1 <= limit <= 64:
        raise ValueError("matrix window/limit exceeds investigation budget")
    # Engine viewport values may be different from a scaled desktop capture.
    # In any-viewports mode using the operator's screenshot dimensions here
    # incorrectly rejects otherwise isotropic projections at another aspect.
    candidate_view = candidate.get("viewport_values")
    viewport_source = "operator_input_fallback"
    if candidate_view is not None and len(candidate_view) >= 2:
        viewport = _viewport(candidate_view[0], candidate_view[1])
        viewport_source = "candidate_viewport_float_triple"
    out = []
    pos = candidate["target"]
    centres = [("world_y_zero", (pos[0], 0.0, pos[2]))]
    if abs(pos[1]) > .01:
        centres.append(("target_y", tuple(pos[:3])))
    for offset in range(0, len(blob) - 63, 4):
        values = struct.unpack_from("<16f", blob, offset)
        if not _finite(values):
            continue
        for convention in ("row_vector", "column_vector_transposed"):
            matrix = values if convention == "row_vector" else tuple(
                values[row * 4 + col] for col in range(4) for row in range(4))
            for height_strategy, centre in centres:
                score = matrix_shape(matrix, centre, viewport)
                if score is None:
                    continue
                out.append({"address": base + offset, "address_hex": hex(base + offset),
                            "offset_from_camera": base + offset - candidate["base"],
                            "convention": convention, "height_strategy": height_strategy,
                            "viewport_hypothesis": dict(viewport, source=viewport_source),
                            "raw_float16": list(values), "matrix16_row_vector": list(matrix),
                            "raw_hex": blob[offset:offset + 64].hex(), **score})
    out.sort(key=lambda item: (item["centre_error"], abs(item["offset_from_camera"])))
    return out[:limit]


def package_version(image_path):
    """Ordinary XML from this image's folder; never read protected EXE payload."""
    manifest = Path(image_path).parent / "appxmanifest.xml"
    try:
        for item in ET.parse(manifest).getroot().iter():
            if (item.tag.rsplit("}", 1)[-1] == "Identity"
                    and item.attrib.get("Name") == "Microsoft.624F8B84B80"):
                return item.attrib.get("Version")
    except (OSError, ET.ParseError):
        pass
    return None


def select_regions(regions, *, minimum=0x10000, maximum=0x7fffffffffff,
                   max_region_bytes=8192 * MIB, region_bases=(), start=0):
    """Pure committed+writable+private+canonical metadata gate."""
    selected, excluded = [], {}
    explicit = set(region_bases)
    for item in regions:
        base, size = item.get("base"), item.get("size")
        reason = None
        if type(base) is not int or type(size) is not int or size <= 0:
            reason = "invalid_range"
        elif base < minimum or base + size - 1 > maximum or base + size <= base:
            reason = "noncanonical_range"
        elif item.get("state") != 0x1000 or item.get("type") != MEM_PRIVATE:
            reason = "not_committed_private"
        elif (item.get("protect", 0) & 0x101
              or item.get("protect", 0) & 0xff not in WRITABLE):
            reason = "not_unguarded_writable"
        elif size > max_region_bytes:
            reason = "region_exceeds_explicit_size_cap"
        elif explicit and base not in explicit:
            reason = "not_requested_region"
        elif base + size <= start:
            reason = "before_resume_address"
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            selected.append(dict(item, scan_start=max(base, start)))
    return sorted(selected, key=lambda r: r["scan_start"]), excluded


@dataclass
class ReadBudget:
    seconds: float
    byte_limit: int
    started: float = 0.0
    attempted_bytes: int = 0
    successful_bytes: int = 0
    calls: int = 0
    failures: int = 0

    def __post_init__(self):
        if not 0 < self.seconds <= 60 or not 0 < self.byte_limit <= 8192 * MIB:
            raise ValueError("budget exceeds 60 seconds / 8192 MiB hard cap")
        self.started = time.monotonic()

    @property
    def deadline(self):
        return self.started + self.seconds

    @property
    def remaining(self):
        return self.byte_limit - self.attempted_bytes

    def active(self):
        return time.monotonic() < self.deadline and self.remaining > 0

    def read(self, process, address, size):
        if not self.active() or size > self.remaining:
            return None
        self.calls += 1
        self.attempted_bytes += size  # Failed requests count against the cap.
        result = process.read(address, size)
        if result is None or len(result) != size:
            self.failures += 1
            return None
        self.successful_bytes += len(result)
        return result

    def summary(self):
        return {"seconds_limit": self.seconds, "byte_limit": self.byte_limit,
                "elapsed_seconds": time.monotonic() - self.started,
                "attempted_bytes": self.attempted_bytes,
                "successful_bytes": self.successful_bytes,
                "read_calls": self.calls, "read_failures": self.failures}


def _write_blob(directory, name, blob):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(blob)
    return {"file": str(path.resolve()), "size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}


def scan_process(process, args, viewport, raw_dir, budget):
    """Bounded scan, injectable for tests; does not discover or launch a PID."""
    regions = process.regions(deadline=budget.deadline)
    selected, excluded = select_regions(
        regions, minimum=process.min_address, maximum=process.max_address,
        max_region_bytes=args.max_region_mib * MIB,
        region_bases=args.region_base, start=args.start)
    hits, seen, visited = [], set(), 0
    resume = None
    stop_reason = "eligible_ranges_finished"
    overlap = args.back + 12
    # Reserve detail bytes so a broad scan does not consume every sample budget.
    reserve = min(budget.byte_limit // 8, args.details * (args.radius * 2 + 192))
    scan_limit = budget.byte_limit - reserve
    for region in selected:
        visited += 1
        cursor, end = region["scan_start"], region["base"] + region["size"]
        tail = b""
        while cursor < end:
            resume = cursor
            if not budget.active():
                stop_reason = "time_or_byte_budget"
                break
            available = scan_limit - budget.attempted_bytes
            if available < 44:
                stop_reason = "scan_byte_budget_reserved_for_details"
                break
            size = min(args.chunk_kib * 1024, end - cursor, available)
            size -= size % 4
            if size < 44:
                break
            blob = budget.read(process, cursor, size)
            if blob is not None:
                data, base = tail + blob, cursor - len(tail)
                candidates = candidates_from_chunk(
                    data, base, viewport, back=args.back, limit=args.max_candidates,
                    tolerance=args.view_tolerance, any_viewports=args.any_viewports)
                for item in candidates:
                    key = (item["base"], item["viewport_address"])
                    if key in seen:
                        continue
                    seen.add(key)
                    item["region_base"] = region["base"]
                    item["region_size"] = region["size"]
                    item["sample_monotonic"] = time.monotonic()
                    hits.append(item)
                    if len(hits) >= args.max_candidates:
                        break
                tail = data[-overlap:]
            else:
                # Never join bytes across an unreadable gap into one candidate.
                tail = b""
            cursor += size
            resume = cursor
            if len(hits) >= args.max_candidates:
                stop_reason = "candidate_limit"
                # The remainder of this chunk may contain discarded hits.
                # Resume at the last retained camera, rather than skip it.
                resume = min(cursor, hits[-1]["base"])
                break
        if stop_reason != "eligible_ranges_finished":
            break
    complete = stop_reason == "eligible_ranges_finished"
    if not complete and resume is not None:
        # A new invocation has no in-memory tail from the previous chunk.
        # Revisit this small overlap so a camera before the stop and its view
        # triple after it are not missed when the operator resumes the scan.
        resume = max(process.min_address, resume - overlap)
    for index, item in enumerate(hits[:args.details]):
        if not budget.active():
            break
        region = next(r for r in selected if r["base"] == item["region_base"])
        low = max(region["base"], item["base"] - args.radius)
        high = min(region["base"] + region["size"], item["base"] + args.radius + 192)
        size = high - low
        blob = budget.read(process, low, size)
        if blob is None:
            item["detail_error"] = dict(process.last_error or {}, detail="detail read unavailable")
            continue
        item["detail_sample_monotonic"] = time.monotonic()
        item["detail_address"] = low
        item["detail_raw"] = _write_blob(raw_dir, f"candidate_{index:03d}_{item['base']:x}.bin", blob)
        item["nearby_matrix_hypotheses"] = nearby_matrices(blob, low, item, viewport,
                                                          limit=args.matrix_limit)
    return {"candidates": hits, "candidate_count": len(hits),
            "regions_eligible": len(selected), "regions_visited": visited,
            "excluded_regions": excluded, "scan_complete": complete,
            "stop_reason": stop_reason, "resume_address": resume,
            "coverage_claim": "Only selected ranges; counts do not prove all map cameras found."}


def snapshot_process(process, args, viewport, raw_dir, budget):
    prior = None
    if args.from_report:
        prior = json.loads(Path(args.from_report).read_text(encoding="utf-8"))
        if prior.get("identity", {}).get("session_key") != process.identity["session_key"]:
            raise ValueError("saved report belongs to another process creation identity")
        if prior.get("package_version") != package_version(process.identity["image_path"]):
            raise ValueError("saved report package version differs from current image manifest")
    choices = []
    if prior is not None:
        candidates = prior.get("candidates", [])
        indexes = args.candidate_index if args.candidate_index else list(range(min(16, len(candidates))))
        for index in indexes:
            if not 0 <= index < len(candidates):
                raise ValueError("candidate index outside report")
            choices.append((candidates[index]["base"], candidates[index]))
    choices.extend((address, None) for address in args.address)
    choices = list({address: candidate for address, candidate in choices}.items())
    if not choices or len(choices) > 32:
        raise ValueError("snapshot requires 1..32 explicit addresses or saved candidates")
    regions = process.regions(deadline=budget.deadline)
    selected, excluded = select_regions(regions, minimum=process.min_address,
                                        maximum=process.max_address,
                                        max_region_bytes=args.max_region_mib * MIB)
    records = []
    for sample in range(args.samples):
        if not budget.active():
            break
        for address, candidate in choices:
            if not budget.active():
                break
            item = {"base": address, "sample": sample, "phase": args.phase,
                    "sample_monotonic": time.monotonic(), "verified": False}
            region = next((r for r in selected if r["base"] <= address < r["base"] + r["size"]), None)
            if region is None:
                item["error"] = "address outside selected committed writable private ranges"
                records.append(item)
                continue
            low = max(region["base"], address - args.radius)
            high = min(region["base"] + region["size"], address + args.radius + 192)
            blob = budget.read(process, low, high - low)
            item.update(raw_address=low, region_base=region["base"], region_size=region["size"])
            if blob is None:
                item["error"] = dict(process.last_error or {}, detail="exact read unavailable")
            else:
                item["raw"] = _write_blob(raw_dir, f"sample_{sample:03d}_{address:x}.bin", blob)
                item["current_shape_candidates"] = candidates_from_chunk(
                    blob, low, viewport, back=args.back, limit=32,
                    tolerance=args.view_tolerance, any_viewports=args.any_viewports)
                if candidate is not None:
                    # Re-decode the candidate from this SAME snapshot before testing matrices.
                    current = next((c for c in item["current_shape_candidates"]
                                    if c["base"] == address
                                    and c["layout"] == candidate["layout"]), None)
                    item["same_camera_shape_present"] = current is not None
                    if current is not None:
                        item["nearby_matrix_hypotheses"] = nearby_matrices(
                            blob, low, current, viewport, limit=args.matrix_limit)
            records.append(item)
        if sample + 1 < args.samples and budget.active():
            delay = min(args.interval, max(0, budget.deadline - time.monotonic()))
            if delay:
                time.sleep(delay)
    return {"snapshots": records, "sample_count": len(records),
            "addresses": [address for address, _ in choices],
            "excluded_regions": excluded, "verified": False,
            "comparison_note": "Compare raw bytes between explicit phases; changes alone do not prove map-open."}


def _integer(value):
    return int(value, 0)


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = ap.add_subparsers(dest="command", required=True)
    for command in ("scan", "snapshot"):
        p = commands.add_parser(command)
        p.add_argument("--pid", required=True, type=int)
        p.add_argument("--session-key", required=True)
        p.add_argument("--expect-version", default=None)
        p.add_argument("--width", required=True, type=float)
        p.add_argument("--height", required=True, type=float)
        p.add_argument("--phase", required=True, help="operator label, e.g. map_open / pan / closed")
        p.add_argument("--output", required=True, help="new JSON path; existing evidence is never overwritten")
        p.add_argument("--seconds", type=float, default=12)
        p.add_argument("--max-mib", type=int, default=512 if command == "scan" else 4)
        p.add_argument("--max-region-mib", type=int, default=8192)
        p.add_argument("--radius", type=int, default=8192 if command == "scan" else 4096)
        p.add_argument("--back", type=int, default=512)
        p.add_argument("--matrix-limit", type=int, default=16)
        p.add_argument("--view-tolerance", type=float, default=2)
        p.add_argument("--any-viewports", action="store_true")
        if command == "scan":
            p.add_argument("--chunk-kib", type=int, default=1024)
            p.add_argument("--max-candidates", type=int, default=128)
            p.add_argument("--details", type=int, default=24)
            p.add_argument("--region-base", type=_integer, action="append", default=[])
            p.add_argument("--start", type=_integer, default=0, help="resume address from same-session report")
        else:
            p.add_argument("--from-report")
            p.add_argument("--candidate-index", type=int, action="append", default=[])
            p.add_argument("--address", type=_integer, action="append", default=[])
            p.add_argument("--samples", type=int, default=1)
            p.add_argument("--interval", type=float, default=.2)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    viewport = _viewport(args.width, args.height)
    if (not 0 < args.seconds <= 60 or not 1 <= args.max_mib <= 8192
            or not 1 <= args.max_region_mib <= 32768 or not 64 <= args.radius <= 32672
            or not 32 <= args.back <= 2048 or not 1 <= args.matrix_limit <= 64
            or not math.isfinite(args.view_tolerance) or not 0 <= args.view_tolerance <= 128):
        raise ValueError("requested investigation bounds exceed hard caps")
    if args.command == "scan" and (not 4 <= args.chunk_kib <= 4096
                                   or not 1 <= args.max_candidates <= 512
                                   or not 0 <= args.details <= 64 or args.start % 4):
        raise ValueError("invalid bounded scan parameters")
    if args.command == "snapshot" and (not 1 <= args.samples <= 100
                                       or not .02 <= args.interval <= 2):
        raise ValueError("invalid bounded snapshot parameters")
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError("refusing to overwrite an existing evidence JSON")
    raw_dir = output.parent / (output.stem + "_raw")
    if raw_dir.exists():
        raise FileExistsError("refusing to overwrite an existing raw evidence directory")
    # This is the ONLY native dependency and the ONLY point a game handle opens.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from win_readonly import Process
    started = time.monotonic()
    report = {"schema": 1, "tool": "fh5_map_probe", "command": args.command,
              "filter_revision": "cheap_gate_2_candidate_viewport",
              "phase": args.phase, "notice": NOTICE, "verified": False,
              "projection_verified": False, "map_open": None,
              "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "viewport_input": viewport, "parameters": vars(args)}
    status = 0
    with Process(args.pid) as process:
        identity = process.identity
        if identity["session_key"] != args.session_key:
            raise ValueError("process creation identity mismatch: no payload reads performed")
        version = package_version(identity["image_path"])
        if args.expect_version is not None and version != args.expect_version:
            raise ValueError("package version mismatch: no payload reads performed")
        report.update(identity=identity, main_module=process.main_module,
                      package_version=version, metadata_warnings=process.metadata_warnings)
        budget = ReadBudget(args.seconds, args.max_mib * MIB)
        try:
            fn = scan_process if args.command == "scan" else snapshot_process
            report.update(fn(process, args, viewport, raw_dir, budget))
        except (OSError, TimeoutError, ProcessLookupError, ValueError) as error:
            report["error"] = {"type": type(error).__name__, "detail": str(error),
                               "last_read_error": process.last_error}
            report["scan_complete"] = False
            status = 2
        report["budget"] = budget.summary()
        report["process_still_alive"] = process.is_alive()
        report["session_reuse_allowed"] = False
    report["total_elapsed_seconds"] = time.monotonic() - started
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(output), "phase": args.phase,
                      "candidate_count": report.get("candidate_count"),
                      "sample_count": report.get("sample_count"),
                      "stop_reason": report.get("stop_reason"),
                      "budget": report["budget"], "verified": False,
                      "error": report.get("error")}, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
