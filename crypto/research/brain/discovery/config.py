"""Brain discovery engine configuration — the §14 parameter choices, one place.

ALL values here are operator-tunable. Defaults are chosen conservatively and the
reasoning is inline (the spec asks for the choices to be surfaced and justified).
Sized for a heavy BATCH job on the shared zero-swap 16 GB host where the
HOST-AGGREGATE memory is the binding limit (not any per-unit cap).

Honest expectation (§11): early on the engine will generate huge candidate counts
with almost everything dying at the permutation null, slow accumulation in
``confirming``, and few/no promotions. That is correct behaviour — the null is
designed to kill the overwhelming majority. The metric that matters is whether
anything survives FORWARD confirmation and holds, not raw candidate counts. If
nothing promotes after weeks, that is a valid honest result (no durable edge in the
searched space) — NOT a reason to loosen any bar.
"""
from __future__ import annotations

from crypto.research.brain import config as brain_cfg
from crypto.research.brain import labels as brain_labels

# -- stores -------------------------------------------------------------------
#: The discovery layer's OWN mutable store: a separate SQLite-WAL DB so its (batch)
#: writer never contends with the substrate registry's tick-loop writer, and the
#: dashboard opens it read-only (WAL: readers never block the lone writer). A separate
#: FILE (not registry.sqlite) keeps the evolving discovery layer decoupled from the
#: stable substrate registry. Gitignored, under the brain's own writer domain.
DISCOVERY_DB_PATH = "data/research/brain/discovery.sqlite"
BRAIN_STORE_ROOT = brain_cfg.BRAIN_STORE_ROOT      # raw primitives live here
LABEL_STORE_ROOT = brain_cfg.BRAIN_STORE_ROOT      # forward-only labels live here
BRAIN_REGISTRY_PATH = brain_cfg.BRAIN_REGISTRY_PATH  # substrate cursors/bookkeeping (read)

#: 60s base grid == 1 window == 1 minute (the substrate cadence). Re-exported so the
#: discovery layer never hard-codes the grain.
WINDOW_NS = brain_cfg.BRAIN_BASE_CADENCE_NS

# -- §3 engineered (coin-relative) primitive layer ----------------------------
#: Per-coin z-score trailing window, in WINDOWS (60s grid). 1440 == 24 h: enough
#: samples for a stable z (std-error ~1/sqrt(1440)) and a full daily cycle (funding
#: epochs, session rotation), while a batch job tolerates the coarser responsiveness.
#: A LIST so the operator can add a shorter regime later; the default is one window.
ZSCORE_WINDOWS = (1440,)
#: Need at least this many PRIOR windows before emitting a z (else the feature is
#: absent for that window) — guards a degenerate z off a tiny sample.
ZSCORE_MIN_HISTORY = 60
#: Cross-universe rank needs at least this many coins present in the window.
XUNIV_MIN_COINS = 5

# -- §4 rule representation + threshold discretisation ------------------------
#: Quantile bins per engineered feature across the tape -> interior thresholds at the
#: 1/N .. (N-1)/N quantiles. Deciles: a fine-but-enumerable grid where every threshold
#: has equal support (balanced firing rates; no empty conditions).
QUANTILE_BINS = 10
#: SAFETY CEILING ONLY. §1 is explicit: depth is capped by the data (the null at each
#: depth), NOT by a constant. This is a runaway guard so an unbounded search cannot
#: spin forever; the null is what actually stops growth in practice.
MAX_DEPTH = 4
#: A candidate must fire on at least this many in-sample instances to be scorable
#: (below this the edge estimate is noise; it is neither passed nor counted).
MIN_FIRING_INSTANCES = 20

# -- §5 risk-adjusted excursion label binding ---------------------------------
#: The label horizon Stage-1 scores against (minutes == windows). 60 == 1 h: long
#: enough for a microstructure edge to express in MFE/MAE, short enough to accumulate
#: instances. Must be one of the materialised label horizons.
SCORE_HORIZON_MIN = 60
assert SCORE_HORIZON_MIN in brain_labels.HORIZONS_MIN
#: Side scored in Stage 1. The risk-adjusted excursion ``mfe + mae`` (mae<=0) is the
#: favourable excursion minus the adverse magnitude — "favourable beats adverse" (§5),
#: framed long. Short side is the symmetric negation (extension point, not Stage-1 default).
SCORE_SIDE = "long"

# -- §6.1 permutation null (the heaviest compute) -----------------------------
#: Permutations to characterise the null distribution AT EACH DEPTH. 200 resolves the
#: ~99th percentile of best-on-noise; the whole search is re-run this many times per
#: depth, so this is the dominant cost — SIZE IT AGAINST MEASURED HOST RUN-COST (§14).
N_PERMUTATIONS = 200
#: A real candidate at depth d must beat THIS quantile of the per-permutation
#: best-on-noise edge at depth d. 0.95 controls the search's ghost-generation rate at
#: each complexity; 1.0 (max) is the strictest bar (used by small-N tests).
NULL_QUANTILE = 0.95

# -- §6.2 forward confirmation ------------------------------------------------
#: M fresh POST-DISCOVERY instances required before a confirming rule can promote.
#: CONSERVATIVE DEFAULT, EXPLICITLY NOT FINAL (§6.2): the right value depends on
#: observed firing rates and accumulation speed, which cannot be calibrated in the
#: abstract. Surfaced operator-tunable (config + dashboard); the operator adjusts it
#: after watching live firing for a week or two. An INSTANCE count (not calendar time)
#: so rare and common rules are judged fairly.
CONFIRM_M = 30
#: The fresh-instance edge must be POSITIVE and distinguishable from zero past this
#: z (mean / (std/sqrt(n)) >= CONFIRM_Z) AND stay above the in-sample null bar.
CONFIRM_Z = 2.0

# -- §7 Stage-2 conditional exit discovery ------------------------------------
#: Excursion-level exits as MULTIPLES OF THE COIN'S VOLATILITY (not fixed %), so a
#: target/stop means the same thing across coins. Coin vol = the per-coin baseline
#: std of per-window returns (lookahead-free, trailing).
EXIT_FAVORABLE_VOL_MULTIPLES = (1.0, 1.5, 2.0, 3.0)
EXIT_ADVERSE_VOL_MULTIPLES = (0.5, 1.0, 1.5, 2.0)
#: Time-cap exits (max holding windows == minutes). Subset of the label horizons so a
#: round trip always resolves within a materialised label.
EXIT_TIME_CAPS_MIN = (5, 15, 30, 60)

# -- §9 batch cadence ---------------------------------------------------------
#: The discovery batch's own cadence (the systemd .timer; NOT the tick loop). 6 h
#: accumulates meaningful fresh instances between runs while keeping the heavy job's
#: host load modest. Tunable; size against measured run-cost (§14). The unit is wired
#: BUILT-NOT-DEPLOYED — enabling it is the operator's deploy, not this PR.
DISCOVERY_TIMER_ONCALENDAR = "*-*-* 00/6:30:00"   # 00:30, 06:30, 12:30, 18:30 UTC
