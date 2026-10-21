"""Windowed FH5 entry, also used to build the standalone executable."""
from pathlib import Path
import sys
import traceback
from fh5_paths import STATE_DIR


def main():
    STATE_DIR.mkdir(exist_ok=True, parents=True)
    log = open(STATE_DIR / 'fh5_app.log', 'a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr = log
    try:
        from fh5_app import main as launch
        return launch()
    except Exception:
        traceback.print_exc()
        from tkinter import messagebox
        messagebox.showerror('FH5 车包坐标', '启动失败，详情已保存到 state/fh5_app.log。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
