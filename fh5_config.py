"""Local FH5 UI preferences; importing this module performs no writes."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

from fh5_paths import STATE_DIR
from fh5_controller import DEFAULT_BUTTON, valid_button
PATH = STATE_DIR / 'fh5_settings.json'
DEFAULTS = {'range': 5000.0, 'alpha': 0.82, 'size': 320,
            'visible': True, 'map_visible': True, 'port': 53005,
            'pad_button': DEFAULT_BUTTON, 'map_pad_button': 0x8000,
            'min_level': 4}


def validated(value):
    result = dict(DEFAULTS)
    if not isinstance(value, dict):
        return result
    for key, low, high in (('range', 200.0, 24000.0),
                           ('alpha', 0.2, 1.0), ('size', 220, 700),
                           ('port', 49152, 65535)):
        try:
            number = float(value.get(key, result[key]))
            if math.isfinite(number):
                result[key] = min(high, max(low, number))
        except (TypeError, ValueError):
            pass
    result['size'], result['port'] = int(result['size']), int(result['port'])
    for key in ('visible', 'map_visible'):
        flag = value.get(key, result[key])
        if type(flag) is bool:
            result[key] = flag
    for key in ('pad_button', 'map_pad_button'):
        button = value.get(key, result[key])
        if valid_button(button):
            result[key] = button
    minimum = value.get('min_level', result['min_level'])
    if type(minimum) is int and 4 <= minimum <= 10:
        result['min_level'] = minimum
    return result


def load():
    try:
        return validated(json.loads(PATH.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(value):
    """Replace only this application's own config; keep all other files."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=STATE_DIR, prefix='fh5_settings_',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(validated(value), handle, ensure_ascii=False, indent=2)
        os.replace(temporary, PATH)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
