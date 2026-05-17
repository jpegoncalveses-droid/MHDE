"""Unit tests for helpers in crypto/ingestion/universe_builder.py.

The hysteresis-based ``build_universe`` itself is tested in
``test_universe_hysteresis.py``. The exclusion behavior previously tested
here is now tested at the producer side — see
``test_rank_universe_daily.py`` and ``test_backfill_universe_rankings.py``.
"""
from crypto.ingestion.universe_builder import _is_safe_symbol


def test_safe_symbol_accepts_canonical_perp_tickers():
    assert _is_safe_symbol("BTCUSDT")
    assert _is_safe_symbol("ETHUSDT")
    assert _is_safe_symbol("1000PEPEUSDT")
    assert _is_safe_symbol("4USDT")


def test_safe_symbol_rejects_non_ascii():
    """Real Binance pairs seen 2026-05-16: CJK-encoded base assets."""
    assert not _is_safe_symbol("币安人生USDT")
    assert not _is_safe_symbol("我踏马来了USDT")
    assert not _is_safe_symbol("龙虾USDT")


def test_safe_symbol_rejects_lowercase():
    assert not _is_safe_symbol("btcusdt")
    assert not _is_safe_symbol("BtcUsdt")


def test_safe_symbol_rejects_hyphen_or_other_punctuation():
    assert not _is_safe_symbol("FOO-BARUSDT")
    assert not _is_safe_symbol("FOO_BARUSDT")
    assert not _is_safe_symbol("FOO.BARUSDT")


def test_safe_symbol_rejects_non_usdt_quote():
    assert not _is_safe_symbol("BTCUSDC")
    assert not _is_safe_symbol("BTCBUSD")
    assert not _is_safe_symbol("BTCUSD")
    # USDT-prefix only doesn't match — must END in USDT.
    assert not _is_safe_symbol("USDTBTC")


def test_safe_symbol_rejects_empty_base():
    assert not _is_safe_symbol("USDT")
