"""Session 0 hold-the-line: nothing in legacy/ is imported by ACTIVE code.

This is the standing guard against accidental re-coupling. If anyone
adds `from legacy.X import Y` to active code, this test fails loudly
before the regression makes it to a review.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache",
             ".claude", "samples", "data", "outputs", "reports",
             "node_modules", ".worktrees", "legacy", "htmlcov"}


def _walk_active_py():
    for p in REPO.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.relative_to(REPO).parts):
            continue
        yield p


def _imports(path: Path) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return []
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((a.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append((node.module, node.lineno))
    return out


def test_no_active_code_imports_legacy():
    """No file under the active tree may `from legacy.* import ...`
    or `import legacy.*`. KI-116 / Session 0 hold-the-line."""
    offenders: list[tuple[str, int, str]] = []
    for py in _walk_active_py():
        for mod, lineno in _imports(py):
            top = mod.split(".", 1)[0]
            if top == "legacy":
                offenders.append((str(py.relative_to(REPO)), lineno, mod))
    assert not offenders, (
        "Active code imports from legacy/ — Session 0 hold-the-line broken. "
        f"{offenders}"
    )


def test_legacy_dir_present():
    """legacy/ must exist (Session 0 baseline). If it goes missing
    something is very wrong — possibly a runaway delete."""
    legacy = REPO / "legacy"
    assert legacy.is_dir(), "legacy/ directory missing — was it deleted accidentally?"
    py_count = sum(1 for _ in legacy.rglob("*.py"))
    assert py_count > 50, (
        f"legacy/ should have ~99 .py files (Session 0 baseline). "
        f"Found {py_count}. Did something get deleted?"
    )


def test_legacy_readme_explains_contents():
    readme = REPO / "legacy" / "README.md"
    assert readme.exists(), "legacy/README.md missing"
    text = readme.read_text()
    # Sanity: a few key terms describing what's in there.
    assert "dormant" in text.lower(), "legacy/README.md should describe legacy/ as dormant"
    assert "git mv" in text.lower() or "preserve" in text.lower(), (
        "legacy/README.md should explain history preservation"
    )
