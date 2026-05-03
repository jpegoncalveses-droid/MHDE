# MHDE Full System Completion Status

Generated: 2026-05-03 (Phase 0 Preflight)

---

## Git State

**Branch:** master

**Last 10 commits:**

```
2b66db2 feat: add prediction-vs-actual learning summary to dashboard, digest, and review server
e4e3014 feat: add learning predictions dashboard page (17)
46629f2 feat: add learning metrics row to dashboard homepage
1d96316 feat: add learning section to catalyst digest txt and html
106a9be feat: add /learning page and artifact routes to Flask review server
c77f712 feat: add get_learning_stats service reading prediction/enriched CSVs
5a9172f feat: embed root cause summary in prediction-vs-actual report
c8af4df feat: add missed enrich-root-causes CLI command
3be4124 test: complete root-cause classification test coverage
b47bbfc fix: address date handling and type annotation issues in root_cause_enrichment
```

---

## Test Baseline

Run command: `venv/bin/python -m pytest tests/ -q --tb=short`

| Metric  | Count |
|---------|-------|
| Passed  | 904   |
| Failed  | 0     |
| Skipped | 0     |
| Warnings| 689   |

**Result: CLEAN — 904 passed, 0 failed, 0 skipped (69.69s)**

All warnings are `DeprecationWarning: datetime.datetime.utcnow()` — no functional failures.

---

## Live Services Status

### Review Server (port 8765)

- **systemd unit:** NOT found (`mhde-review-server.service` does not exist)
- **Process:** Running as ad-hoc process (PID 705812)
  - Command: `venv/bin/python main.py missed review-server --host 127.0.0.1 --port 8765 --history-root data/processed/catalyst_queue_history --output-dir data/processed`
  - Started: 2026-05-03 16:26
- **HTTP / (root):** HTTP 401 (auth-gated, server is alive)
- **HTTP /health:** HTTP 404 (health route not implemented)

**Verdict:** Server is live and responding. Not managed by systemd — started manually/interactively.

---

## Key Artifacts

| Artifact | Status | Size | Last Modified |
|---|---|---|---|
| `data/processed/prediction_vs_actual_rows.csv` | EXISTS | 148K | 2026-05-03 16:26 |
| `data/processed/prediction_vs_actual_enriched_rows.csv` | EXISTS | 407K | 2026-05-03 16:27 |
| `data/processed/root_cause_enrichment_report.md` | EXISTS | 8.1K | 2026-05-03 16:27 |
| `data/processed/daily_catalyst_queue.csv` | EXISTS | 29K | 2026-05-03 07:37 |
| `data/processed/catalyst_queue_history/` | EXISTS | — | — |

**catalyst_queue_history last 3 entries:**
- `history_summary.md` (707 bytes, 2026-05-02 20:45)
- `2026-05-02/` (dir, 2026-05-02 21:43)
- `2026-05-03/` (dir, 2026-05-03 07:37)

**All key artifacts present.**

---

## Phase Checklist

| Phase | Name | Status |
|---|---|---|
| 0 | Preflight status and baseline docs | In Progress |
| 1 | Dashboard control center — /today, /candidates, /moves, /ops routes | Not Started |
| 2 | Daily automation script and systemd timer | Not Started |
| 3 | Universe modes and Polygon ticker-details enrichment | Not Started |
| 4 | Incomplete score diagnostics report | Not Started |
| 5 | Deterministic catalyst classification rules | Not Started |
| 6 | Sector ETF ingestion and sympathy attribution | Not Started |
| 7 | Earnings estimates and surprises | Not Started |
| 8 | GDELT news ingestion and catalyst classifiers | Not Started |
| 9 | Move episode lifecycle tracking | Not Started |
| 10 | Auto-populate forward returns daily | Not Started |
| 11 | Feature flag registry | Not Started |
| 12 | Production scoring governance CLI | Not Started |
| 13 | Operating manual and architecture docs | Not Started |

---

## Known Dirty Files (Untracked)

These files exist locally but are not tracked by git:

| Path | Notes |
|---|---|
| `.claude/CLAUDE.md` | Claude Code project instructions |
| `.claude/local_scripts/audit_mhde_status.py` | Local audit script |
| `.claude/settings.json` | Claude Code settings |
| `data/processed/mhde_phase_status.csv` | Phase status data (do NOT commit) |
| `data/processed/missed_spike_investigations.jsonl` | Processed artifact (do NOT commit) |
| `data/processed/prediction_vs_actual_enriched_rows.csv` | Processed artifact (do NOT commit) |
| `data/processed/prediction_vs_actual_report.md` | Processed artifact (do NOT commit) |
| `data/processed/prediction_vs_actual_rows.csv` | Processed artifact (do NOT commit) |
| `data/processed/root_cause_enrichment_report.md` | Processed artifact (do NOT commit) |
| `docs/mhde_status_and_missing_phases.md` | Earlier status doc (untracked) |
| `docs/superpowers/plans/2026-05-03-dashboard-learning-integration.md` | Plan doc (untracked) |
| `docs/superpowers/plans/2026-05-03-deterministic-root-cause-enrichment.md` | Plan doc (untracked) |
| `docs/superpowers/plans/2026-05-03-prediction-vs-actual-report.md` | Plan doc (untracked) |

**Note:** `data/processed/` files must NOT be committed. `.claude/` files are local tooling.
