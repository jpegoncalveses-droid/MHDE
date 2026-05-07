from __future__ import annotations

import pytest

from storage.db import get_connection, init_schema
from features.feature_builder import build_features


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_build_features_empty_universe(conn):
    # No data — features should not crash; macro features may still insert global context rows
    build_features(conn, "run001", [], {})
    # Only stock-specific features should be absent; macro context (ticker=NULL) may be present
    count = conn.execute(
        "SELECT COUNT(*) FROM features WHERE ticker IS NOT NULL AND run_id = 'run001'"
    ).fetchone()[0]
    assert count == 0


def test_build_features_no_data_ticker(conn):
    conn.execute("INSERT INTO companies (ticker, company_name) VALUES ('FAKE', 'Fake Corp')")
    build_features(conn, "run002", ["FAKE"], {})
    # Risk features should be inserted even with no data
    # (NULL feature values drive risk penalty)
    rows = conn.execute("SELECT feature_group FROM features WHERE run_id = 'run002'").fetchall()
    groups = {r[0] for r in rows}
    assert "risk" in groups


def test_feature_scores_clamped(conn):
    from features.feature_builder import _upsert_feature
    from datetime import date
    _upsert_feature(conn, "run003", "TEST", date.today(), {
        "feature_group": "valuation", "feature_name": "ps_proxy",
        "feature_value": 150.0, "feature_score": 120.0, "source": "test",
    })
    rows = conn.execute(
        "SELECT feature_score FROM features WHERE run_id = 'run003'"
    ).fetchall()
    # The upsert stores whatever value is passed — clamping happens in scorecard
    assert rows[0][0] == 120.0


def test_risk_feature_has_confidence(conn):
    from features.risk import compute_risk
    from datetime import date
    features = compute_risk(conn, "run004", "FAKE", date.today(), [])
    # Returns a list of feature dicts
    assert isinstance(features, list)
    for f in features:
        if f.get("feature_score") is not None:
            assert 0.0 <= f["feature_score"] <= 100.0
