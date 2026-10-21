"""Capture a few own-car FH5 Data Out UDP position samples on loopback only.

No process APIs, game setting changes, packet forwarding or external listeners
are used. Importing this file opens no socket. Capture must be invoked explicitly.
Only the evidenced 324-byte FH5 layout is accepted; unknown lengths are logged
and rejected. This is a parser implementation, not proof that this machine's
game build or a particular UDP sender uses that layout.

Example after manually configuring the game's Data Out target:
    python telemetry_capture.py --port 53005 --seconds 20 --samples 20
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import socket
import struct
import time


ROOT = Path(__file__).resolve().parent
BIND_ADDRESS = '127.0.0.1'
PACKET_BYTES = 324
POSITION_OFFSET = 244
MAX_DATAGRAMS = 8192
SOURCES = [
    {
        'url': 'https://github.com/xxr0ss/fh5_telemetry/blob/70b3242fadb1946fd044bee00e59d900b1363a91/fh5.dat',
        'supports': 'Author field order: 232-byte sled prefix, 12-byte Horizon placeholder, PositionXYZ at 244/248/252 and Speed at 256',
    },
    {
        'url': 'https://github.com/xxr0ss/fh5_telemetry/blob/70b3242fadb1946fd044bee00e59d900b1363a91/FH5.py.in',
        'supports': 'Author declares DATA_SIZE=324',
    },
    {
        'url': 'https://github.com/xxr0ss/fh5_telemetry/blob/70b3242fadb1946fd044bee00e59d900b1363a91/dev_tool.py',
        'supports': 'Author generates the binary struct format with little-endian prefix',
    },
    {
        'url': 'https://github.com/raweceek-temeletry/forza-horizon-5-UDP/blob/main/src/parser/FH5.packet.parser.ts',
        'supports': 'Independent FH5 parser explicitly reads little-endian fields and the same 12-byte Horizon gap before PositionXYZ',
    },
]


def parse_packet(data: bytes) -> dict:
    """Pure decoder with basic structure checks and explicit heuristic warnings."""
    if len(data) != PACKET_BYTES:
        raise ValueError('unsupported_packet_length')
    race_on, timestamp = struct.unpack_from('<iI', data, 0)
    if race_on not in (0, 1):
        raise ValueError('invalid_IsRaceOn')
    max_rpm, idle_rpm, rpm = struct.unpack_from('<3f', data, 8)
    velocity = struct.unpack_from('<3f', data, 32)
    yaw = struct.unpack_from('<f', data, 56)[0]
    car_ordinal, car_class, pi, drivetrain, cylinders = struct.unpack_from('<5i', data, 212)
    position = struct.unpack_from('<3f', data, POSITION_OFFSET)
    speed = struct.unpack_from('<f', data, 256)[0]
    floats = (*position, *velocity, yaw, speed, max_rpm, idle_rpm, rpm)
    if not all(math.isfinite(value) for value in floats):
        raise ValueError('nonfinite_basic_fields')
    if (car_ordinal < 0 or not 0 <= car_class <= 7 or not 0 <= pi <= 999
            or drivetrain not in (0, 1, 2) or not 0 <= cylinders <= 64):
        raise ValueError('invalid_basic_car_fields')
    if speed < 0 or min(max_rpm, idle_rpm, rpm) < 0:
        raise ValueError('negative_speed_or_rpm')
    # Broad corruption guards, not known map borders or game specification.
    if max(abs(value) for value in position) > 10_000_000 or speed > 1000:
        raise ValueError('outside_broad_diagnostic_limits')
    warnings = []
    velocity_speed = math.hypot(*velocity)
    if abs(velocity_speed - speed) > max(2.0, speed * 0.2):
        warnings.append('dash_speed_and_velocity_norm_disagree; verify layout before comparison')
    if all(value == 0 for value in position):
        warnings.append('zero_position; may be an origin, loading state or incompatible data')
    return {
        'timestamp_ms': timestamp, 'is_race_on': race_on,
        'position_x': position[0], 'position_y': position[1], 'position_z': position[2],
        'yaw': yaw, 'speed_m_per_s': speed,
        'local_velocity_xyz': list(velocity), 'velocity_norm_m_per_s': velocity_speed,
        'car_ordinal': car_ordinal, 'car_class': car_class, 'performance_index': pi,
        'warnings': warnings,
    }


def capture(port: int, seconds: float, max_samples: int, interval: float) -> dict:
    """Receive only; socket creation and bind occur exclusively in this call."""
    if not 49152 <= port <= 65535 or not 0 < seconds <= 30:
        raise ValueError('Use a high port 49152..65535 and duration >0..30 seconds')
    if not 1 <= max_samples <= 64 or not 0.1 <= interval <= 5:
        raise ValueError('Use 1..64 samples and interval 0.1..5 seconds')
    report = {
        'game': 'FH5', 'mode': 'own_car_data_out_loopback_udp',
        'started_utc': datetime.now(timezone.utc).isoformat(),
        'bind_address': BIND_ADDRESS, 'port': port, 'duration_limit_seconds': seconds,
        'sample_limit': max_samples, 'sample_interval_seconds': interval,
        'supported_packet_bytes': PACKET_BYTES,
        'byte_order': 'little', 'position_xyz_offsets': [244, 248, 252],
        'position_scale_and_axes': 'Reported game coordinates; compare movement and scale on this build before treating as absolute ground truth',
        'sources': SOURCES, 'samples': [], 'status': 'starting',
        'limitations': [
            'Only own-car telemetry is captured; Data Out is not a crate or other-car list',
            'Length and field plausibility cannot authenticate a sender as FH5; loopback UDP tuple is not a process session identity',
            'Parser sources are author implementations, not an independently verified FH5 developer protocol specification',
            'This file does not change game Data Out settings, loopback exemptions, firewall settings or any game memory',
            'A timeout is not evidence that Data Out is unsupported; configuration or local delivery may prevent packets',
            'All 323/311/331-byte or other unknown layouts are rejected rather than shifted or guessed',
        ],
    }
    lengths = Counter()
    rejected = Counter()
    datagrams = 0
    valid = 0
    last_saved_time = -math.inf
    last_timestamp = None
    accepted_peer = None
    start = time.monotonic()
    deadline = start + seconds
    stop = 'time_budget'
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((BIND_ADDRESS, port))
            while time.monotonic() < deadline and datagrams < MAX_DATAGRAMS:
                listener.settimeout(min(0.25, max(0.001, deadline - time.monotonic())))
                try:
                    # Receive the whole UDP datagram so a longer format cannot
                    # look like a valid 324-byte packet after truncation.
                    data, peer = listener.recvfrom(65535)
                except socket.timeout:
                    continue
                received = time.monotonic()
                datagrams += 1
                lengths[len(data)] += 1
                if peer[0] != BIND_ADDRESS:
                    rejected['non_loopback_peer'] += 1
                    continue
                try:
                    sample = parse_packet(data)
                except ValueError as error:
                    rejected[str(error)] += 1
                    continue
                if not sample['is_race_on']:
                    rejected['inactive_menu_or_race_stopped'] += 1
                    continue
                if accepted_peer is not None and peer != accepted_peer:
                    rejected['different_sender_tuple'] += 1
                    continue
                timestamp = sample['timestamp_ms']
                if last_timestamp is not None:
                    delta = (timestamp - last_timestamp) & 0xffffffff
                    if delta == 0 or delta > 0x80000000:
                        rejected['duplicate_or_out_of_order_timestamp'] += 1
                        continue
                accepted_peer = peer
                last_timestamp = timestamp
                valid += 1
                if received - last_saved_time < interval:
                    continue
                sample['received_utc'] = datetime.now(timezone.utc).isoformat()
                sample['capture_elapsed_seconds'] = round(received - start, 6)
                sample['packet_bytes'] = len(data)
                report['samples'].append(sample)
                last_saved_time = received
                if len(report['samples']) >= max_samples:
                    stop = 'sample_limit'
                    break
            if datagrams >= MAX_DATAGRAMS and stop != 'sample_limit':
                stop = 'datagram_limit'
        report['status'] = 'samples_captured' if report['samples'] else 'no_accepted_samples'
    except OSError as error:
        report['status'] = 'socket_error'
        report['error'] = str(error)
        report['winerror'] = getattr(error, 'winerror', None)
    report.update(
        finished_utc=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.monotonic() - start, 3), stop_reason=stop,
        datagrams_received=datagrams, valid_active_packets=valid,
        packet_lengths={str(key): count for key, count in sorted(lengths.items())},
        rejected_reasons=dict(rejected),
        sender_tuple=list(accepted_peer) if accepted_peer is not None else None,
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=53005)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--interval', type=float, default=0.5)
    args = parser.parse_args()
    try:
        report = capture(args.port, args.seconds, args.samples, args.interval)
    except ValueError as error:
        parser.error(str(error))
    folder = ROOT / 'sessions'
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    path = folder / (stamp + '_telemetry.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(path), 'status': report['status'],
                      'samples': len(report['samples'])}, ensure_ascii=False))
    return 0 if report['status'] == 'samples_captured' else 3


if __name__ == '__main__':
    raise SystemExit(main())
