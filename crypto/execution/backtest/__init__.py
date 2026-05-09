"""Crypto execution backtest (Phase 1).

See SPEC.md in this directory for the full design. This package is fully
isolated from the equity, FX, and live crypto pipelines: it only reads
from existing crypto tables and writes to new tables prefixed
``crypto_backtest_*``.
"""
