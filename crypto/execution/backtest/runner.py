"""Phase 1B grid runner — drive the harness across the spec's full
configuration matrix.

Per ``crypto/execution/backtest/SPEC.md`` § "Policies to Test (Phase 1A
Base Grid)":

    Horizons:        ["5d", "10d"]      (20d deferred)
    Exit policies:   ["A", "B", "C", "D", "E"]
    Selection rules: ["top_n", "threshold"]

    20 base configurations total. Each is a deterministic ``run_id``
    (see ``harness.make_run_id``) so re-runs are idempotent.

Public API
----------

* :class:`GridConfig`   — one row of (horizon × policy × selection × params).
* :class:`GridRunResult` — outcome of running one config.
* :class:`GridResult`   — collection across the whole grid.
* :func:`base_grid_configs() -> list[GridConfig]` — the spec's 20-row grid.
* :func:`run_grid(conn, configs, *, force, skip_existing, dry_run)
  -> GridResult` — orchestrator.
* :func:`summarize_grid_result(result) -> str` — pretty CLI summary.

Failure-handling contract
-------------------------

run_grid is **defensive**: a failure on one config never halts the grid.
Every config produces exactly one :class:`GridRunResult`, with one of:

    "completed"          — run + summary persisted (or lifecycle ran in
                           dry-run mode with no DB writes).
    "skipped_existing"   — run_id already in crypto_backtest_runs and
                           skip_existing=True (default).
    "failed_collision"   — run_id already exists and skip_existing=False
                           without --force; collision raised by harness.
    "failed_runtime"     — any other exception inside run_backtest or
                           compute_and_persist_summary.

This module imports nothing from equity / FX / shared ``ml/``; it only
composes ``harness`` + ``metrics`` from the same ``crypto/execution/backtest``
package.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import duckdb

from crypto.execution.backtest.harness import (
    make_run_id,
    run_backtest,
)
from crypto.execution.backtest.metrics import (
    SummaryRow,
    compute_and_persist_summary,
)

logger = logging.getLogger("mhde.crypto.backtest.runner")


# ──────────────────────────────────────────────────────────────────────
# Defaults — match SPEC.md's base-grid description
# ──────────────────────────────────────────────────────────────────────


DEFAULT_HORIZONS: tuple[str, ...] = ("5d", "10d")
DEFAULT_POLICIES: tuple[str, ...] = ("A", "B", "C", "D", "E")
DEFAULT_SELECTION_RULES: tuple[str, ...] = ("top_n", "threshold")
DEFAULT_TOP_N: int = 6
DEFAULT_THRESHOLD: float = 0.55


# ──────────────────────────────────────────────────────────────────────
# Config + result types
# ──────────────────────────────────────────────────────────────────────


@dataclass
class GridConfig:
    """One row of the configuration matrix.

    ``selection_params`` and ``policy_params`` are passed through to the
    respective modules (see ``selection.py`` / ``policies.py`` for keys).
    The :attr:`run_id` is derived deterministically — same config →
    same id, idempotent re-runs.
    """

    horizon: str
    policy: str
    selection: str
    selection_params: dict[str, Any] = field(default_factory=dict)
    policy_params: dict[str, Any] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return make_run_id(
            horizon=self.horizon,
            exit_policy_id=self.policy,
            selection_rule=self.selection,
            selection_params=self.selection_params,
            policy_params=self.policy_params,
        )


@dataclass
class GridRunResult:
    """Outcome of running one :class:`GridConfig`."""

    config: GridConfig
    run_id: str
    status: str           # see module docstring for the enum
    summary: Optional[SummaryRow]
    error: Optional[str]
    elapsed_seconds: float


@dataclass
class GridResult:
    """Aggregate across the whole grid."""

    results: list[GridRunResult]

    @property
    def n_completed(self) -> int:
        return sum(1 for r in self.results if r.status == "completed")

    @property
    def n_skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped_existing")

    @property
    def n_failed(self) -> int:
        return sum(1 for r in self.results if r.status.startswith("failed"))


# ──────────────────────────────────────────────────────────────────────
# Base grid
# ──────────────────────────────────────────────────────────────────────


def base_grid_configs(
    *,
    horizons: tuple[str, ...] = DEFAULT_HORIZONS,
    policies: tuple[str, ...] = DEFAULT_POLICIES,
    selection_rules: tuple[str, ...] = DEFAULT_SELECTION_RULES,
    top_n: int = DEFAULT_TOP_N,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[GridConfig]:
    """Build the spec's 2 × 5 × 2 = 20 base configurations.

    Defaults match SPEC.md "Selection rules" (Top N=6, threshold p ≥ 0.55)
    and "Policies to Test" (policy params left at the policy class
    defaults — `tp_pct=0.05`, `sl_pct=0.03`, `atr_mult=2.0`,
    `trail_pct=0.50`, etc.). Phase 1B sensitivity grid is a separate
    factory (TODO: ``sensitivity_grid_configs``).
    """
    configs: list[GridConfig] = []
    for horizon in horizons:
        for policy in policies:
            for selection in selection_rules:
                if selection == "top_n":
                    sel_params: dict[str, Any] = {"n": top_n}
                elif selection == "threshold":
                    sel_params = {"threshold": threshold}
                else:
                    raise ValueError(
                        f"unknown selection_rule {selection!r}"
                    )
                configs.append(
                    GridConfig(
                        horizon=horizon, policy=policy,
                        selection=selection,
                        selection_params=sel_params,
                        policy_params={},
                    )
                )
    return configs


# ──────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────


def _run_id_already_persisted(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> bool:
    return conn.execute(
        "SELECT 1 FROM crypto_backtest_runs WHERE run_id = ?", [run_id],
    ).fetchone() is not None


def _run_one_config(
    conn: duckdb.DuckDBPyConnection,
    cfg: GridConfig,
    *,
    force: bool,
    skip_existing: bool,
    dry_run: bool,
) -> GridRunResult:
    """Run one config end-to-end, capturing any exception into a status."""
    run_id = cfg.run_id

    # In dry-run mode we never write to the DB, so existence check + skip
    # logic does not apply (the harness itself doesn't persist).
    if not dry_run and _run_id_already_persisted(conn, run_id):
        if skip_existing and not force:
            return GridRunResult(
                config=cfg, run_id=run_id, status="skipped_existing",
                summary=None, error=None, elapsed_seconds=0.0,
            )
        # else: caller wants either failure-on-collision or force-overwrite;
        # both are handled inside run_backtest.

    try:
        run_backtest(
            conn,
            horizon=cfg.horizon,
            exit_policy_id=cfg.policy,
            selection_rule=cfg.selection,
            selection_params=cfg.selection_params,
            policy_params=cfg.policy_params,
            dry_run=dry_run,
            force=force,
        )
    except RuntimeError as exc:
        if "already exists" in str(exc):
            return GridRunResult(
                config=cfg, run_id=run_id, status="failed_collision",
                summary=None, error=str(exc), elapsed_seconds=0.0,
            )
        return GridRunResult(
            config=cfg, run_id=run_id, status="failed_runtime",
            summary=None, error=f"{type(exc).__name__}: {exc}",
            elapsed_seconds=0.0,
        )
    except Exception as exc:
        return GridRunResult(
            config=cfg, run_id=run_id, status="failed_runtime",
            summary=None, error=f"{type(exc).__name__}: {exc}",
            elapsed_seconds=0.0,
        )

    summary: Optional[SummaryRow] = None
    if not dry_run:
        try:
            summary = compute_and_persist_summary(conn, run_id)
        except Exception as exc:
            return GridRunResult(
                config=cfg, run_id=run_id, status="failed_runtime",
                summary=None,
                error=f"summary failed: {type(exc).__name__}: {exc}",
                elapsed_seconds=0.0,
            )

    return GridRunResult(
        config=cfg, run_id=run_id, status="completed",
        summary=summary, error=None, elapsed_seconds=0.0,
    )


def run_grid(
    conn: duckdb.DuckDBPyConnection,
    configs: list[GridConfig],
    *,
    force: bool = False,
    skip_existing: bool = True,
    dry_run: bool = False,
) -> GridResult:
    """Run a list of :class:`GridConfig`s sequentially.

    Args:
        conn: writable DuckDB connection.
        configs: ordered list of configurations to attempt. ``run_id`` is
            derived deterministically per-config so the same set of
            configs always produces the same set of run_ids.
        force: forward to ``run_backtest`` and overwrite any existing
            row for a given ``run_id``. Default ``False``.
        skip_existing: when ``True`` (default), configs whose ``run_id``
            already exists are silently skipped. Set ``False`` to make
            collisions a per-config failure (status ``failed_collision``)
            instead.
        dry_run: forward to ``run_backtest``; lifecycle runs but neither
            ``crypto_backtest_runs`` / ``crypto_backtest_trades`` nor
            ``crypto_backtest_summary`` is written.

    Returns:
        :class:`GridResult` with one :class:`GridRunResult` per input
        config, in the same order. The grid never halts on a single
        failure — every config is attempted.
    """
    results: list[GridRunResult] = []
    n_total = len(configs)
    grid_t0 = time.perf_counter()
    logger.info(
        "run_grid start: %d configs (force=%s, skip_existing=%s, dry_run=%s)",
        n_total, force, skip_existing, dry_run,
    )

    for i, cfg in enumerate(configs, start=1):
        t0 = time.perf_counter()
        result = _run_one_config(
            conn, cfg,
            force=force, skip_existing=skip_existing, dry_run=dry_run,
        )
        elapsed = time.perf_counter() - t0
        result = replace(result, elapsed_seconds=elapsed)
        results.append(result)
        logger.info(
            "[%d/%d] %s status=%s elapsed=%.2fs%s",
            i, n_total, result.run_id, result.status, elapsed,
            f"  error={result.error}" if result.error else "",
        )

    grid_elapsed = time.perf_counter() - grid_t0
    grid = GridResult(results=results)
    logger.info(
        "run_grid done : %d completed, %d skipped, %d failed; total %.1fs",
        grid.n_completed, grid.n_skipped, grid.n_failed, grid_elapsed,
    )
    return grid


# ──────────────────────────────────────────────────────────────────────
# Pretty-print summary
# ──────────────────────────────────────────────────────────────────────


def summarize_grid_result(result: GridResult) -> str:
    """Human-readable summary of a :func:`run_grid` invocation."""
    lines = [
        "=" * 78,
        "  Phase 1B grid results",
        "=" * 78,
        f"  total runs       : {len(result.results)}",
        f"  completed        : {result.n_completed}",
        f"  skipped existing : {result.n_skipped}",
        f"  failed           : {result.n_failed}",
    ]

    failures = [r for r in result.results if r.status.startswith("failed")]
    if failures:
        lines.append("\n  Failures:")
        for f in failures:
            lines.append(f"    {f.run_id}  ({f.status})")
            lines.append(f"        {f.error}")

    lines.append("\n  Per-run table:")
    lines.append(
        f"  {'#':>3}  {'run_id':<48}  {'status':<22}  {'sec':>6}"
    )
    lines.append("  " + "-" * 84)
    for i, r in enumerate(result.results, start=1):
        lines.append(
            f"  {i:>3}  {r.run_id:<48}  {r.status:<22}  "
            f"{r.elapsed_seconds:>6.1f}"
        )
    return "\n".join(lines)
