# Universe correction: systemd timers

Two systemd timers operate the corrected universe pipeline introduced in
the `feat-universe-correction` branch (2026-05-16) and shipped as
`feat-universe-hysteresis-continuous` (2026-05-17). Both run daily: the
rank timer fills today's ranking buffer at 23:00 UTC; the build timer
applies hysteresis and writes the result to `crypto_universe` 30 minutes
later, so each 00:30 UTC predict run on the next day sees a freshly-
evaluated universe.

## Units

### `mhde-crypto-rank-universe-daily.{service,timer}`

- **What:** writes one row per top-100 USDT-perp symbol into
  `crypto_universe_ranking_buffer` for today (UTC). Idempotent on
  `(symbol, ranking_date)`. Does NOT modify `crypto_universe`.
- **Schedule:** daily at `23:00 UTC`. 30 min before the daily rebuild
  at 23:30 UTC consumes today's row; 90 min before the 00:30 UTC predict
  on the next day uses the resulting universe.
- **Runtime:** ~3 min wall clock (one Binance kline fetch per ~530
  eligible perps at 0.08s spacing).
- **Failure mode:** if Binance is down for an entire run, the buffer
  for that date is unchanged. A single missed day still leaves the
  prior 7 dates available because the rebuild reads "7 most recent
  distinct dates" rather than "last 7 calendar days". Sustained outages
  (≥7 consecutive misses) cause the rebuild to raise ValueError and
  exit non-zero — predict then keeps using the most recent successful
  universe rather than silently going stale.

### `mhde-crypto-build-universe-daily.{service,timer}`

- **What:** reads the 7 most recent buffer dates, applies hysteresis
  (ADD on 7-consecutive in_top_50=TRUE + ≥60d listed; REMOVE on
  7-consecutive in_top_50=FALSE), updates `crypto_universe` and
  `crypto_universe_pending`.
- **Schedule:** daily at `23:30 UTC`. 30 min after the daily ranking
  timer fires — so the rebuild always sees today's fresh entry as the
  most recent `ranking_date`. The hysteresis 7-consecutive rule means
  membership is stable across daily invocations except on transition
  days; predict on the next day at 00:30 UTC consumes the result.
- **Runtime:** seconds. No Binance calls except the lightweight
  `exchangeInfo` for onboard dates.
- **Failure mode:** if the buffer has fewer than 7 distinct dates, the
  rebuild raises `ValueError` and exits non-zero. This is intentional —
  surfaces a daily-timer outage so the operator can backfill before
  predict relies on a stale universe.

## Install (operator only — gated by Step 7 approval)

```bash
sudo cp /home/jpcg/MHDE/systemd/mhde-crypto-rank-universe-daily.service /etc/systemd/system/
sudo cp /home/jpcg/MHDE/systemd/mhde-crypto-rank-universe-daily.timer   /etc/systemd/system/
sudo cp /home/jpcg/MHDE/systemd/mhde-crypto-build-universe-daily.service /etc/systemd/system/
sudo cp /home/jpcg/MHDE/systemd/mhde-crypto-build-universe-daily.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mhde-crypto-rank-universe-daily.timer
sudo systemctl enable --now mhde-crypto-build-universe-daily.timer
```

## Status / logs / manual trigger

```bash
# All MHDE crypto timers, next-fire times:
systemctl list-timers --all 'mhde-crypto-*'

# Follow logs:
journalctl -u mhde-crypto-rank-universe-daily -f
journalctl -u mhde-crypto-build-universe-daily -f

# One-shot manual run (does NOT wait for timer):
sudo systemctl start mhde-crypto-rank-universe-daily.service
sudo systemctl start mhde-crypto-build-universe-daily.service

# Dry-run the daily rebuild without touching the DB:
cd /home/jpcg/MHDE && venv/bin/python main.py crypto build-universe --dry-run

# Inspect the ranking buffer:
duckdb /home/jpcg/MHDE/data/mhde.duckdb -c \
    "SELECT ranking_date, COUNT(*) FROM crypto_universe_ranking_buffer \
     GROUP BY 1 ORDER BY 1 DESC LIMIT 14"

# Inspect pending list:
duckdb /home/jpcg/MHDE/data/mhde.duckdb -c \
    "SELECT * FROM crypto_universe_pending ORDER BY eligible_after_date"
```

## Schedule summary

| Time (UTC) | What |
|---|---|
| 23:00 daily | `mhde-crypto-rank-universe-daily` (writes today's buffer row) |
| 23:30 daily | `mhde-crypto-build-universe-daily` (hysteresis rebuild) |
| 00:30 daily | `mhde-crypto-predict` (existing — reads fresh universe) |

## Disable / uninstall

```bash
sudo systemctl disable --now mhde-crypto-rank-universe-daily.timer
sudo systemctl disable --now mhde-crypto-build-universe-daily.timer
sudo rm /etc/systemd/system/mhde-crypto-rank-universe-daily.{service,timer}
sudo rm /etc/systemd/system/mhde-crypto-build-universe-daily.{service,timer}
sudo systemctl daemon-reload
```

## Related

- `DATABASE_SCHEMA.md` — `crypto_universe`, `crypto_universe_ranking_buffer`,
  `crypto_universe_pending`.
- `docs/PATH_TO_LIVE_PLAN.md` (Step 8 update) — Universe section.
- ADR for the methodology fix (Step 8 will also add this).
