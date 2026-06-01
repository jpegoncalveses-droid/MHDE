"""Unit tests for the shared retry-aware engine-DB read-only opener.

Covers the behaviour the monitors depend on to stop emitting false
"engine DuckDB not reachable" reds:
  - clean open when nothing holds the lock
  - retry-then-succeed when a transient write lock clears
  - re-raise after the (short) retry budget when the lock persists
  - fail FAST (no retry) when the database genuinely does not exist
  - a WARNING is logged on each retry
  - the path resolves from CRYPTO_ENGINE_DB_PATH when none is passed
"""
import duckdb
import pytest

from monitoring import engine_db

# Real duckdb 1.5.2 messages (captured in the Step-0 diagnostic).
LOCK_MSG = (
    'IO Error: Could not set lock on file "/x/trading_engine.duckdb": '
    "Conflicting lock is held in /usr/bin/python3.12 (PID 1) by user jpcg."
)
MISSING_MSG = (
    'IO Error: Cannot open database "/x/missing.duckdb" '
    "in read-only mode: database does not exist"
)


def test_opens_readonly_when_no_lock(tmp_path):
    db = tmp_path / "engine.duckdb"
    seed = duckdb.connect(str(db))
    seed.execute("CREATE TABLE t(x INTEGER)")
    seed.close()

    conn = engine_db.open_engine_db_readonly(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_retries_then_succeeds_when_lock_clears(monkeypatch, caplog):
    calls = {"n": 0}
    sentinel = object()

    def fake_connect(path, read_only=False):
        calls["n"] += 1
        if calls["n"] < 3:  # first two opens collide with the write lock
            raise duckdb.IOException(LOCK_MSG)
        return sentinel  # third clears

    sleeps: list[float] = []
    monkeypatch.setattr(engine_db.duckdb, "connect", fake_connect)
    monkeypatch.setattr(engine_db.time, "sleep", lambda s: sleeps.append(s))

    with caplog.at_level("WARNING", logger="mhde.monitoring.engine_db"):
        conn = engine_db.open_engine_db_readonly("/x/engine.duckdb")

    assert conn is sentinel
    assert calls["n"] == 3
    assert sleeps == [0.1, 0.25]  # one backoff per retry, in order
    assert sum("retrying" in r.message for r in caplog.records) == 2


def test_persistent_lock_reraises_after_budget(monkeypatch):
    calls = {"n": 0}

    def fake_connect(path, read_only=False):
        calls["n"] += 1
        raise duckdb.IOException(LOCK_MSG)

    sleeps: list[float] = []
    monkeypatch.setattr(engine_db.duckdb, "connect", fake_connect)
    monkeypatch.setattr(engine_db.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(duckdb.IOException):
        engine_db.open_engine_db_readonly("/x/engine.duckdb")

    # initial attempt + one per backoff delay, then give up
    assert calls["n"] == len(engine_db._LOCK_RETRY_DELAYS_SEC) + 1
    assert sleeps == list(engine_db._LOCK_RETRY_DELAYS_SEC)


def test_missing_db_fails_fast_without_retry(monkeypatch):
    calls = {"n": 0}

    def fake_connect(path, read_only=False):
        calls["n"] += 1
        raise duckdb.IOException(MISSING_MSG)

    slept: list[float] = []
    monkeypatch.setattr(engine_db.duckdb, "connect", fake_connect)
    monkeypatch.setattr(engine_db.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(duckdb.IOException):
        engine_db.open_engine_db_readonly("/x/missing.duckdb")

    assert calls["n"] == 1  # no retry on a genuinely-missing DB
    assert slept == []


def test_resolves_path_from_env(monkeypatch, tmp_path):
    db = tmp_path / "from_env.duckdb"
    seed = duckdb.connect(str(db))
    seed.execute("CREATE TABLE t(x INTEGER)")
    seed.close()
    monkeypatch.setenv(engine_db.ENGINE_DB_ENV, str(db))

    conn = engine_db.open_engine_db_readonly()  # no explicit path -> env
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()
