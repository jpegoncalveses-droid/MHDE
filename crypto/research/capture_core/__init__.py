"""Capture-core: raw, lossless Binance USDT-M futures market-data capture.

A research substrate that persists **every raw market event** (no derived
features, no opinionated universe) so any future feature/label can be
reconstructed offline. Deliberately isolated from the live prediction/execution
path:

  * Read-only against Binance USDT-M **public** WS + REST endpoints (no auth).
  * Writes ONLY parquet under ``data/research/capture_core/`` — never the
    production ``mhde.duckdb`` or the engine DB; it is not a DuckDB writer.
  * Universe is the full set of TRADING USDT-M perps, resolved live from
    ``exchangeInfo`` and re-resolved on a cadence (new listings enter
    automatically).

See ``docs`` / the capture-core plan for the stream-set and PR-split rationale.
"""
