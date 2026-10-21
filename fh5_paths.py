"""Keep writable state beside the app, resources inside a frozen bundle."""
from pathlib import Path
import sys

RESOURCE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
STATE_DIR = APP_DIR / 'state'
