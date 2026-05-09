"""KI-118 regression: production code must not import an untracked module.

This catches the failure mode where source files live in the working tree
on the deployment host but were never `git add`-ed. See
`legacy/RESOLVED_ISSUES_ARCHIVE.md` "KI-118" for the original incident.

Three checks:

1. ``test_no_production_py_imports_untracked_module``
   Walk every tracked .py file outside ``tests/``, ``legacy/``,
   ``.claude/local_scripts/``, and ``venv``/``.venv``. For each
   ``import a.b.c`` / ``from a.b import c`` that resolves to a file
   inside this repo, assert that file is in ``git ls-files``.
   Imports that resolve to stdlib or pip dependencies are skipped
   (their target path doesn't exist in the repo root).

2. ``test_no_untracked_systemd_units``
   Every ``.service`` / ``.timer`` file on disk under ``systemd/``
   must be tracked. KI-118 had five untracked unit files installed
   into ``/etc/systemd/system/`` while their repo copies were never
   staged.

3. ``test_deployed_systemd_units_have_tracked_source``
   When running on the production host (``/etc/systemd/system/`` or
   ``~/.config/systemd/user/`` exist), for every deployed
   ``mhde-*.service`` / ``mhde-*.timer`` whose ``ExecStart`` references
   the repo, assert the matching repo source in ``systemd/`` is tracked.
   Skipped gracefully off-host (mirrors the pattern in
   ``test_active_model_paths_resolve``).
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()
)

EXCLUDED_TOP_LEVEL_DIRS = {"tests", "legacy", "venv", ".venv"}


def _git_ls_files() -> set[Path]:
    out = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "ls-files"], text=True
    )
    return {REPO_ROOT / line for line in out.splitlines() if line}


def _is_excluded(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in EXCLUDED_TOP_LEVEL_DIRS:
        return True
    if len(parts) >= 2 and parts[0] == ".claude" and parts[1] == "local_scripts":
        return True
    return False


def _production_py_files(tracked: set[Path]) -> list[Path]:
    return sorted(
        p for p in tracked
        if p.suffix == ".py" and not _is_excluded(p.relative_to(REPO_ROOT))
    )


def _resolve_module(name: str) -> Path | None:
    """Return the repo path for module ``name`` if it resolves inside the
    repo, else None. Stdlib and pip-installed deps return None."""
    parts = name.split(".")
    candidates = [
        REPO_ROOT.joinpath(*parts).with_suffix(".py"),
        REPO_ROOT.joinpath(*parts, "__init__.py"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _imports_in(path: Path) -> set[str]:
    """Return the set of importable module names referenced by ``path``."""
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return set()

    names: set[str] = set()
    pkg_parts = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative import. Resolve against the package the file lives in.
                if node.level > len(pkg_parts):
                    continue  # malformed; would step above repo root
                base = pkg_parts[: len(pkg_parts) - node.level + 1]
                base_full: tuple[str, ...] = (*base, node.module) if node.module else base
            elif node.module:
                base_full = tuple(node.module.split("."))
            else:
                continue
            if base_full:
                names.add(".".join(base_full))
                # `from a.b import c` may be importing a submodule `c` of
                # package `a.b`. Add that candidate too — if it doesn't
                # resolve to a file under the repo, _resolve_module returns
                # None and it is skipped.
                for alias in node.names:
                    if alias.name != "*":
                        names.add(".".join((*base_full, alias.name)))
    return names


def test_no_production_py_imports_untracked_module():
    tracked = _git_ls_files()
    py_files = _production_py_files(tracked)
    failures: list[str] = []
    for py in py_files:
        for name in _imports_in(py):
            target = _resolve_module(name)
            if target is None:
                continue
            if target not in tracked:
                failures.append(
                    f"{py.relative_to(REPO_ROOT)}: import '{name}' -> "
                    f"{target.relative_to(REPO_ROOT)} (untracked)"
                )
    assert not failures, (
        "Production code imports modules whose source files are NOT tracked "
        "by git (KI-118 regression). Either `git add` the file or remove "
        "the import:\n  " + "\n  ".join(failures)
    )


def test_no_untracked_systemd_units():
    tracked = _git_ls_files()
    systemd_dir = REPO_ROOT / "systemd"
    if not systemd_dir.is_dir():
        pytest.skip("repo has no systemd/ directory")
    units_on_disk = [
        p for p in systemd_dir.rglob("*")
        if p.is_file() and p.suffix in {".service", ".timer"}
    ]
    untracked = sorted(p for p in units_on_disk if p not in tracked)
    assert not untracked, (
        "systemd unit files exist on disk in systemd/ but are NOT tracked "
        "by git (KI-118 regression):\n  "
        + "\n  ".join(str(p.relative_to(REPO_ROOT)) for p in untracked)
    )


def test_deployed_systemd_units_have_tracked_source():
    deploy_dirs = [
        Path("/etc/systemd/system"),
        Path.home() / ".config" / "systemd" / "user",
    ]
    available = [d for d in deploy_dirs if d.is_dir()]
    if not available:
        pytest.skip("no systemd deploy directory present (not on production host)")

    tracked = _git_ls_files()
    failures: list[str] = []
    for d in available:
        for unit in (*sorted(d.glob("mhde-*.service")), *sorted(d.glob("mhde-*.timer"))):
            try:
                content = unit.read_text()
            except (PermissionError, OSError):
                continue
            if str(REPO_ROOT) not in content:
                continue
            source = REPO_ROOT / "systemd" / unit.name
            if not source.exists():
                continue
            if source not in tracked:
                failures.append(
                    f"deployed unit {unit} references "
                    f"{source.relative_to(REPO_ROOT)} which is untracked"
                )
    assert not failures, (
        "Deployed mhde-* units have untracked source files in systemd/ "
        "(KI-118 regression):\n  " + "\n  ".join(failures)
    )
