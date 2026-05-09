"""Refresh GBP/EUR hourly bars into the production fx_prices_hourly table.

Post-cutover (Session 2 of the FX migration, 2026-05-08): this is now
backed by TwelveData via :mod:`fx.data.refresh_twelvedata`. The Dukascopy
ATSRP-subprocess implementation that previously lived here was retired
once the 30-day comparison gate passed (DECISIONS.md ADR-013).

Public surface is unchanged — ``main.py`` still imports ``refresh_prices``
from this module and the systemd ExecStart line still calls
``main.py fx refresh-prices``. Only the data source flipped.
"""
from __future__ import annotations

import logging
from typing import Any

import duckdb

from fx.data.refresh_twelvedata import refresh_prices as _twelvedata_refresh

logger = logging.getLogger("mhde.fx.refresh")


def refresh_prices(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Fetch the latest GBP/EUR 1h bar from TwelveData and upsert into
    fx_prices_hourly. Return shape mirrors the pre-cutover Dukascopy
    implementation so log readers and callers don't need updating."""
    return _twelvedata_refresh(conn, table="fx_prices_hourly")
