"""Conservative persisted guard against repeated controller marks.

There is no verified match identifier yet. Once a sample has been saved, this
guard stays locked across empty frames, process changes and application restarts.
It deliberately has no inferred or automatic unlock operation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import uuid


LATCH_FILE = 'round_latch.json'
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 4096


def _valid_id(value):
    return type(value) is str and 0 < len(value) <= 128 and bool(value.strip())


def _valid_hash(value):
    return (type(value) is str and len(value) == 64
            and all(character in '0123456789abcdefABCDEF' for character in value))


class RoundLatch:
    """Read-only construction; lock permanently on the first persisted sample.

    ``sync`` consumes the collector's last *successfully persisted* sample, not a
    candidate frame. Storage trouble always fails closed and is reported through
    ``status``; it never propagates into the radar's rendering or scanning loop.
    Only a sample ID and optional snapshot digest are retained, never coordinates.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self._lock = threading.RLock()
        self._locked = False
        self._error = None
        self._sample_id = None
        self._snapshot_hash = None
        try:
            with (self.directory / LATCH_FILE).open('rb') as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError('latch file exceeds bounded size')
            record = json.loads(data.decode('utf-8-sig'))
            if (type(record) is not dict
                    or type(record.get('schema_version')) is not int
                    or record['schema_version'] != SCHEMA_VERSION
                    or record.get('locked') is not True
                    or not _valid_id(record.get('sample_id'))
                    or ('snapshot_hash' in record
                        and not _valid_hash(record['snapshot_hash']))
                    or set(record) - {'schema_version', 'locked', 'sample_id', 'snapshot_hash'}):
                raise ValueError('invalid latch file; existing file preserved')
            self._locked = True
            self._sample_id = record['sample_id']
            self._snapshot_hash = record.get('snapshot_hash')
        except FileNotFoundError:
            # Missing state is reconciled against the collector by sync before
            # any mark is accepted. Construction never creates a directory.
            pass
        except (OSError, ValueError, UnicodeError, TypeError) as exc:
            self._fail_closed('load_failed', exc)

    def _fail_closed(self, stage, exc):
        self._locked = True
        self._error = f'{stage}: {type(exc).__name__}: {str(exc)[:180]}'

    def allowed(self):
        with self._lock:
            return not self._locked and self._error is None

    def status(self):
        with self._lock:
            return {'locked': self._locked, 'error': self._error,
                    'sample_id': self._sample_id}

    def sync(self, last_sample):
        """Reconcile persisted collector state and return a detached status dict.

        Repeating the same sample, passing None, or presenting a newer process
        session never releases the lock or rewrites the latch. A malformed sample
        or failed write also locks this instance instead of permitting a duplicate.
        """
        with self._lock:
            if self._locked or last_sample is None:
                return self.status()
            try:
                if (type(last_sample) is not dict
                        or not _valid_id(last_sample.get('match_id'))
                        or ('snapshot_hash' in last_sample
                            and not _valid_hash(last_sample['snapshot_hash']))):
                    raise ValueError('invalid persisted sample identity')
                self._sample_id = last_sample['match_id']
                self._snapshot_hash = last_sample.get('snapshot_hash')
                # Lock in memory before I/O. Even failed storage must not allow
                # another controller press to create a second observation.
                self._locked = True
                record = {'schema_version': SCHEMA_VERSION, 'locked': True,
                          'sample_id': self._sample_id}
                if self._snapshot_hash is not None:
                    record['snapshot_hash'] = self._snapshot_hash
                self._atomic_write(record)
            except (OSError, ValueError, UnicodeError, TypeError) as exc:
                self._fail_closed('save_failed', exc)
            return self.status()

    def _atomic_write(self, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / LATCH_FILE
        temporary = self.directory / f'.round_latch.{uuid.uuid4().hex}.tmp'
        try:
            with temporary.open('x', encoding='utf-8', newline='\n') as stream:
                json.dump(record, stream, ensure_ascii=True, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
