"""OKX Stage B WS — ctVal (contract-value) table + contracts->coin conversion.

OKX WS sizes (`sz` on trades/liquidation, level sizes on bbo-tbt) are in CONTRACTS;
the byte-identical Binance rows store coin-denominated quantities. The conversion is
`contracts x ctVal` and MUST use Decimal (ctVal is a decimal string: BTC 0.01, ETH 0.1,
PEPE 10000000, SHIB 1000000) — float would lose precision on the multiplier symbols.
"""
from __future__ import annotations

from decimal import Decimal

from crypto.research.capture_core_okx import ctval


def test_ctval_loaded_as_decimal_from_instruments_payload():
    # /public/instruments rows (the same payload the universe filter consumes).
    instruments = [
        {"instId": "BTC-USDT-SWAP", "ctType": "linear", "settleCcy": "USDT",
         "state": "live", "instCategory": "1", "ctVal": "0.01"},
        {"instId": "ETH-USDT-SWAP", "ctType": "linear", "settleCcy": "USDT",
         "state": "live", "instCategory": "1", "ctVal": "0.1"},
        {"instId": "PEPE-USDT-SWAP", "ctType": "linear", "settleCcy": "USDT",
         "state": "live", "instCategory": "1", "ctVal": "10000000"},
        # non-universe rows must be excluded (inverse, equity perp, dead)
        {"instId": "BTC-USD-SWAP", "ctType": "inverse", "settleCcy": "BTC",
         "state": "live", "instCategory": "1", "ctVal": "100"},
        {"instId": "AAPL-USDT-SWAP", "ctType": "linear", "settleCcy": "USDT",
         "state": "live", "instCategory": "3", "ctVal": "0.1"},
    ]

    table = ctval.parse_ctval_table(instruments)

    assert table == {
        "BTC-USDT-SWAP": Decimal("0.01"),
        "ETH-USDT-SWAP": Decimal("0.1"),
        "PEPE-USDT-SWAP": Decimal("10000000"),
    }
    # values are Decimal, not float
    assert all(isinstance(v, Decimal) for v in table.values())


def test_decimal_ctval_contracts_to_coin_exact():
    # (contract size string, ctVal, expected coin string) — exact, no float rounding.
    cases = [
        ("3", Decimal("0.01"), "0.03"),          # BTC: 3 contracts -> 0.03 BTC
        ("15", Decimal("0.1"), "1.5"),           # ETH: 15 contracts -> 1.5 ETH
        ("2", Decimal("10000000"), "20000000"),  # PEPE: no sci-notation, no float error
        ("7", Decimal("1000000"), "7000000"),    # SHIB
        ("0", Decimal("0.01"), "0.00"),          # zero contracts (scale-preserving)
    ]
    for sz, ct, expected in cases:
        coin = ctval.contracts_to_coin(sz, ct)
        assert coin == expected, f"{sz} x {ct} -> {coin!r} != {expected!r}"
        # the brain WS readers cast q/B/A via float() with no _safe_float: must not raise
        assert float(coin) == float(Decimal(sz) * ct)


def test_contracts_to_coin_avoids_scientific_notation():
    # large PEPE/SHIB products must be plain fixed-point (float-castable, byte-clean)
    coin = ctval.contracts_to_coin("123456", Decimal("10000000"))
    assert "E" not in coin and "e" not in coin
    assert coin == "1234560000000"
