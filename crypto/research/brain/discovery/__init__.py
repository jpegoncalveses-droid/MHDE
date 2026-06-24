"""Brain discovery engine (the discovery/evaluation layer over the substrate).

The substrate (BUILT, in the parent ``brain`` package) turns the raw capture tape
into per-(symbol, window) raw primitives (``pipeline.py`` + ``bucket_*``) and a
forward-only label store (``labels.py``: forward return / MFE / MAE / valid, settled
only once resolved). This package builds the discovery half (ABSENT until now):

  * ``engineered``   — §3 coin-relative primitive layer (per-coin z, cross-universe rank).
  * ``rules``        — §4 conjunction rule representation + depth-extensible generation.
  * ``scoring``      — §5 risk-adjusted excursion label binding + §6.1 permutation null.
  * ``rulestore``    — §8.1 mutable rule store + state machine (SQLite-WAL).
  * ``confirmation`` — §6.2 forward confirmation on post-discovery instances only.
  * ``exits``        — §7 Stage-2 conditional exit discovery (round-trip sim + null + fwd).
  * ``tradelog``     — §8.2 simulated round-trip log for promoted rules.
  * ``runner``       — §9 batch orchestration (its own cadence, NOT the tick loop).

CORE PRINCIPLE (§1): depth is not capped by a constant; it is capped by the data's
ability to distinguish a rule from a random rule of the SAME depth on the SAME tape,
measured by the permutation null at each depth. Promotion requires BOTH the entry and
the exit to independently beat the null AND re-confirm on fresh forward tape (§2).

Nothing here wires to a real executor: promotion logs simulated round trips only; the
brain<->executor loop stays open by design (§8.3).
"""
