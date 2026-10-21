"""Lazy, verified Tk images of the original locally extracted FH5 crests.

Importing this module performs no filesystem, GUI, process or network work.
The extraction tool uses Pillow; the application uses Tk's PNG decoder only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


SUPPORTED_SIZES = (24, 28, 32, 40, 48)


class TierIcons:
    def __init__(self, master, size: int = 32, *, root=None, photo_factory=None):
        if type(size) is not int or size not in SUPPORTED_SIZES:
            raise ValueError("unsupported original crest size")
        self.master = master
        self.size = size
        self.root = Path(root) if root is not None else Path(__file__).resolve().parent / "data/tier_icons"
        self.photo_factory = photo_factory
        self._records = None
        self._images = {}
        self.error = None

    def _manifest(self):
        path = self.root / "manifest.json"
        if path.stat().st_size > 100000:
            raise ValueError("oversized tier icon manifest")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != 1 or data.get("original_game_assets") is not True or data.get("variant") != "Spatial":
            raise ValueError("unverified original tier icon manifest")
        icons = data.get("icons")
        if not isinstance(icons, list) or len(icons) != 10:
            raise ValueError("ten original crests are required")
        records = {}
        for icon in icons:
            if not isinstance(icon, dict):
                raise ValueError("invalid original crest record")
            level = icon.get("level")
            if type(level) is not int or level not in range(1, 11) or level in records:
                raise ValueError("invalid original crest level")
            sizes = icon.get("sizes")
            if not isinstance(sizes, dict):
                raise ValueError("invalid original crest sizes")
            record = sizes.get(str(self.size), {})
            if not isinstance(record, dict):
                raise ValueError("invalid original crest cache record")
            if record.get("file") != f"{self.size}/level_{level}.png" or record.get("width") != self.size or record.get("height") != self.size:
                raise ValueError("invalid original crest cache path or dimensions")
            digest = record.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid original crest digest")
            records[level] = record
        return records

    def get(self, level: int):
        """Return a persistent PhotoImage, or None if the original is unavailable."""
        if type(level) is not int or level not in range(1, 11):
            return None
        if level in self._images:
            return self._images[level]
        try:
            if self._records is None:
                self._records = self._manifest()
            record = self._records[level]
            path = self.root / record["file"]
            if not 1 <= path.stat().st_size <= 65536:
                raise ValueError("unsafe tier icon PNG size")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("original tier icon PNG digest mismatch")
            factory = self.photo_factory
            if factory is None:
                from tkinter import PhotoImage
                factory = PhotoImage
            image = factory(master=self.master, data=data, format="png")
            if image.width() != self.size or image.height() != self.size:
                raise ValueError("original tier icon dimensions mismatch")
            self._images[level] = image
            self.error = None
            return image
        except Exception as error:
            # A missing/corrupted resource or Tk PNG failure must not break the
            # overlay render loop. Cache the failure to avoid retrying disk I/O
            # every frame. A fresh renderer retries on the next application run.
            self._images[level] = None
            self.error = str(error)
            return None
