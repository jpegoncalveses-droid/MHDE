# MHDE Decision Log

## 2026-05-01 — v1 engine build

Built the full MHDE engine surface area: universe construction, ingestion, feature engineering,
scoring, hypotheses, LLM briefs, notifications, candidate outcome tracking, backtesting,
XGBoost smoke, governance, pipelines, and Streamlit dashboard.

**Key decisions:**

- Paper trading was explicitly excluded. MHDE is a discovery and evidence system, not a trading
  simulator. Candidate outcome tracking (`candidate_outcomes` table) replaces paper trading as
  the evaluation mechanism.

- Universe selection is name-filtered only (SEC company_tickers.json). No market cap, liquidity,
  or price filter is applied at universe construction time. This is a known v1 limitation.

- All external credentials are optional. Every ingestor, LLM provider, and notification channel
  degrades gracefully to stub/mock/no-op when credentials are absent.

- DuckDB is the storage layer. File-based, no server. All engine data persists at `data/mhde.duckdb`.

- XGBoost model is quarantined: experimental only, not used for alerts or rankings.

- Dashboard authentication is enabled by default for VPS deployment.

## 2026-05-01 — Predictive philosophy clarified

**Decision:** MHDE is a current-evidence hypothesis discovery engine, not a historical
pattern-matching engine.

**Rationale:** MHDE must be predictive, but prediction is grounded in observable signals
as of today — filings, fundamentals, prices, short interest, events. It does not back-fit
scoring rules to historical price returns. Historical outcomes (candidate_outcomes) are used
to evaluate whether the current-evidence logic produces useful hypotheses, not to derive the
logic itself.

**Impact:** Updated `docs/learning_loop.md`, `docs/scorecard_v1.md`, `docs/model_governance.md`,
and `docs/known_limitations.md` to state this explicitly. Governance now requires that any
model trained on historical returns must be validated for current-evidence generalization before
influencing production outputs.

## 2026-05-01 — Learning loop added

Added the MHDE learning loop: candidate review table, scorecard experiment table, structured
error taxonomy, calibration analytics, insights engine, and dashboard learning page.

**Key decisions:**

- Learning is driven by **human review quality signals** (`candidate_reviews`), not only forward
  returns. The two questions are: "did the stock go up?" AND "was the hypothesis good?"

- MHDE does **not** automatically apply scorecard changes. All experiments require human
  approval (`approved_by` field) before being applied.

- A structured false-positive taxonomy (15 reason codes) replaces free-form notes as the
  primary failure classification signal.

- Scorecard experiments may be proposed automatically by `learning/insights.py` but are never
  applied without human approval.

- `python main.py learn summarize` generates a calibration report from all accumulated outcome
  and review data.

- Dashboard page 13 (`13_learning_calibration.py`) provides a review submission form and
  calibration visualizations.
