"""Brain subsystem configuration (Phase 1).

Isolation contract (enforced by tests + structure):
  * The brain has its OWN writer domain: a parquet event store under
    ``data/research/brain/`` and a small SQLite-WAL registry at
    ``data/research/brain/registry.sqlite``. Both are gitignored.
  * It reads capture_core's tape READ-ONLY (the ``aggTrade`` dataset under
    ``data/research/capture_core/``) and NEVER opens ``mhde.duckdb``, the
    engine DB, or capture's store for writing.
  * It is NOT registered in ``crypto.schema.ALL_SCHEMAS`` (dodges the DuckDB
    single-writer contention MHDE and the engine both hit).

The settled-window watermark is derived from capture's REAL flush cadence so
it stays in sync: a window only emits once it is at least
``CAPTURE_FIREHOSE_FLUSH_S + BRAIN_BASE_CADENCE_S`` behind ``now`` — a full
flush interval (so every trailing trade of the window is on disk) plus one
window of slack. The capture flush constant is imported, not copied. Importing
``capture_core.config`` reads pure module-level constants only — no DB is
opened and nothing is written; it is the one intentional coupling, and it keeps
the watermark synced to the real cadence.
"""
from __future__ import annotations

from crypto.research.capture_core import config as capture_cfg

#: Capture tape we consume, READ-ONLY: ``<RAW_DIR>/aggTrade/symbol=*/date=*``.
CAPTURE_RAW_DIR = capture_cfg.RAW_DIR
AGGTRADE_DATASET = "aggTrade"

#: Brain's own writer domain (gitignored; NEVER mhde.duckdb / the engine DB).
BRAIN_STORE_ROOT = "data/research/brain"
#: SQLite-WAL registry: the reader cursor (last processed recv_ts_ns) + snapshot
#: bookkeeping. SQLite-WAL is chosen over DuckDB so concurrent readers later
#: never contend with the writer (DuckDB allows a single writer at a time).
BRAIN_REGISTRY_PATH = "data/research/brain/registry.sqlite"

#: Per-source datasets. For each: the capture dir we read (READ-ONLY), the brain
#: store dataset we write, and the registry reader/cursor name. Each source has
#: its OWN cursor so they advance independently.
TRADES_DATASET = "trades"
TRADES_READER = "trades"

BOOKTICKER_CAPTURE_DATASET = "bookTicker"
BOOKTICKER_DATASET = "bookticker"
BOOKTICKER_READER = "bookticker"

MARKPRICE_CAPTURE_DATASET = "markPrice"
MARKPRICE_DATASET = "markprice"
MARKPRICE_READER = "markprice"

FORCEORDER_CAPTURE_DATASET = "forceOrder"
FORCEORDER_DATASET = "forceorder"
FORCEORDER_READER = "forceorder"

# As-of (REST present-state) sources. For these the capture dir name is already
# snake_case, so capture dir == brain store dataset == registry cursor name.
OPEN_INTEREST_DATASET = "open_interest"
PREMIUM_INDEX_DATASET = "premium_index"
GLOBAL_LS_ACCOUNT_DATASET = "global_ls_account"
TOP_LS_ACCOUNT_DATASET = "top_ls_account"
TOP_LS_POSITION_DATASET = "top_ls_position"
TAKER_LS_RATIO_DATASET = "taker_ls_ratio"
BASIS_DATASET = "basis"

# klines_1h: the hourly-context bar (a multi-field as-of source). capture dir ==
# brain store dataset == cursor name.
KLINES_CAPTURE_DATASET = "klines_1h"
KLINES_DATASET = "klines_1h"

#: zstd, mirroring capture_core (compaction-friendly).
PARQUET_COMPRESSION = "zstd"

#: Base window cadence for the trades primitive. 60s is capture's natural
#: minute grain (and the signal_probe cadence): one snapshot per (symbol, minute).
BRAIN_BASE_CADENCE_S = 60.0
BRAIN_BASE_CADENCE_NS = int(BRAIN_BASE_CADENCE_S * 1_000_000_000)

#: Settled-window watermark, in seconds behind ``now``. Set FROM the real
#: capture flush cadence: a window's trailing trades are flushed within
#: ``CAPTURE_FIREHOSE_FLUSH_S`` of the window end, so wait that long (all rows
#: on disk) PLUS one base window of slack before treating a window as complete.
BRAIN_WATERMARK_S = capture_cfg.CAPTURE_FIREHOSE_FLUSH_S + BRAIN_BASE_CADENCE_S
BRAIN_WATERMARK_NS = int(BRAIN_WATERMARK_S * 1_000_000_000)
