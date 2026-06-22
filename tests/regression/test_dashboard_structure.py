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


def _freshness_call_guarded(
    file_path: Path, call_name: str
) -> tuple[bool, bool, bool]:
    """Inspect `file_path` for a `try` whose body contains a call to
    `call_name(...)`. Returns ``(guarded, has_handler, degrades)``:

      guarded      — the call appears inside some `try` block's body
      has_handler  — that `try` has at least one `except` handler
      degrades     — at least one handler does something other than only
                     re-`raise` (i.e. it logs / renders a notice instead of
                     letting the exception propagate)

    AST-based (the dashboard app cannot be imported under test — it opens
    mhde.duckdb and renders Streamlit at module import).
    """
    tree = ast.parse(file_path.read_text())

    def _body_has_call(try_node: ast.Try) -> bool:
        for stmt in try_node.body:
            for n in ast.walk(stmt):
                if isinstance(n, ast.Call):
                    func = n.func
                    name = (
                        func.id
                        if isinstance(func, ast.Name)
                        else getattr(func, "attr", None)
                    )
                    if name == call_name:
                        return True
        return False

    def _handler_degrades(try_node: ast.Try) -> bool:
        for h in try_node.handlers:
            stmts = [s for s in h.body if not isinstance(s, ast.Pass)]
            if stmts and not all(isinstance(s, ast.Raise) for s in stmts):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and _body_has_call(node):
            return True, bool(node.handlers), _handler_degrades(node)
    return False, False, False


def test_freshness_banner_cannot_crash_dashboard():
    """The System Health data-freshness banner is non-critical observability
    and must never crash the whole dashboard.

    The module-level `check_all` call (imported as `_check_all_freshness`) in
    dashboard/app.py runs at import, before any tab renders. If a freshness
    check raises — e.g. `_duckdb.CatalogException` from a missing/renamed
    table such as `fx_prices_hourly` — the unguarded call kills every tab.
    This regression requires the call to be wrapped in a try/except that
    DEGRADES (logs + renders a notice) instead of propagating.
    """
    app_py = REPO / "dashboard" / "app.py"
    calls = _module_level_calls_to(app_py, "_check_all_freshness")
    assert calls, (
        "expected a module-level _check_all_freshness(conn) call in "
        "dashboard/app.py"
    )
    guarded, has_handler, degrades = _freshness_call_guarded(
        app_py, "_check_all_freshness"
    )
    assert guarded and has_handler, (
        "dashboard/app.py module-level _check_all_freshness(conn) must be "
        "wrapped in a try/except so a failing freshness check (e.g. missing "
        "table -> CatalogException) cannot crash the page."
    )
    assert degrades, (
        "the freshness-banner except handler must DEGRADE (log + render a "
        "visible notice), not merely re-raise."
    )


def _with_body_has_guarding_try(
    file_path: Path, ctx_name: str
) -> tuple[bool, bool, bool]:
    """Find a ``with <ctx_name>:`` block and report whether its body is wrapped
    in a degrading try/except. Returns ``(found, guarded, degrades)``:

      found    — a ``with <ctx_name>:`` block exists
      guarded  — its body contains a direct-child ``try`` with >=1 except handler
      degrades — at least one handler does something other than only re-``raise``

    AST-based: the dashboard app cannot be imported under test (it opens
    mhde.duckdb and renders Streamlit at module import). st.tabs() executes
    every tab body in one pass, so an unguarded tab crashes the whole page.
    """
    tree = ast.parse(file_path.read_text())

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Name) and ctx.id == ctx_name:
                    target = node
                    break
        if target is not None:
            break
    if target is None:
        return False, False, False

    for stmt in target.body:
        if isinstance(stmt, ast.Try) and stmt.handlers:
            degrades = False
            for h in stmt.handlers:
                body = [s for s in h.body if not isinstance(s, ast.Pass)]
                if body and not all(isinstance(s, ast.Raise) for s in body):
                    degrades = True
            return True, True, degrades
    return True, False, False


def test_equity_and_crypto_tabs_degrade_on_missing_tables():
    """st.tabs() runs every tab body in ONE render pass, so an unguarded tab
    that queries an absent table crashes the WHOLE page — even with the
    freshness banner guarded. The Equity/ML tab queries the dormant `ml_*`
    tables (absent) and the Crypto tab queries `crypto_*` tables (present only
    by luck). Each tab body must wrap its query path in a try/except that
    DEGRADES to a visible notice instead of propagating a CatalogException.
    """
    app_py = REPO / "dashboard" / "app.py"
    for ctx in ("tab_equities", "tab_crypto"):
        found, guarded, degrades = _with_body_has_guarding_try(app_py, ctx)
        assert found, f"expected a `with {ctx}:` block in dashboard/app.py"
        assert guarded, (
            f"the `with {ctx}:` tab body must wrap its queries in a try/except "
            f"so a missing/absent table (CatalogException) degrades instead of "
            f"crashing the whole page (st.tabs runs every tab body in one pass)."
        )
        assert degrades, (
            f"the `with {ctx}:` except handler must DEGRADE (log + render a "
            f"notice), not merely re-raise."
        )
