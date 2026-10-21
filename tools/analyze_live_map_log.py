"""One-shot bounded JSONL display-log summary; no process, UI or socket access.

Use production map_markers to distinguish current projectable source points
from the actual Canvas points recorded by FrameRecorder. A timestamp just
crossing its TTL between drawing and recording is diagnostic, not proof of a
renderer bug. Recent seconds are measured relative to the newest logged stamp;
log_age_seconds separately reports whether the append stream has stopped.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from fh5_map_model import MapFrame, map_markers

TTL = .25
MAX_POLICY_ROWS = 1024


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def read_tail(path, max_bytes=8*1024*1024):
    """Read a fixed byte snapshot of an appending log, ignoring partial lines."""
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 64*1024*1024:
        raise ValueError('tail budget must be 1KiB..64MiB')
    with Path(path).open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        offset = max(0, size-max_bytes)
        stream.seek(offset)
        raw = stream.read(size-offset)
    skipped_prefix = False
    if offset:
        newline = raw.find(b'\n')
        raw = raw[newline+1:] if newline >= 0 else b''
        skipped_prefix = True
    rows, invalid = [], 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict) and finite(row.get('monotonic')):
                rows.append(row)
            else:
                invalid += 1
        except (ValueError, UnicodeError):
            invalid += 1
    return rows, {'file_bytes_at_open': size, 'tail_start_byte': offset,
                  'tail_byte_budget': max_bytes, 'skipped_partial_prefix': skipped_prefix,
                  'ignored_nonrecord_or_partial_lines': invalid}


def age(stamp, now):
    return now-stamp if finite(stamp) and finite(now) else None


def fresh(value):
    return finite(value) and 0 <= value < TTL


def read_health(record):
    gaps, limited, error = (record.get(key) for key in ('read_gaps', 'refresh_limited', 'last_error'))
    if ((type(gaps) is int and gaps > 0) or limited is True
            or ('last_error' in record and error is not None)):
        return 'unhealthy'
    if (type(gaps) is int and gaps == 0 and limited is False
            and 'last_error' in record and error is None):
        return 'healthy'
    return 'unknown'


def frame_summary(record):
    """Pure single-record source/presentation diagnosis, no retained state."""
    now = record.get('monotonic')
    raw_frame = record.get('map_frame')
    render = record.get('map_render')
    render = render if isinstance(render, dict) else None
    points = render.get('points') if render is not None else None
    points_count = len(points) if isinstance(points, list) else None
    rows = record.get('rows')
    rows_count = len(rows) if isinstance(rows, list) else None
    source = dict(record)
    if isinstance(rows, list):
        source['rows'] = rows[:MAX_POLICY_ROWS]
    threshold = record.get('display_min_level', render.get('min_level', 0) if render else 0)
    policy = map_markers(source, raw_frame, now, min_level=threshold) if finite(now) else {
        'reason': 'invalid_record_time', 'markers': []}
    policy_count = len(policy.get('markers') or [])
    sample_age = activity_age = None
    schema_valid = False
    foreground = verified = map_open = None
    camera_address = None
    if isinstance(raw_frame, dict):
        sample_age = age(raw_frame.get('sample_monotonic'), now)
        activity_age = age(raw_frame.get('activity_sample_monotonic'), now)
        foreground, verified, map_open = (raw_frame.get(k) for k in
                                         ('game_foreground', 'projection_verified', 'map_open'))
        address = raw_frame.get('camera_address')
        if type(address) is int and 0 < address <= 0x7fffffffffff:
            camera_address = hex(address)
        elif isinstance(address, str) and address.startswith('0x'):
            camera_address = address
        try:
            MapFrame.from_mapping(raw_frame)
            schema_valid = True
        except (ValueError, TypeError, OverflowError, KeyError):
            pass
    reason = policy.get('reason', 'invalid_input')
    camera_valid = bool(schema_valid and fresh(sample_age) and fresh(activity_age)
                        and reason in ('ready', 'no_visible_markers'))
    eligible_points = policy_count if camera_valid else 0
    user_hidden = record.get('map_visible') is False
    if user_hidden:
        eligible_points = 0
    if schema_valid and reason in ('ready', 'no_visible_markers') and not fresh(activity_age):
        reason = 'activity_timestamp_missing_stale_or_invalid'
    if points_count is not None and points_count > 0:
        category = ('source_and_render_points' if eligible_points > 0
                    else 'render_points_without_current_eligible_source')
    elif eligible_points > 0:
        category = ('source_points_but_render_empty' if points_count == 0
                    else 'source_points_but_renderer_not_recorded')
    elif user_hidden:
        category = 'map_display_disabled'
    elif not isinstance(raw_frame, dict):
        category = 'camera_unavailable'
    elif foreground is False:
        category = 'game_background_hidden'
    elif verified is not True:
        category = 'projection_not_authorized'
    elif not camera_valid:
        category = 'source_frame_invalid_or_expired'
    elif rows_count == 0:
        category = 'valid_camera_no_rows'
    else:
        category = 'valid_camera_rows_not_projectable'
    return {'monotonic': now, 'utc': record.get('utc'),
            'session_key': record.get('session_key'), 'process_pid': record.get('process_pid'),
            'map_state': record.get('map_state'), 'map_reason': record.get('map_reason'),
            'source_frame_available': isinstance(raw_frame, dict),
            'source_frame_schema_valid': schema_valid, 'source_frame_effectively_valid': camera_valid,
            'camera_address': camera_address, 'foreground': foreground,
            'map_open': map_open, 'projection_verified': verified,
            'min_level': threshold, 'map_visible': record.get('map_visible'),
            'camera_age_seconds': sample_age, 'activity_age_seconds': activity_age,
            'camera_ttl_fresh': fresh(sample_age), 'activity_ttl_fresh': fresh(activity_age),
            'source_policy_reason': reason, 'source_policy_projectable_points': policy_count,
            'eligible_source_points': eligible_points, 'actual_map_render_points': points_count,
            'actual_visible_item_count': render.get('visible_item_count') if render is not None else None,
            'map_render_reason': render.get('reason') if render is not None else None,
            'map_render_frame_available': render.get('frame_available') if render is not None else None,
            'rows_count': rows_count, 'rows_truncated_for_policy': bool(rows_count and rows_count > MAX_POLICY_ROWS),
            'read_health': read_health(record), 'read_gaps': record.get('read_gaps'),
            'refresh_limited': record.get('refresh_limited'), 'last_error': record.get('last_error'),
            'memory_state': record.get('memory_state'), 'telemetry_age': record.get('telemetry_age'),
            'replay': record.get('replay'), 'classification': category}


def summarize(records, seconds=10, *, now=None):
    if not finite(seconds) or not 0 < seconds <= 600:
        raise ValueError('summary window must be 0..600 seconds')
    valid = [record for record in records if isinstance(record, dict) and finite(record.get('monotonic'))]
    if not valid:
        return {'record_count': 0, 'error': 'no_timestamped_records'}
    end = max(record['monotonic'] for record in valid)
    window = sorted((record for record in valid if 0 <= end-record['monotonic'] <= seconds),
                    key=lambda record: record['monotonic'])
    frames = [frame_summary(record) for record in window]
    latest = frames[-1]
    def counts(key):
        return dict(Counter(str(frame.get(key)) for frame in frames))
    transitions, previous = [], None
    for frame in frames:
        key = tuple(frame.get(k) for k in ('map_state', 'map_reason', 'classification', 'camera_address'))
        if key != previous:
            transitions.append({k: frame.get(k) for k in
                ('monotonic', 'map_state', 'map_reason', 'classification', 'camera_address')})
            previous = key
    observed_age = age(end, now) if now is not None else None
    result = {'record_count': len(frames), 'requested_window_seconds': seconds,
              'window_start_monotonic': frames[0]['monotonic'], 'window_end_monotonic': end,
              'observed_window_seconds': end-frames[0]['monotonic'],
              'log_age_seconds': observed_age,
              'latest': latest, 'map_state_counts': counts('map_state'),
              'map_reason_counts': counts('map_reason'), 'classification_counts': counts('classification'),
              'read_health_counts': counts('read_health'),
              'effective_frame_records': sum(frame['source_frame_effectively_valid'] for frame in frames),
              'source_points_but_render_empty_records': sum(frame['classification'] == 'source_points_but_render_empty' for frame in frames),
              'render_points_without_current_source_records': sum(frame['classification'] == 'render_points_without_current_eligible_source' for frame in frames),
              'transitions': transitions[-24:],
              'note': 'Source policy is recomputed with production map_markers; Canvas points are separately recorded outputs. TTL-boundary differences alone do not prove a bug.'}
    for key in ('rows_count', 'eligible_source_points', 'actual_map_render_points',
                'camera_age_seconds', 'activity_age_seconds'):
        values = [frame[key] for frame in frames if finite(frame[key])]
        result[key+'_range'] = [min(values), max(values)] if values else None
    return result


def compact(summary):
    if not summary.get('record_count'):
        return 'No timestamped display records in bounded log tail.'
    last = summary['latest']
    def number(value):
        return f'{value:.3f}' if finite(value) else '?'
    return '\n'.join((
        f"frames={summary['record_count']} window={number(summary['observed_window_seconds'])}s log_age={number(summary['log_age_seconds'])}s | state={last['map_state']} reason={last['map_reason']}",
        f"frame_valid={last['source_frame_effectively_valid']} camera={last['camera_address']} foreground={last['foreground']} ttl_age={number(last['camera_age_seconds'])}/{number(last['activity_age_seconds'])}s | source_points={last['eligible_source_points']} render_points={last['actual_map_render_points']} items={last['actual_visible_item_count']}",
        f"rows={last['rows_count']} health={last['read_health']} gaps={last['read_gaps']} limited={last['refresh_limited']} | {last['classification']} | source_reason={last['source_policy_reason']} render_reason={last['map_render_reason']}",
        'window_classifications='+json.dumps(summary['classification_counts'], ensure_ascii=False)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--max-tail-mib', type=int, default=8)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    records, tail = read_tail(args.log, args.max_tail_mib*1024*1024)
    report = summarize(records, args.seconds, now=time.monotonic())
    report.update(log_file=str(args.log.resolve()), tail=tail)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) if args.json else compact(report))
    return 0 if report.get('record_count') else 2


if __name__ == '__main__':
    raise SystemExit(main())
