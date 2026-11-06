"""Directory scanning shared by the server manifest and the client sync engine."""
from __future__ import annotations

import os
from pathlib import Path

IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".dsstate"}
IGNORED_SUFFIXES = (".dspart",)


def ignored(name: str) -> bool:
    return name in IGNORED_NAMES or name.endswith(IGNORED_SUFFIXES)


def scan_tree(base: Path) -> dict[str, dict]:
    """Return {relative_posix_path: {type, size, mtime}} for everything under base."""
    out: dict[str, dict] = {}
    stack = [Path(base)]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if ignored(e.name):
                continue
            try:
                if e.is_symlink():
                    continue
                rel = Path(e.path).relative_to(base).as_posix()
                if e.is_dir(follow_symlinks=False):
                    out[rel] = {"type": "dir", "size": 0, "mtime": 0.0}
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    out[rel] = {"type": "file", "size": st.st_size, "mtime": st.st_mtime}
            except OSError:
                continue
    return out
