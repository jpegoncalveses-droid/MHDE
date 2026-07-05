"""OKX capture configuration constants (Stage A: as-of series + klines).

Cadence parity with the Binance collectors is deliberate (the brain sees the
same sampling rhythm from either venue): 60s open-interest/premium, the
coarsened 1200s ratio cadence, hourly klines. The Binance /futures/data
budget machinery has NO OKX analog — OKX rate-limit buckets are per-endpoint
and mostly per-instId ("IP + Instrument ID"), so a fixed inter-request delay
is the only pacing needed (worst per-endpoint limit relevant to us is
5 req/2s per (IP, instId); our per-instId revisit is one request per 1200s).
"""
from __future__ import annotations

from crypto.research.capture_core import config as cc_cfg

#: Root of the OKX raw capture tree — a SEPARATE root from the Binance
#: ``capture_core`` tree (recon Q4: venue is a root-path concern, never a
#: partition dimension). Same gitignore coverage via the ``data/research/`` rule.
RAW_DIR = "data/research/capture_core_okx"

#: Public REST base. ``www.okx.com`` is live-verified (2026-07-03 probes); the
#: docs now name ``openapi.okx.com`` as the production alias — swap here if
#: www is ever deprecated. Public market data needs no key on either.
OKX_REST_BASE = "https://www.okx.com"

#: Fixed inter-request pacing (seconds). ~6.7 req/s aggregate ceiling across
#: DIFFERENT endpoints/instIds — far under every documented per-bucket limit
#: (plain-IP endpoints we poll are 10-20 req/2s; rubik is per-instId).
REQUEST_DELAY_S = 0.15
REST_MAX_RETRIES = 5

# -- cadences (Binance parity, see module docstring) --
OI_CADENCE_S = 60.0
PREMIUM_INDEX_CADENCE_S = 60.0
#: The 4 rubik ratio series + derived basis sample on the Binance-coarsened
#: cadence (1200s) — parity, not an OKX rate-limit necessity.
RATIO_CADENCE_S = cc_cfg.FUTURES_DATA_CADENCE_S
#: Rubik window per poll: 1200s cadence / 300s native buckets = 4, x2 so one
#: missed poll self-heals (same math as Binance ``_FD_LIMIT``); the collector
#: dedups overlapping buckets on ``timestamp``.
RUBIK_WINDOW_LIMIT = 8

# -- klines (Binance parity: hourly maintenance, closed bars only) --
OKX_KLINES_BAR = "1H"                       # OKX bar enum for the 1h interval
#: history-candles page size for the one-time seed (OKX max 100/request).
KLINES_SEED_PAGE_LIMIT = 100

# -- gap manifest (collector-outage contract, recon Q1/gap section) --
#: A successful poll arriving more than ``factor x cadence`` after the previous
#: success records a ``_gaps`` row for that series: the silence was a capture
#: hole (collector down / venue unreachable), not a normal poll interval.
GAP_SILENCE_FACTOR = 2.5
GAP_REASON = "rest_outage"

#: Universe re-resolve interval — same rhythm as Binance (newly-listed
#: instruments enter without a restart).
UNIVERSE_RERESOLVE_INTERVAL_S = cc_cfg.UNIVERSE_RERESOLVE_INTERVAL_S
