from __future__ import annotations

import pytest

from storage.db import get_connection, init_schema
from models.xgboost_ranker import train_smoke
from models.dataset_builder import build_dataset


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def test_train_smoke_no_data(conn):
    result = train_smoke(conn, {})
    assert result is None


def test_build_dataset_returns_none_insufficient_data(conn):
    X, y, names = build_dataset(conn)
    assert X is None
    assert y is None
    assert names is None


def test_train_smoke_xgboost_not_installed(conn, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "xgboost":
            raise ImportError("No module named 'xgboost'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    result = train_smoke(conn, {})
    assert result is None


def test_model_run_warning_message():
    from models.xgboost_ranker import _WARNING
    assert "Experimental only" in _WARNING
    assert "Not used for alerts or rankings" in _WARNING
