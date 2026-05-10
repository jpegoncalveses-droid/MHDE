"""Atomic file operations for the export pipeline.

All ops use ``os.replace`` for POSIX-atomic rename on the same
filesystem. Failure modes (disk full, permission denied) bubble up;
caller is responsible for not modifying outputs on preflight failure.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

EXPORTS_DIR = Path("/home/jpcg/MHDE/data/exports")


def atomic_write_json(path: Path, obj) -> None:
    """Write ``obj`` as JSON to ``path`` atomically.

    Strategy: write to ``<path>.tmp.<pid>``, fsync, ``os.replace`` to
    the final path. Concurrent readers either see the old file or the
    new file, never a partial write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_replace_symlink(link_path: Path, target_name: str) -> None:
    """Atomically (re)create a symlink at ``link_path`` pointing at
    ``target_name`` (relative).

    Strategy: ``os.symlink`` to a temp link, ``os.replace`` to the
    final link path. Replaces existing symlinks AND existing regular
    files silently (the latter for initial bootstrap).
    """
    link_path = Path(link_path)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = link_path.with_name(f"{link_path.name}.tmp.{os.getpid()}")
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target_name, tmp)
        os.replace(tmp, link_path)
    finally:
        if tmp.is_symlink() or tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
