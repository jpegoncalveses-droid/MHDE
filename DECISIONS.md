# Architecture Decision Records

Format: one record per major decision. Each record states the context,
the decision, and the consequence so future-you (or future Claude Code)
can re-evaluate without re-deriving the rationale.

---

## ADR-001 — Preserve legacy code rather than delete it

**Date:** 2026-05-07
**Session:** Session 0 of `HARDENING_PLAN.md`
**Status:** Active

**Context.** Roughly 100 .py files (across 5 whole directories and
~20 individual files) had no path from any ACTIVE entry point —
systemd unit, dashboard tab, or pipeline. They were imported only by
dormant CLI commands or by other dormant code.

**Decision.** Move them to `legacy/` rather than delete. Preserve git
history via `git mv`. Re-evaluate deletion after 2 weeks of stable
operation post-Session 7.

**Consequence.** A safety net: if a regression points at a missing
function, the code is recoverable in one `git mv` step. The repo gets
quieter without losing institutional memory. The trade-off is the
~4 MB on disk and the ongoing temptation to grep into `legacy/` instead
of treating it as opaque.

---

## ADR-002 — Retire the Flask catalyst-review server

**Date:** 2026-05-07
**Session:** Session 0
**Status:** Active

**Context.** `review/server.py` (~3900 lines) plus
`mhde-review-server.service` and `mhde-bridge-relay.service` (always-on
user-level units) served `https://mhde.duckdns.org/review/`. The
catalyst review UI it powered is no longer used in the workflow.

**Decision.** Move `review/` to `legacy/review/`. Disable both services
(`systemctl --user disable --now`). Remove the
`upstream mhde_review { ... }` block and the `location /review/`
proxy from `/home/jpcg/homeboard/nginx/nginx.conf`. Reload nginx.

**Consequence.** The `/review/` subpath now returns 502 (Streamlit
returns an error for the path because it falls through to
`location /` → Streamlit relay, and Streamlit's relay rejects the
unknown path). The architectural goal — no review server can serve
traffic — is achieved. A clean 404 would require an explicit
`location /review/ { return 404; }` block and is deferred. No
production code or data depends on the review server being reachable.

---

## ADR-003 — External FX data repo (`/home/jpcg/ATSRP/`) stays put

**Date:** 2026-05-07
**Session:** Session 0
**Status:** Active

**Context.** `HARDENING_PLAN.md` Session 0 deliverable 7 asked whether
to relocate `/home/jpcg/ATSRP/research/gbpeur_personal_fx/` (the old
FX research code) into MHDE. `INFRASTRUCTURE.md` confirms ATSRP is
**actively used**: `fx/data/refresh.py` shells out into ATSRP for
Dukascopy bi5 hourly bars, and `notifications/telegram.py` reads
`/home/jpcg/ATSRP/.env` for `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`.

**Decision.** Leave ATSRP exactly where it is. Do not relocate the
research code; do not duplicate the secrets. The plan's "Option B" —
keep ATSRP as a historical reference *and* an active dependency —
matches reality.

**Consequence.** ATSRP remains a hard dependency of the FX engine.
Anything that touches FX data refresh or Telegram credentials must
continue to reach ATSRP via subprocess / .env path. Document this in
INFRASTRUCTURE.md (already done) so it isn't surprising in future
sessions.

---

## ADR-004 — Drop the dead `outcomes.compute_forward_returns` re-export

**Date:** 2026-05-07
**Session:** Session 0
**Status:** Active

**Context.** `outcomes/__init__.py` re-exported
`compute_forward_returns` from `outcomes/labels.py`. A grep across the
codebase showed zero callers of `outcomes.compute_forward_returns`
outside of `__init__.py` itself. The active forward-return code lives
in `outcomes/tracker.py:update_forward_returns`, not in
`outcomes/labels.py`.

**Decision.** Move `outcomes/labels.py` to `legacy/outcomes/labels.py`
and delete the re-export line from `outcomes/__init__.py`. Same for
`outcomes/candidate_lifecycle.py` (only used by the legacy review
server).

**Consequence.** `outcomes/__init__.py` exposes only the symbols that
are actually consumed: `create_outcome_record`, `update_forward_returns`,
`get_pending_outcomes`, `update_review_status`. No behavior change for
any caller.

---

## ADR-005 — Plan deviates from the codebase; codebase wins

**Date:** 2026-05-07
**Session:** Session 0
**Status:** Active

**Context.** `HARDENING_PLAN.md` Session 0 listed several directories
as legacy (`features/`, `scoring/`, parts of `missed/`, the
`daily_radar` orchestration). An import-graph analysis showed those
items are still reachable from `mhde-daily-analysis.service` (which
runs `daily_radar` → `prediction-vs-actual` → `enrich-root-causes` →
`priority-refresh-queue` → `daily-catalyst-queue` Mon-Fri 23:15 UTC).

**Decision.** The codebase is the source of truth, not the plan. Files
that are imported by ACTIVE entry points stay at the active path;
everything else moves to `legacy/`. `legacy/README.md` documents the
specific corrections.

**Consequence.** The "30-50% file count reduction" target in the plan's
Session 0 exit criteria is not met as stated — the plan was wrong about
what was movable. Actual reduction is roughly 100 files out of ~250
project .py files (~40%, mostly concentrated in `models/`, `learning/`,
`governance/`, `backtest/`, `review/`, and the 19 `_legacy` dashboard
pages). Update HARDENING_PLAN.md before Session 1 to reflect the real
ACTIVE / LEGACY boundaries.
