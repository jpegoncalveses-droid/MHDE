"""Signal-probe collector configuration.

This is **research infrastructure**, deliberately isolated from the live
prediction/execution path:

  * Rows land in a **separate research DB**
    (``data/research/signal_probe.duckdb``) — NEVER the production
    ``mhde.duckdb`` (single-writer contention) and NEVER registered in
    ``crypto.schema.ALL_SCHEMAS``.
  * The collector is **read-only against Binance USDT-M PUBLIC endpoints**
    (no auth, no engine client) and never writes any production artifact.
  * The universe is **snapshotted here**, not read live from
    ``crypto_universe`` each cycle, so the collector touches no production
    DB at all. Refresh it by re-running the universe query and editing this
    list (see the module docstring of ``collector.py``).

The universe below is the ``crypto_universe`` active set as of
2026-06-02 (57 symbols; ``BTCUSDT`` already included and used as the
benchmark for the return-vs-BTC features).
"""
from __future__ import annotations

from crypto.config import BINANCE_FUTURES_BASE, REQUEST_DELAY_S  # noqa: F401 (re-exported)

#: Separate research DB (gitignored; NOT the production mhde.duckdb).
RESEARCH_DB_PATH = "data/research/signal_probe.duckdb"

#: Benchmark symbol for the return-vs-BTC features.
BTC_SYMBOL = "BTCUSDT"

#: Probe universe — snapshot of the active crypto_universe (2026-06-02).
#: BTCUSDT is part of the active set and doubles as the benchmark.
UNIVERSE: list[str] = [
    "1000LUNCUSDT", "1000PEPEUSDT", "4USDT", "AAVEUSDT", "ADAUSDT", "APEUSDT",
    "ASTERUSDT", "AVAXUSDT", "BCHUSDT", "BEATUSDT", "BIOUSDT", "BNBUSDT",
    "BSBUSDT", "BTCUSDT", "BUSDT", "DASHUSDT", "DOGEUSDT", "DOGSUSDT",
    "DOTUSDT", "EDENUSDT", "ENAUSDT", "ETHUSDT", "FHEUSDT", "FIDAUSDT",
    "FILUSDT", "HIGHUSDT", "HIVEUSDT", "HYPEUSDT", "INJUSDT", "KATUSDT",
    "LABUSDT", "LINKUSDT", "LTCUSDT", "NEARUSDT", "NILUSDT", "NOTUSDT",
    "ONDOUSDT", "ORCAUSDT", "PAXGUSDT", "PENGUUSDT", "PIEVERSEUSDT",
    "PLAYUSDT", "RAVEUSDT", "SAGAUSDT", "SKYAIUSDT", "SOLUSDT", "SUIUSDT",
    "SWARMSUSDT", "TAOUSDT", "TONUSDT", "TRUMPUSDT", "UBUSDT", "VVVUSDT",
    "WLDUSDT", "XRPUSDT", "ZBTUSDT", "ZECUSDT",
]

# -- Lookback windows (number of bars fetched per cycle) --
#: 1-minute klines fetched per cycle. The longest minute-scale feature is the
#: 60m ROC / SMA-100 / 60m-window family; +1 for the "prior bar" of the
#: acceleration terms; pad to 130 for headroom against a missing bar.
LOOKBACK_1M = 130
#: 1-hour klines fetched per cycle. 30 days = 720 bars; the 30d-high and the
#: 1h-50-SMA both read from this series. One request (< 1000/page).
LOOKBACK_1H = 730

# -- Open-interest history (period/limit for openInterestHist) --
#: openInterestHist minimum period is 5m; 13 points reaches back one hour.
OI_HIST_PERIOD = "5m"
OI_HIST_LIMIT = 16

# -- Order-book depth (optional) --
#: Capture depth-imbalance + spread. Set False to drop the per-symbol depth
#: call entirely (one fewer request per symbol per cycle).
INCLUDE_DEPTH = True
#: Depth levels to request (enough to cover ±0.5% of mid for liquid perps).
DEPTH_LIMIT = 50
