"""Crypto ML table schema definitions for DuckDB."""

SCHEMA_CRYPTO_PRICES_DAILY = """
CREATE TABLE IF NOT EXISTS crypto_prices_daily (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    trades INTEGER,
    taker_buy_volume DOUBLE,
    source VARCHAR DEFAULT 'binance',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date)
);
"""

SCHEMA_CRYPTO_FUNDING_RATES = """
CREATE TABLE IF NOT EXISTS crypto_funding_rates (
    symbol VARCHAR NOT NULL,
    funding_time TIMESTAMP NOT NULL,
    funding_rate DOUBLE,
    mark_price DOUBLE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, funding_time)
);
"""

SCHEMA_CRYPTO_OPEN_INTEREST = """
CREATE TABLE IF NOT EXISTS crypto_open_interest (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open_interest DOUBLE,
    open_interest_value DOUBLE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date)
);
"""

SCHEMA_CRYPTO_UNIVERSE = """
CREATE TABLE IF NOT EXISTS crypto_universe (
    symbol VARCHAR NOT NULL,
    base_asset VARCHAR NOT NULL,
    avg_daily_volume_30d DOUBLE,
    rank_by_volume INTEGER,
    is_active BOOLEAN DEFAULT TRUE,
    added_date DATE,
    removed_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol)
);
"""

SCHEMA_CRYPTO_ML_LABELS = """
CREATE TABLE IF NOT EXISTS crypto_ml_labels (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    close_price DOUBLE,
    fwd_return_1d DOUBLE,
    fwd_return_3d DOUBLE,
    fwd_return_5d DOUBLE,
    fwd_return_10d DOUBLE,
    fwd_max_return_1d DOUBLE,
    fwd_max_return_3d DOUBLE,
    fwd_max_return_5d DOUBLE,
    fwd_max_return_10d DOUBLE,
    fwd_max_drawdown_1d DOUBLE,
    fwd_max_drawdown_3d DOUBLE,
    fwd_max_drawdown_5d DOUBLE,
    fwd_max_drawdown_10d DOUBLE,
    label_1d_5pct BOOLEAN,
    label_1d_3pct BOOLEAN,
    label_3d_5pct BOOLEAN,
    label_3d_10pct BOOLEAN,
    label_5d_10pct BOOLEAN,
    label_5d_15pct BOOLEAN,
    label_10d_10pct BOOLEAN,
    label_10d_15pct BOOLEAN,
    label_10d_20pct BOOLEAN,
    -- Knockout (triple-barrier) label — see crypto/ml/knockout_label.py and
    -- crypto/ml/KNOCKOUT_LABEL_SPEC.md. label_Nd_knockout = (knockout_outcome_Nd == 'tp');
    -- knockout_outcome_Nd ∈ {'tp','sl','neither'}; knockout_resolve_day_Nd is the
    -- 1-indexed forward bar a barrier was touched (NULL for 'neither').
    label_5d_knockout BOOLEAN,
    label_10d_knockout BOOLEAN,
    knockout_outcome_5d VARCHAR,
    knockout_outcome_10d VARCHAR,
    knockout_resolve_day_5d INTEGER,
    knockout_resolve_day_10d INTEGER,
    PRIMARY KEY (symbol, trade_date)
);
"""

# Idempotent migrations for crypto_ml_labels — applied after the CREATE so an
# existing live table (predating the knockout columns) gains them without
# losing data; all no-ops on a freshly-created table.
_CRYPTO_ML_LABELS_MIGRATIONS = [
    "ALTER TABLE crypto_ml_labels ADD COLUMN IF NOT EXISTS label_5d_knockout BOOLEAN",
    "ALTER TABLE crypto_ml_labels ADD COLUMN IF NOT EXISTS label_10d_knockout BOOLEAN",
    "ALTER TABLE crypto_ml_labels ADD COLUMN IF NOT EXISTS knockout_outcome_5d VARCHAR",
    "ALTER TABLE crypto_ml_labels ADD COLUMN IF NOT EXISTS knockout_outcome_10d VARCHAR",
    "ALTER TABLE crypto_ml_labels ADD COLUMN IF NOT EXISTS knockout_resolve_day_5d INTEGER",
    "ALTER TABLE crypto_ml_labels ADD COLUMN IF NOT EXISTS knockout_resolve_day_10d INTEGER",
]

SCHEMA_CRYPTO_ML_FEATURES = """
CREATE TABLE IF NOT EXISTS crypto_ml_features (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    return_1d DOUBLE,
    return_3d DOUBLE,
    return_5d DOUBLE,
    return_10d DOUBLE,
    return_20d DOUBLE,
    return_60d DOUBLE,
    rsi_14d DOUBLE,
    drawdown_from_90d_high DOUBLE,
    price_vs_20d_ma DOUBLE,
    price_vs_50d_ma DOUBLE,
    bollinger_position DOUBLE,
    close_in_range DOUBLE,
    realized_vol_10d DOUBLE,
    realized_vol_30d DOUBLE,
    vol_ratio DOUBLE,
    atr_pct_14d DOUBLE,
    relative_volume_20d DOUBLE,
    volume_trend_5d DOUBLE,
    taker_buy_ratio DOUBLE,
    return_vs_btc_1d DOUBLE,
    return_vs_btc_5d DOUBLE,
    return_vs_btc_10d DOUBLE,
    beta_to_btc_30d DOUBLE,
    funding_rate_current DOUBLE,
    funding_rate_avg_3d DOUBLE,
    funding_rate_avg_7d DOUBLE,
    funding_rate_zscore DOUBLE,
    oi_change_1d DOUBLE,
    oi_change_3d DOUBLE,
    oi_change_7d DOUBLE,
    oi_price_divergence_3d DOUBLE,
    btc_dominance DOUBLE,
    btc_return_7d DOUBLE,
    btc_vol_30d DOUBLE,
    market_cap_log DOUBLE,
    PRIMARY KEY (symbol, trade_date)
);
"""

SCHEMA_CRYPTO_ML_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS crypto_ml_predictions (
    symbol VARCHAR NOT NULL,
    prediction_date DATE NOT NULL,
    model_id VARCHAR NOT NULL,
    horizon VARCHAR NOT NULL,
    predicted_probability DOUBLE,
    prediction_threshold DOUBLE,
    market_cap_bucket VARCHAR,
    actual_max_return DOUBLE,
    actual_max_drawdown DOUBLE,
    actual_hit BOOLEAN,
    outcome_filled_at TIMESTAMP,
    predicted_at TIMESTAMP,
    PRIMARY KEY (symbol, prediction_date, model_id, horizon)
);
"""

SCHEMA_CRYPTO_ML_MODEL_RUNS = """
CREATE TABLE IF NOT EXISTS crypto_ml_model_runs (
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
    is_active BOOLEAN DEFAULT FALSE,
    -- Valid values: 'pending' | 'promoted' | 'promotion_blocked'
    -- Set by the validation gate (crypto/ml/validation_gate.py) after each training run.
    promotion_status VARCHAR DEFAULT 'pending',
    -- 'legacy'  → trained on label_Nd_10pct (close-based +10% tag)
    -- 'knockout' → trained on label_Nd_knockout (triple-barrier TP/SL). Knockout
    -- models are inserted with is_active=false and are NOT auto-gated/promoted —
    -- promotion is an explicit operator decision. See ADR-023 / ADR-024.
    label_kind VARCHAR DEFAULT 'legacy'
);
"""

# Idempotent migration for crypto_ml_model_runs — backfills the label_kind
# column onto an existing table (existing rows default to 'legacy').
_CRYPTO_ML_MODEL_RUNS_MIGRATIONS = [
    "ALTER TABLE crypto_ml_model_runs ADD COLUMN IF NOT EXISTS label_kind VARCHAR DEFAULT 'legacy'",
]

# Exceptions log written by the OHLCV plausibility guard
# (pipelines/data_quality_guard.py). One row per flagged (date, symbol,
# check_name); a re-run for the same date UPSERTs. check_name is one of
# 'volume_cliff' / 'range_collapse' / 'trade_count_cliff' (severity 'warn',
# symbol = the coin), or 'systemic_corruption' (severity 'critical',
# symbol = '__systemic__', expected = SYSTEMIC_FLAG_RATIO, observed = the
# flagged-fraction of the evaluable universe). Clean days write nothing.
SCHEMA_CRYPTO_DATA_QUALITY_REPORTS = """
CREATE TABLE IF NOT EXISTS crypto_data_quality_reports (
    date DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    check_name VARCHAR NOT NULL,
    expected DOUBLE,
    observed DOUBLE,
    flagged BOOLEAN,
    severity VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, symbol, check_name)
);
"""

SCHEMA_PHASE0_MILESTONES = """
CREATE TABLE IF NOT EXISTS phase0_milestones (
    engine VARCHAR NOT NULL,
    model_id VARCHAR NOT NULL,
    milestone VARCHAR NOT NULL,
    fired_at TIMESTAMP NOT NULL,
    detail VARCHAR,
    PRIMARY KEY (engine, model_id, milestone)
);
"""

# Audit log of coins suppressed by the post-parabolic exclusion filter
# (crypto/ml/postparabolic_filter.py) at prediction-export time. One row per
# (export_date, symbol, model_id); UPSERTed so a re-run of the export for the
# same date is idempotent. dd90 = drawdown_from_90d_high, ret60 = return_60d,
# ret5 = return_5d (the feature values that tripped each rule); raw_probability
# = the model's calibrated probability before suppression. ``reason`` records
# which rule(s) fired — ``post_parabolic`` / ``short_momentum`` /
# ``post_parabolic_and_short_momentum`` (the ret5 column was added in
# ADR-028 alongside Rule B). See ADR-021 + ADR-028 in DECISIONS.md.
SCHEMA_CRYPTO_SIGNAL_EXCLUSIONS = """
CREATE TABLE IF NOT EXISTS crypto_signal_exclusions (
    export_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    model_id VARCHAR NOT NULL,
    raw_probability DOUBLE,
    dd90 DOUBLE,
    ret60 DOUBLE,
    ret5 DOUBLE,
    reason VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (export_date, symbol, model_id)
);
"""

# Idempotent migration for the ADR-028 ``ret5`` column — applied after
# create_all_tables so an existing copy of the table picks the column up.
_CRYPTO_SIGNAL_EXCLUSIONS_MIGRATIONS = [
    "ALTER TABLE crypto_signal_exclusions "
    "ADD COLUMN IF NOT EXISTS ret5 DOUBLE",
]

ALL_SCHEMAS = [
    SCHEMA_CRYPTO_PRICES_DAILY,
    SCHEMA_CRYPTO_FUNDING_RATES,
    SCHEMA_CRYPTO_OPEN_INTEREST,
    SCHEMA_CRYPTO_UNIVERSE,
    SCHEMA_CRYPTO_ML_LABELS,
    SCHEMA_CRYPTO_ML_FEATURES,
    SCHEMA_CRYPTO_ML_PREDICTIONS,
    SCHEMA_CRYPTO_ML_MODEL_RUNS,
    SCHEMA_CRYPTO_SIGNAL_EXCLUSIONS,
    SCHEMA_CRYPTO_DATA_QUALITY_REPORTS,
    SCHEMA_PHASE0_MILESTONES,
]


def create_all_tables(conn):
    for schema in ALL_SCHEMAS:
        conn.execute(schema)
    for stmt in _CRYPTO_ML_LABELS_MIGRATIONS:
        conn.execute(stmt)
    for stmt in _CRYPTO_ML_MODEL_RUNS_MIGRATIONS:
        conn.execute(stmt)
    for stmt in _CRYPTO_SIGNAL_EXCLUSIONS_MIGRATIONS:
        conn.execute(stmt)
