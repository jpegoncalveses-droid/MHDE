"""Regression tests for dashboard structure invariants.

KI-105: no module-level cached connection (causes stale reads after
        the underlying file rotates).
KI-113: outcome rendering must work for all 3 engines (parity check
        on prediction-table columns the dashboard reads).
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _module_level_calls_to(file_path: Path, target_name: str) -> list[int]:
    """Return line numbers in `file_path` where `target_name(...)` is
    called at the module-level (not inside a function, method, or class).

    Walks only statements directly in the Module body. For each, recurses
    into expressions but stops at function/class boundaries.
    """
    src = file_path.read_text()
    tree = ast.parse(src)
    hits: list[int] = []

    def _check(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return  # don't descend
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == target_name:
                hits.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            _check(child)

    for stmt in tree.body:
        _check(stmt)
    return hits


def test_no_module_level_connection():
    """KI-105: no `duckdb.connect(...)` (or `get_connection(...)`) at
    module-level in any file under dashboard/. The pattern must be
    per-page-render acquisition.
    """
    dashboard_dir = REPO / "dashboard"
    offenders: list[tuple[Path, list[int]]] = []
    for py in dashboard_dir.rglob("*.py"):
        if "_legacy" in py.parts:
            continue
        for target in ("connect", "get_connection"):
            lines = _module_level_calls_to(py, target)
            if lines:
                offenders.append((py.relative_to(REPO), lines))
    assert not offenders, (
        f"Dashboard files have module-level DB connection calls. KI-105. "
        f"{offenders}"
    )


def test_outcome_columns_in_predictions():
    """KI-113: every engine's predictions table must expose the columns
    the dashboard outcome view reads.

    The shared dashboard "outcome" rendering only works if all three
    prediction tables expose the same outcome surface.
    """
    expected = {"actual_hit", "outcome_filled_at"}

    # Check by parsing each schema source for column names.
    schema_sources = [
        REPO / "ml" / "schema.py",
        REPO / "crypto" / "schema.py",
        REPO / "fx" / "schema.py",
    ]
    missing: dict[str, set[str]] = {}
    for src in schema_sources:
        text = src.read_text()
        for col in expected:
            if col not in text:
                missing.setdefault(src.name, set()).add(col)
    assert not missing, (
        f"Schema sources missing shared outcome columns. KI-113. {missing}"
    )


def test_dashboard_queries_module_imports_cleanly():
    """The dashboard data-layer must be importable without booting
    Streamlit. assert_dashboard_renders depends on this."""
    import importlib
    mod = importlib.import_module("dashboard.services.queries")
    # Smoke: every documented page query exists.
    for fn in ("get_overview_stats", "get_candidates", "get_outcomes",
               "get_health_checks", "get_alerts", "get_hypotheses"):
        assert hasattr(mod, fn), f"dashboard.services.queries.{fn} missing"
