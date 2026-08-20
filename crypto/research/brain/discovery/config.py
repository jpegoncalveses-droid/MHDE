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
#: Beam cap on the depth search: at each depth only the top-K passers by edge are RETAINED
#: (returned + persisted) and EXTENDED to the next depth. Measured basis
#: (data/processed/stage1_breadth_cap_measurement.md, 2026-08-10): the permutation null goes
#: PERMEABLE on selection-conditioned extensions at depth>=3 (45% pass at full universe,
#: 46-54% on the 300-symbol proxy), flooding the pass set with a flat redundant tail — the
#: top 1% of depth-4 survivors carry only ~2% of total lift. Adaptive beam-coverage on the
#: TRUE-SCALE designed-depth run (full universe, MAX_DEPTH=4, funnel 3 -> 4 -> 772 ->
#: 31,760): K=500 keeps 99.3% of ALL depth-4 survivors — 100% of the top-100, 99.9% of the
#: top-1k, 99.6% of the top-10k, 99.3% of total depth-4 lift mass (90% of depth-4 lineage
#: flows through the top 483 depth-3 parents). The loss is confined to the redundant tail.
#: Bounds the extension pool (<= K x n_atoms candidates) and the retained set (<= K per
#: depth). ``None`` disables (unbounded — tests/analysis only).
BEAM_WIDTH = 500
#: Stage-2 exit discovery samples at most this many fired instances per rule. Basis
#: (data/processed/stage1_breadth_cap_measurement.md §12): the unbounded per-rule
#: continuation build lifted the full pass from its ~9.5G load steady-state to
#: ~11.3-11.6G — into the host OOM kill zone that ended six gate attempts (every kill
#: host-level, the unit always under its own 13G cap). Deep rules fire fewer than N and
#: are UNTOUCHED (byte-identical continuations); only broad shallow rules sample. The
#: sample is DETERMINISTIC per rule (seeded from the rule's canonical_id, attempt-stable
#: — a re-run discovers the same exit). Bounds the stage-2 increment to ~0.3-0.5G
#: (run peak ~10G). ``None`` disables (unbounded — tests/analysis only).
EXIT_DISCOVERY_MAX_INSTANCES = 5000

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

# -- bounded substrate window (memory scaling) --------------------------------
#: A pass reads only the last N days of labels, NOT the whole (unbounded, growing) store — the
#: fix for the whole-store OOM (~22G materialized-as-dicts vs a 15G box). N must give the in-sample
#: search enough valid horizon-60 instances for the null (>> MIN_FIRING_INSTANCES) AND leave the
#: trailing post-frontier days for even floor-rate rules to reach CONFIRM_M fresh instances before
#: they scroll out of the window. 14d is a conservative starting point (a single day already powers
#: the depth-3 null); lower it once the real-substrate peak-RSS is profiled. Operator-tunable.
DISCOVERY_HISTORY_DAYS = 14
DISCOVERY_HISTORY_NS = DISCOVERY_HISTORY_DAYS * 86_400 * 1_000_000_000
#: Primitives are read back an EXTRA lookback beyond the label window so the oldest SCORED
#: instances get full-fidelity trailing features (the 1440-window / 24h z-score + its 60-window
#: min history) instead of a cold start. 2d covers 1440+60 windows with margin. These extra days
#: are UNLABELED lookback (they feed z-score + threshold population), not extra scored instances.
DISCOVERY_PRIMITIVE_LOOKBACK_DAYS = 2
DISCOVERY_PRIMITIVE_LOOKBACK_NS = DISCOVERY_PRIMITIVE_LOOKBACK_DAYS * 86_400 * 1_000_000_000

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
#: Demotion hysteresis (operator decision 2026-08-14): a promoted rule demotes only when
#: its fresh recount falls below ``CONFIRM_M - CONFIRM_DEMOTE_HYSTERESIS``; the band
#: [M-5, M) is a deliberate HOLD zone — no demotion, no decay judgment (decay needs a
#: full n >= M sample), no promotion. Basis: recounts are non-monotonic and 66 of the 75
#: demotion-eligible promoted rules sat in [M-5, M) at first application — recount
#: jitter, not evidence loss — so a symmetric threshold flaps ~88% of them every pass
#: (PROMOTED<->CONFIRMING churn + trade-log discontinuities). The bounded quiet band is
#: the accepted trade (ADR-041). Operator-tunable.
CONFIRM_DEMOTE_HYSTERESIS = 5

# -- pace-collapse expiry (ADR-042, 2026-08-15; redesigned after review) -------
#: A never-promoted confirming rule is expired once resolution is IMPLAUSIBLE at its own
#: pace. Opportunity O = n_fires x min(elapsed, DISCOVERY_HISTORY)/DISCOVERY_HISTORY is
#: WINDOW-CAPPED so it compares like with like (fresh_count is a rolling 14d count): for
#: mature rules the criterion is the pure forward/in-sample rate-collapse ratio
#: fresh x 6 < n_fires — time-free; a rate-stable rule is held forever. Expire iff
#: promoted_at_ns IS NULL (once-promoted NEVER pace-expires — demotee protection is
#: structural) AND O >= CONFIRM_M AND fresh < M-H AND fresh x EXPIRE_PACE_FACTOR < O.
#: Honesty: this prunes rate-collapsed never-resolvers only; mechanism 1 (shared-compute
#: confirmation) carries the saturation fix. Operator-tunable.
EXPIRE_PACE_FACTOR = 6

#: KI-166 incident hold (F4): expiry runs only once the frontier has passed this value.
#: After the 08-15->08-20 tape-loss gap, ``frontier - discovery_window`` OVERCOUNTS
#: labeled exposure for pre-gap mints until the 5-day hole rolls out of the 14-day
#: window — judging pace against overcounted opportunity would re-manufacture
#: artifacts. 2026-09-03 00:00 UTC = gap end (2026-08-20) + DISCOVERY_HISTORY (14d).
#: Set to 0 to disable the hold (the steady-state value once removed). Operator-tunable.
EXPIRE_RESUME_FRONTIER_NS = 1_788_393_600_000_000_000

# -- Option 2 family-level admission (ADR-043) ---------------------------------
#: Max CONCURRENT confirming seats per family (a seat = a confirming row carrying
#: n_fires_at_admission). Three cohort-distinct seats let the graduation bar's
#: "3+ cohorts" accrue in PARALLEL while bounding the standing set to
#: ~(active families x k). k=1 degenerates to the sticky-seat bound (serialized
#: bar); the resolvability floor is CONFIRM_M itself (no separate constant).
#: Operator-tunable. Design: data/processed/family_admission_design.md.
ADMIT_MAX_PER_FAMILY = 3

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
