-- MHDE Engine Schema v1
-- All tables use VARCHAR IDs (Python-generated UUIDs) unless noted.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description VARCHAR
);

CREATE TABLE IF NOT EXISTS companies (
    ticker VARCHAR PRIMARY KEY,
    cik VARCHAR,
    company_name VARCHAR NOT NULL,
    exchange VARCHAR,
    sector VARCHAR,
    industry VARCHAR,
    is_active BOOLEAN DEFAULT true,
    is_etf BOOLEAN DEFAULT false,
    is_fund BOOLEAN DEFAULT false,
    is_adr BOOLEAN DEFAULT false,
    market_cap DOUBLE,
    last_seen_at TIMESTAMP,
    universe_tier VARCHAR DEFAULT 'extended',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_runs (
    id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    source_name VARCHAR NOT NULL,
    use_case VARCHAR DEFAULT '',
    status VARCHAR NOT NULL,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    records_attempted INTEGER DEFAULT 0,
    records_inserted INTEGER DEFAULT 0,
    records_failed INTEGER DEFAULT 0,
    error_message VARCHAR,
    metadata_json VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS filings (
    id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    cik VARCHAR,
    form_type VARCHAR NOT NULL,
    accession_number VARCHAR,
    filing_date DATE,
    report_date DATE,
    description VARCHAR,
    doc_url VARCHAR,
    run_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fundamentals_raw (
    id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    cik VARCHAR,
    concept VARCHAR NOT NULL,
    value DOUBLE,
    unit VARCHAR,
    as_of_date DATE,
    period_of_report DATE,
    form VARCHAR,
    run_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fundamentals_features (
    id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    revenue DOUBLE,
    net_income DOUBLE,
    shares_outstanding DOUBLE,
    revenue_growth_yoy DOUBLE,
    net_margin DOUBLE,
    dilution_rate DOUBLE,
    pe_proxy DOUBLE,
    ps_proxy DOUBLE,
    data_freshness_days INTEGER,
    run_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, as_of_date)
);

CREATE TABLE IF NOT EXISTS prices_daily (
    id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE NOT NULL,
    volume BIGINT,
    adjusted_close DOUBLE,
    source VARCHAR DEFAULT 'polygon',
    run_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, trade_date)
);

CREATE TABLE IF NOT EXISTS macro_series (
    id VARCHAR PRIMARY KEY,
    series_id VARCHAR NOT NULL,
    series_name VARCHAR,
    value DOUBLE,
    as_of_date DATE NOT NULL,
    frequency VARCHAR,
    source VARCHAR DEFAULT 'fred',
    run_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (series_id, as_of_date)
);

CREATE TABLE IF NOT EXISTS short_interest (
    id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    settlement_date DATE NOT NULL,
    short_interest BIGINT,
    avg_daily_volume BIGINT,
    days_to_cover DOUBLE,
    source VARCHAR DEFAULT 'finra',
    run_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, settlement_date)
);

CREATE TABLE IF NOT EXISTS events (
    id VARCHAR PRIMARY KEY,
    ticker VARCHAR,
    event_type VARCHAR NOT NULL,
    event_date DATE,
    title VARCHAR,
    description VARCHAR,
    source VARCHAR,
    is_upcoming BOOLEAN DEFAULT false,
    run_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS features (
    id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ticker VARCHAR,
    as_of_date DATE NOT NULL,
    feature_group VARCHAR NOT NULL,
    feature_name VARCHAR NOT NULL,
    feature_value DOUBLE,
    feature_score DOUBLE,
    source VARCHAR,
    confidence VARCHAR DEFAULT 'medium',
    metadata_json VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, ticker, feature_group, feature_name)
);

CREATE TABLE IF NOT EXISTS scores (
    id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    cheap_score DOUBLE,
    quality_score DOUBLE,
    catalyst_score DOUBLE,
    momentum_score DOUBLE,
    sentiment_score DOUBLE,
    risk_penalty DOUBLE,
    total_score DOUBLE NOT NULL,
    tier VARCHAR NOT NULL,
    confidence VARCHAR DEFAULT 'medium',
    why_ranked VARCHAR,
    why_rejected VARCHAR,
    missing_data_json VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, ticker)
);

CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    company_name VARCHAR,
    rank INTEGER,
    tier VARCHAR NOT NULL,
    total_score DOUBLE,
    thesis VARCHAR,
    why_now VARCHAR,
    cheap_evidence_json VARCHAR,
    quality_evidence_json VARCHAR,
    catalyst_evidence_json VARCHAR,
    risks_json VARCHAR,
    missing_evidence_json VARCHAR,
    status VARCHAR DEFAULT 'new' CHECK (status IN ('new', 'watch', 'research', 'rejected', 'archived')),
    review_status VARCHAR DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rejections (
    id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    reason VARCHAR,
    risk_flags_json VARCHAR,
    missing_data_json VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candidate_outcomes (
    candidate_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    tier VARCHAR,
    total_score DOUBLE,
    reference_price DOUBLE,
    forward_return_1d DOUBLE,
    forward_return_5d DOUBLE,
    forward_return_20d DOUBLE,
    forward_return_60d DOUBLE,
    forward_return_120d DOUBLE,
    max_drawdown_20d DOUBLE,
    max_drawdown_60d DOUBLE,
    max_runup_20d DOUBLE,
    max_runup_60d DOUBLE,
    hit_10pct_before_down_10pct BOOLEAN,
    hit_20pct_before_down_10pct BOOLEAN,
    review_status VARCHAR DEFAULT 'pending' CHECK (review_status IN (
        'pending', 'validated', 'false_positive',
        'needs_more_time', 'invalid_due_to_data_issue', 'archived'
    )),
    review_notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, ticker)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    backtest_run_id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    as_of_date DATE,
    lookback_days INTEGER,
    forward_days INTEGER DEFAULT 20,
    tickers_tested INTEGER DEFAULT 0,
    hit_rate DOUBLE,
    avg_return DOUBLE,
    metrics_json VARCHAR,
    warning VARCHAR,
    status VARCHAR DEFAULT 'complete',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_runs (
    model_run_id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    model_type VARCHAR DEFAULT 'xgboost',
    target VARCHAR,
    train_start_date DATE,
    train_end_date DATE,
    test_start_date DATE,
    test_end_date DATE,
    metrics_json VARCHAR,
    feature_importance_json VARCHAR,
    status VARCHAR DEFAULT 'complete',
    warning VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llm_runs (
    llm_run_id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    ticker VARCHAR,
    job_type VARCHAR,
    provider VARCHAR,
    model VARCHAR,
    prompt_version VARCHAR,
    input_hash VARCHAR,
    output_hash VARCHAR,
    input_json VARCHAR,
    output_json VARCHAR,
    estimated_tokens INTEGER,
    estimated_cost DOUBLE,
    status VARCHAR DEFAULT 'ok',
    error_message VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    ticker VARCHAR,
    channel VARCHAR NOT NULL,
    alert_type VARCHAR,
    status VARCHAR DEFAULT 'sent',
    dedupe_key VARCHAR,
    message VARCHAR,
    sent_at TIMESTAMP,
    error_message VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    pipeline_run_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    run_date DATE NOT NULL,
    pipeline_type VARCHAR DEFAULT 'daily_radar',
    universe_size INTEGER DEFAULT 0,
    sources_succeeded INTEGER DEFAULT 0,
    sources_failed INTEGER DEFAULT 0,
    sources_skipped INTEGER DEFAULT 0,
    candidates_scored INTEGER DEFAULT 0,
    tier_a INTEGER DEFAULT 0,
    tier_b INTEGER DEFAULT 0,
    tier_c INTEGER DEFAULT 0,
    rejected INTEGER DEFAULT 0,
    hypotheses_created INTEGER DEFAULT 0,
    alerts_sent INTEGER DEFAULT 0,
    llm_provider VARCHAR DEFAULT 'mock',
    report_path VARCHAR,
    warnings_json VARCHAR,
    status VARCHAR DEFAULT 'complete',
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_notes (
    note_id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    run_id VARCHAR,
    hypothesis_id VARCHAR,
    note_type VARCHAR DEFAULT 'general',
    body VARCHAR NOT NULL,
    author VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dashboard_actions (
    action_id VARCHAR PRIMARY KEY,
    action_type VARCHAR NOT NULL,
    target_table VARCHAR,
    target_id VARCHAR,
    payload_json VARCHAR,
    performed_by VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candidate_reviews (
    review_id VARCHAR PRIMARY KEY,
    candidate_id VARCHAR,
    run_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    review_status VARCHAR DEFAULT 'pending' CHECK (review_status IN (
        'pending', 'useful', 'weak', 'false_positive',
        'needs_more_evidence', 'invalid_due_to_data_issue', 'archived'
    )),
    usefulness_score INTEGER CHECK (usefulness_score BETWEEN 1 AND 5),
    thesis_quality_score INTEGER CHECK (thesis_quality_score BETWEEN 1 AND 5),
    evidence_quality_score INTEGER CHECK (evidence_quality_score BETWEEN 1 AND 5),
    false_positive_reason VARCHAR CHECK (false_positive_reason IN (
        'bad_data', 'stale_data', 'cheap_for_good_reason', 'weak_catalyst',
        'poor_quality_business', 'macro_headwind', 'llm_overstated_case',
        'missing_peer_context', 'temporary_noise', 'not_actionable',
        'overfit_score', 'insufficient_liquidity', 'missing_risk_factor',
        'source_failure', 'other'
    )),
    missed_risk VARCHAR,
    missing_evidence VARCHAR,
    review_notes VARCHAR,
    reviewed_by VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, ticker)
);

CREATE TABLE IF NOT EXISTS scorecard_experiments (
    experiment_id VARCHAR PRIMARY KEY,
    based_on_run_ids VARCHAR,
    hypothesis VARCHAR NOT NULL,
    proposed_change_json VARCHAR,
    affected_components_json VARCHAR,
    expected_effect VARCHAR,
    backtest_result_json VARCHAR,
    backtest_notes VARCHAR,
    status VARCHAR DEFAULT 'proposed' CHECK (status IN (
        'proposed', 'tested', 'approved', 'rejected', 'applied', 'archived'
    )),
    review_notes VARCHAR,
    approved_by VARCHAR,
    applied_by VARCHAR,
    applied_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS health_checks (
    id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    check_name VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    severity VARCHAR DEFAULT 'medium',
    message VARCHAR,
    metadata_json VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
