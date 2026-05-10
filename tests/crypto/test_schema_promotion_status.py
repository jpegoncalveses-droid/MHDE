"""Tests for the promotion_status column on crypto_ml_model_runs (gap1)."""
from __future__ import annotations

import duckdb
import pytest

from storage.migrations import run_migrations
from crypto.schema import create_all_tables


def test_promotion_status_exists_in_fresh_db(temp_db):
    """Fresh DB: column exists, type is VARCHAR, default is 'pending'."""
    cols = {
        row[0]: row
        for row in temp_db.execute(
            "SELECT column_name, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'crypto_ml_model_runs'"
        ).fetchall()
    }
    assert "promotion_status" in cols, "promotion_status column missing from fresh DB"
    col = cols["promotion_status"]
    assert col[1].upper() == "VARCHAR", f"Expected VARCHAR, got {col[1]}"
    assert col[2] is not None and "'pending'" in col[2], (
        f"Expected default 'pending', got {col[2]}"
    )


def test_promotion_status_default_on_insert(temp_db):
    """Newly inserted rows get promotion_status = 'pending' automatically."""
    temp_db.execute(
        "INSERT INTO crypto_ml_model_runs "
        "(model_id, horizon, target_threshold, is_active) "
        "VALUES ('model-new-1', '24h', 0.05, false)"
    )
    row = temp_db.execute(
        "SELECT promotion_status FROM crypto_ml_model_runs WHERE model_id = 'model-new-1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "pending", f"Expected 'pending', got {row[0]}"


def test_migration_backfills_pre_existing_db():
    """Migration v9 applied to a DB without the column backfills correctly.

    - is_active = true  → promotion_status = 'promoted'
    - is_active = false → promotion_status = 'pending'
    """
    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    conn.execute(
        "CREATE TABLE IF NOT EXISTS crypto_ml_model_runs ("
        "    model_id VARCHAR PRIMARY KEY,"
        "    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        "    horizon VARCHAR NOT NULL,"
        "    target_threshold DOUBLE NOT NULL,"
        "    train_start DATE,"
        "    train_end DATE,"
        "    test_start DATE,"
        "    test_end DATE,"
        "    n_train_samples INTEGER,"
        "    n_positive_train INTEGER,"
        "    n_positive_test INTEGER,"
        "    n_test_samples INTEGER,"
        "    precision_at_threshold DOUBLE,"
        "    recall_at_threshold DOUBLE,"
        "    f1_score DOUBLE,"
        "    auc_roc DOUBLE,"
        "    base_rate DOUBLE,"
        "    lift_over_base DOUBLE,"
        "    feature_importance_json TEXT,"
        "    model_path VARCHAR,"
        "    is_active BOOLEAN DEFAULT FALSE"
        ")"
    )

    conn.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, is_active) "
        "VALUES ('model-active', '24h', 0.05, true)"
    )
    conn.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, is_active) "
        "VALUES ('model-inactive', '24h', 0.05, false)"
    )

    conn.execute(
        "DELETE FROM schema_version WHERE version = 9"
    )

    from storage.migrations import run_migrations as _run
    _run(conn)

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT model_id, promotion_status FROM crypto_ml_model_runs"
        ).fetchall()
    }
    assert rows["model-active"] == "promoted", (
        f"Expected 'promoted' for is_active=true row, got {rows['model-active']}"
    )
    assert rows["model-inactive"] == "pending", (
        f"Expected 'pending' for is_active=false row, got {rows['model-inactive']}"
    )
    conn.close()


def test_migration_v9_idempotent():
    """Running migration v9 twice on a DB that already has the column is safe.

    When promotion_status already exists, re-running v9 must not error and must
    not duplicate the column.  The backfill UPDATEs are skipped (column already
    present), so existing row values are left untouched — that is correct
    idempotent behaviour.
    """
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    create_all_tables(conn)

    conn.execute(
        "INSERT INTO crypto_ml_model_runs (model_id, horizon, target_threshold, is_active) "
        "VALUES ('model-idem', '24h', 0.05, true)"
    )

    conn.execute("DELETE FROM schema_version WHERE version = 9")
    from storage.migrations import run_migrations as _run
    _run(conn)

    col_count = conn.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name = 'crypto_ml_model_runs' AND column_name = 'promotion_status'"
    ).fetchone()[0]
    assert col_count == 1, "Column should appear exactly once after idempotent re-run"

    row = conn.execute(
        "SELECT promotion_status FROM crypto_ml_model_runs WHERE model_id = 'model-idem'"
    ).fetchone()
    assert row is not None, "Row should still exist after idempotent re-run"
    assert row[0] in ("pending", "promoted"), f"Unexpected promotion_status value: {row[0]}"
    conn.close()
