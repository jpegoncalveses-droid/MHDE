"""Tests for crypto.exports._io — atomic JSON writes and symlink replace.

POSIX-atomic ops via os.replace on the same filesystem. Tested by:
- producing a complete file on the destination path (no partial reads)
- replacing an existing symlink silently
- replacing an existing regular file with a symlink (initial bootstrap)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto.exports._io import atomic_write_json, atomic_replace_symlink


def test_atomic_write_json_creates_file(tmp_path):
    dst = tmp_path / "out.json"
    atomic_write_json(dst, {"a": 1, "b": [2, 3]})
    assert dst.exists()
    assert json.loads(dst.read_text()) == {"a": 1, "b": [2, 3]}


def test_atomic_write_json_overwrites_existing(tmp_path):
    dst = tmp_path / "out.json"
    dst.write_text('{"old": true}')
    atomic_write_json(dst, {"new": True})
    assert json.loads(dst.read_text()) == {"new": True}


def test_atomic_write_json_no_temp_file_left_behind(tmp_path):
    dst = tmp_path / "out.json"
    atomic_write_json(dst, {"x": 1})
    leftover = list(tmp_path.glob("out.json.tmp.*"))
    assert leftover == [], f"temp files leaked: {leftover}"


def test_atomic_replace_symlink_creates_new_link(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "latest.json"
    atomic_replace_symlink(link, "target.json")
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_atomic_replace_symlink_replaces_existing_symlink(tmp_path):
    a = tmp_path / "a.json"
    a.write_text('{"v": "a"}')
    b = tmp_path / "b.json"
    b.write_text('{"v": "b"}')
    link = tmp_path / "latest.json"
    atomic_replace_symlink(link, "a.json")
    atomic_replace_symlink(link, "b.json")
    assert link.is_symlink()
    assert json.loads(link.read_text()) == {"v": "b"}


def test_atomic_replace_symlink_replaces_regular_file(tmp_path):
    """Initial bootstrap case: a regular file at the symlink path is
    replaced silently."""
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "latest.json"
    link.write_text('{"old_regular_file": true}')
    atomic_replace_symlink(link, "target.json")
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_atomic_replace_symlink_uses_relative_target(tmp_path):
    """Symlink target is stored as the relative name passed in, not
    an absolute path. This keeps the link valid if data/exports/ is
    moved or rsync'd."""
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "latest.json"
    atomic_replace_symlink(link, "target.json")
    assert link.readlink() == Path("target.json")
