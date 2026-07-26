# OKX Capture — Stage B (WS firehose): design decisions & gate

Stage B adds the four **fast WebSocket sources** — `trades`, `bookticker`, `markprice`,
`forceorder` — into the existing OKX root `data/research/capture_core_okx`, using the
**shared** `capture_core` store with write-time normalization that produces rows
**byte-identical** to the Binance WS dataset schemas. Depth is **excluded** (Stage C, disk-gated).

Two anchoring constraints (project memory `okx_venue_seam_and_gate`):

1. **Venue seam at the CLIENT boundary.** The OKX WS manager is fresh; arg-routing (incl.
   injecting the `instId` that `bbo-tbt` frames omit) lives *inside* the WS client. By the time
   rows reach the store they are byte-identical to Binance (same column names, field letters,
   symbol convention). The shared store/partitioning/maintenance is unchanged.
2. **`forceorder` gate is plumbing-not-liveness.** Live liquidations can be zero over the gate
   window; the gate passes on a synthetic/replayed liquidation row parsing correctly and the
   cursor advancing — not on catching a live one.

## Byte-identical target (from the Binance normalizers, `capture_core/service.py:91-168`)

| dataset | stored cols (schema order) | OKX WS channel | key mappings |
|---|---|---|---|
| `aggTrade` | recv_ts_ns,e,E,a,s,p,q,f,l,T,m | `trades` | a=l=`tradeId`; f=`tradeId-count+1`; m=(`side`=='sell'); q=`sz`×ctVal |
| `bookTicker` | recv_ts_ns,e,u,s,b,B,a,A,T,E | `bbo-tbt` | u=`seqId`; s **injected from sub-arg**; B/A=`sz`×ctVal; E=T=`ts` |
| `markPrice` | recv_ts_ns,e,E,s,p,i,P,r,T | `mark-price`+`index-tickers`+`funding-rate` | p=`markPx`; i=`idxPx`; r=`fundingRate`; T=`fundingTime`; **P=`markPx`** (see D1) |
| `forceOrder` | recv_ts_ns,E,s,S,o,f,q,p,ap,X,l,z,T (**no `e`**) | `liquidation-orders` | p=ap=`bkPx`; q=`sz`×ctVal; S=`side`; E=T=`ts`; o/f/X/l/z='' |

## Decisions (resolving the mapping open-questions)

- **D1 — markPrice `P` (estimatedSettlePrice) = `markPx`.** OKX has no settle-price field, but
  `read_new_markprice` casts `float(r["P"])` with no `_safe_float` tolerance (reader.py:510), so
  `P=""` would crash the reader/gate, and `P` feeds `settle_*` OHLC persisted to
  `MARKPRICE_SNAPSHOT_SCHEMA`. **Empirical check of the live Binance capture: `P` is never 0 and
  tracks mark within ~0.04%** (0/9812 rows zero). Copying `markPx` into `P` therefore makes OKX
  `settle_*` ≈ `mark_*`, consistent with Binance's own behavior — byte-identical schema, no reader
  change, closest honest proxy. **Operator veto point at review** (alternative: extend the reader
  to `_safe_float` and store `P=""`).
- **D2 — ctVal.** New `capture_core_okx/ctval.py`: a `Decimal`-typed table keyed by `instId`,
  parsed from the same `/api/v5/public/instruments` payload the universe filter already fetches
  (`symbols.py`), refreshed on the same hourly universe re-resolve. `contracts→coin` uses
  `Decimal` (BTC 0.01 / ETH 0.1 / PEPE 10000000 / SHIB 1000000), never float. First ctVal facility
  in the codebase.
- **D3 — bbo-tbt qty unit = contracts.** OKX order-book level sizes are in contracts for SWAP, so
  B/A get `×ctVal` like trades/liquidation `sz`. **Verified against a real frame at the live
  gate**; the conversion is isolated so a flip to coin-units is one line if the gate contradicts.
- **D4 — daemon = one `Type=simple` WS collector** for all four channels on the single keyless
  public plane (`wss://ws.okx.com:8443/ws/v5/public`), mirroring the Stage A REST twin
  (`mhde-capture-okx-rest.service`). No sd_notify/watchdog/sharding for Stage B (lower OKX rates;
  can shard later). BUILT-NOT-DEPLOYED, no sibling timer.
- **D5 — markPrice merge cadence = 1s per symbol**, mirroring Binance `!markPrice@arr@1s`
  cardinality: emit one merged row per symbol per 1s tick from last-seen `mark`/`index`/`funding`.
- **D6 — aggTrade id derivation** per recon (`a=l=tradeId`, `f=tradeId-count+1`); field spellings
  (`count`, level array shape, liquidation nesting) **confirmed at the live gate** (no saved frame
  exists to pin them earlier).
- **D7 — `e` / unused string fields.** `e` stores the Binance literal event-type; `forceOrder`
  keeps its schema with **no `e` column**; the no-equivalent string fields (`o/f/X/l/z`) store `""`
  (not projected by `read_new_forceorder`, so no cast risk).
- **D8 — maintenance verbs.** Stage B emits **firehose** datasets, so maintenance reuses
  `capture-firehose-expire` / `capture-firehose-compact --root data/research/capture_core_okx`
  (Type=oneshot + Persistent timers, `OnCalendar` staggered off the Binance twins) — *not* the
  `capture-asof-compact` used by the Stage A REST series.

## Gate (Stage B acceptance)

Run the WS collectors live against OKX public WS into a **fresh scratch root**, write the four
datasets, point the **actual brain WS readers** (`read_new_{aggtrades,bookticker,markprice,
forceorder}`) at that root, and prove: fragments parse (non-empty, correct types) and per-source
cursors advance across the window. `forceorder` per D-plumbing (synthetic/replayed liquidation row).
Report sustained message rate and reconnect behaviour over the window. **BUILT-NOT-DEPLOYED** — no
unit enabled; live Binance capture, OKX Stage-A capture, and the brain tick loop stay untouched.

### Live gate result (PASS)

Ran `capture-okx-ws-run` against `wss://ws.okx.com:8443/ws/v5/public` for a 30 s window into a
fresh throwaway temp root, universe pinned to `BTC/ETH/SOL/XRP/DOGE-USDT-SWAP`, then read the four
datasets back through the **actual** brain readers (`read_new_{aggtrades,bookticker,markprice,
forceorder}`):

- **9 758 frames routed, ~243 frames/s sustained, 0 reconnects** over the window.
- **aggTrade** — 2 766 rows, cursor advances; sample `ETHUSDT price=1944.0 qty=0.038`
  (contracts→coin via ctVal, float-castable).
- **bookTicker** — 5 661 rows, cursor advances; sample `ETHUSDT bid=1943.95 bid_qty=129.404
  ask=1943.96 ask_qty=127.504` (instId injected from `arg`, sizes ×ctVal — confirms **D3**).
- **markPrice** — 150 rows (5 symbols × ~30 × 1 s, confirms **D5**), cursor advances; sample
  `BTCUSDT mark=65362.8 index=65388.6 settle=65362.8 funding=6.34e-05` — `settle == mark`
  confirms **D1** live and `read_new_markprice`'s `float(P)` never raised.
- **forceOrder** — 2 live liquidations caught, cursor advances; sample `BTCUSDT side=BUY
  qty=0.1105 price=65621.7` (parses through the gate reader; plumbing holds even though live
  liquidations can be zero over a window).

All four readers parsed non-empty, correctly-typed rows and advanced their per-source cursors →
**GATE PASS**. The temp root was discarded; the live OKX root and every running service were
untouched.
