"""ML table schema definitions for DuckDB."""

SCHEMA_ML_LABELS = """
CREATE TABLE IF NOT EXISTS ml_labels (
    ticker VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    close_price DOUBLE,
    fwd_return_5d DOUBLE,
    fwd_return_10d DOUBLE,
    fwd_return_20d DOUBLE,
    fwd_max_return_5d DOUBLE,
    fwd_max_return_10d DOUBLE,
    fwd_max_return_20d DOUBLE,
    fwd_max_drawdown_5d DOUBLE,
    fwd_max_drawdown_10d DOUBLE,
    fwd_max_drawdown_20d DOUBLE,
    label_5d_3pct BOOLEAN,
    label_5d_5pct BOOLEAN,
    label_10d_5pct BOOLEAN,
    label_10d_8pct BOOLEAN,
    label_20d_5pct BOOLEAN,
    label_20d_8pct BOOLEAN,
    label_20d_10pct BOOLEAN,
    label_20d_15pct BOOLEAN,
    PRIMARY KEY (ticker, trade_date)
);
"""

SCHEMA_ML_FEATURES = """
CREATE TABLE IF NOT EXISTS ml_features (
    ticker VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    return_5d DOUBLE,
    return_10d DOUBLE,
    return_20d DOUBLE,
    return_60d DOUBLE,
    rsi_14d DOUBLE,
    drawdown_from_52w_high DOUBLE,
    price_vs_50d_ma DOUBLE,
    price_vs_200d_ma DOUBLE,
    bollinger_position DOUBLE,
    close_in_range DOUBLE,
    gap_from_prev_close DOUBLE,
    realized_vol_20d DOUBLE,
    realized_vol_60d DOUBLE,
    vol_ratio DOUBLE,
    atr_pct_20d DOUBLE,
    relative_volume_20d DOUBLE,
    volume_trend_5d DOUBLE,
    return_vs_spy_5d DOUBLE,
    return_vs_spy_20d DOUBLE,
    return_vs_sector_5d DOUBLE,
    return_vs_sector_20d DOUBLE,
    beta_60d DOUBLE,
    vix_level DOUBLE,
    vix_change_5d DOUBLE,
    yield_curve_10y_2y DOUBLE,
    filing_8k_count_7d INTEGER,
    filing_8k_count_30d INTEGER,
    filing_form4_count_7d INTEGER,
    filing_form4_count_14d INTEGER,
    days_since_last_10q DOUBLE,
    market_cap_log DOUBLE,
    pb_ratio DOUBLE,
    PRIMARY KEY (ticker, trade_date)
);
"""

SCHEMA_ML_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS ml_predictions (
    ticker VARCHAR NOT NULL,
    prediction_date DATE NOT NULL,
    model_id VARCHAR NOT NULL,
    horizon VARCHAR NOT NULL,
    predicted_probability DOUBLE,
    prediction_threshold DOUBLE,
    sector VARCHAR,
    market_cap_bucket VARCHAR,
    actual_max_return DOUBLE,
    actual_max_drawdown DOUBLE,
    actual_hit BOOLEAN,
    outcome_filled_at TIMESTAMP,
    PRIMARY KEY (ticker, prediction_date, model_id, horizon)
);
"""

SCHEMA_ML_MODEL_RUNS = """
CREATE TABLE IF NOT EXISTS ml_model_runs (
    model_id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    horizon VARCHAR NOT NULL,
    target_threshold DOUBLE NOT NULL,
    train_start DATE,
    train_end DATE,
    test_start DATE,
    test_end DATE,
    n_train_samples INTEGER,
    n_test_samples INTEGER,
    n_positive_train INTEGER,
    n_positive_test INTEGER,
    precision_at_threshold DOUBLE,
    recall_at_threshold DOUBLE,
    f1_score DOUBLE,
    auc_roc DOUBLE,
    base_rate DOUBLE,
    lift_over_base DOUBLE,
    feature_importance_json TEXT,
    model_path VARCHAR,
    is_active BOOLEAN DEFAULT FALSE
);
"""

ALL_SCHEMAS = [
    SCHEMA_ML_LABELS,
    SCHEMA_ML_FEATURES,
    SCHEMA_ML_PREDICTIONS,
    SCHEMA_ML_MODEL_RUNS,
]


def create_all_tables(conn):
    for schema in ALL_SCHEMAS:
        conn.execute(schema)
