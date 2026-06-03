"""Capture-core configuration constants.

Research infrastructure, isolated from the live path (see the package
docstring). Mirrors the ``signal_probe`` config style and re-exports the shared
Binance base URL + request delay from :mod:`crypto.config`.
"""
from __future__ import annotations

from crypto.config import BINANCE_FUTURES_BASE, REQUEST_DELAY_S  # noqa: F401 (re-exported)

#: Root of the raw capture tree (gitignored via the ``data/research/`` rule).
#: Layout: ``<RAW_DIR>/<stream>/symbol=<SYM>/date=<YYYY-MM-DD>/part-*.parquet``.
RAW_DIR = "data/research/capture_core"

# -- WebSocket endpoints (USDT-M futures, PUBLIC) --
#: Combined-stream base; subscribe by appending ``a/b/c`` stream names.
WS_COMBINED_BASE = "wss://fstream.binance.com/stream?streams="

# -- Universe --
#: Re-resolve the TRADING USDT-M perp universe from ``exchangeInfo`` this often
#: so newly-listed symbols enter the substrate without a restart (operator GO
#: 2026-06-03: the universe is NOT frozen).
UNIVERSE_RERESOLVE_INTERVAL_S = 3600.0

# -- Sharding --
#: Streams per WS connection. Conservatively far under Binance's 1024/stream
#: connection cap, leaving message-throughput headroom for the 529-symbol
#: firehose. ~529 aggTrade streams -> 3 shards at this size.
STREAMS_PER_CONN = 200

# -- Parquet flush thresholds (flush on the EARLIER of the two) --
FLUSH_INTERVAL_S = 30.0
FLUSH_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB
PARQUET_COMPRESSION = "zstd"

# -- Reconnect (mirrors the engine ws_consumer discipline) --
RECONNECT_BACKOFF_BASE_S = 1.0
RECONNECT_BACKOFF_MAX_S = 60.0
RECONNECT_JITTER = 0.1  # ±10%

# -- Liveness / proactive reconnect --
#: websockets client ping cadence + pong deadline.
WS_PING_INTERVAL_S = 180.0
WS_PING_TIMEOUT_S = 30.0
#: No frame for this long => treat the socket as dead and reconnect.
SOCKET_SILENCE_TIMEOUT_S = 60.0
#: Binance force-closes a connection at 24h; reconnect each shard before then.
PROACTIVE_RECONNECT_S = 23.0 * 3600.0

# -- REST (order-book snapshot seeding, PR-2; 429/418 aware) --
DEPTH_SNAPSHOT_LIMIT = 1000
REST_MAX_RETRIES = 5
