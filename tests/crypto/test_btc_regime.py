"""Unit tests for monitoring.btc_regime — BTC market regime classifier."""
from __future__ import annotations

import duckdb

from crypto.schema import create_all_tables


def test_crypto_regime_daily_table_is_created():
    conn = duckdb.connect(":memory:")
    create_all_tables(conn)
    cols = conn.execute(
        "SELECT column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_name = 'crypto_regime_daily' "
        "ORDER BY ordinal_position"
    ).fetchall()
    assert cols == [
        ("trade_date", "DATE"),
        ("regime", "VARCHAR"),
        ("confidence", "DOUBLE"),
        ("indicators_json", "VARCHAR"),
        ("computed_at", "TIMESTAMP"),
    ]
    pk_cols = conn.execute(
        "SELECT constraint_column_names "
        "FROM duckdb_constraints() "
        "WHERE table_name = 'crypto_regime_daily' "
        "AND constraint_type = 'PRIMARY KEY'"
    ).fetchone()
    assert pk_cols == (["trade_date"],)
