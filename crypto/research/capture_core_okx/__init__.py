"""OKX capture collectors — Stage A: the 7 REST as-of series + 1h klines.

Sibling package to :mod:`crypto.research.capture_core` (Binance), per the
2026-07-03 migration recon (``data/processed/okx_migration_recon.md``). The
venue-agnostic machinery — ``RestPresentStateCollector``, ``store`` writers,
``maintenance`` compaction/expiry, dedup cursors — is IMPORTED UNCHANGED from
capture_core; this package supplies only the OKX-specific pieces: config,
REST client (incl. the composite join fetches), the series registry, symbol
normalization, and the klines seed.

Write-time normalization is the contract: the OKX collectors write the SAME
dataset names, parquet schemas, and field letters as the Binance collectors,
under Binance-style symbol names, into a SEPARATE root
(``data/research/capture_core_okx``) — so the brain readers work against
either root via ``--capture-root`` with zero code changes.

Public market data only; no API key anywhere. NEVER opens mhde.duckdb or the
engine DB. BUILT-NOT-DEPLOYED until the operator enables the okx units.
"""
