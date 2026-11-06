"""Storage accounting for the server: usage cache, quota and free-space guards.

Admins decide how much of the machine the cloud may use (important on small hosts that
only have a few hundred MB for web hosting):

  quota_mb            hard cap for the main storage folder (0 = unlimited)
  min_free_mb         always keep this much of the disk free (0 = off)
  max_upload_mb       cap for one file (0 = unlimited)
  blocked_extensions  file types that may not be stored
  user_writes         whether device users may change files (off = admin only)
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

MB = 1024 * 1024


def dir_size(base: Path) -> int:
    """Total bytes of every regular file below base (symlinks are not followed)."""
    total, stack = 0, [str(base)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def breakdown(base: Path, limit: int = 25) -> list[dict]:
    """Largest top-level entries of base: [{name, type, size}], biggest first."""
    items = []
    try:
        with os.scandir(base) as it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        items.append({"name": e.name, "type": "dir", "size": dir_size(Path(e.path))})
                    elif e.is_file(follow_symlinks=False):
                        items.append({"name": e.name, "type": "file", "size": e.stat(follow_symlinks=False).st_size})
                except OSError:
                    continue
    except OSError:
        pass
    items.sort(key=lambda i: i["size"], reverse=True)
    return items[:limit]


def disk_info(base: Path) -> dict:
    try:
        du = shutil.disk_usage(base)
        return {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        return {}


class UsageCache:
    """Caches dir_size() for a few seconds so uploads do not rescan the tree every time."""

    def __init__(self, ttl: float = 15.0):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, int]] = {}

    def used(self, base: Path) -> int:
        key = str(base)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        size = dir_size(base)
        with self._lock:
            self._cache[key] = (time.monotonic(), size)
        return size

    def invalidate(self):
        with self._lock:
            self._cache.clear()


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


def check_space(*, used: int, quota: int, free: int, reserve: int, size: int):
    """Raise ValueError (with a user-facing message) if storing `size` more bytes breaks a limit.

    used    bytes already stored (excluding the file being replaced)
    quota   cap in bytes, 0 = none
    free    bytes free on the disk, only meaningful when reserve > 0
    reserve bytes of disk that must stay free, 0 = off
    """
    if quota and used + size > quota:
        raise ValueError(f"Storage quota exceeded ({human(used)} used of {human(quota)}). "
                         "Delete something or ask the admin to raise the quota.")
    if reserve and free - size < reserve:
        raise ValueError(f"Not enough free disk space: the server keeps {human(reserve)} in reserve.")
