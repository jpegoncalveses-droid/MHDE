#!/usr/bin/env python
"""MHDE status audit — gather facts for the phase-status document."""
import duckdb

conn = duckdb.connect("data/mhde.duckdb", read_only=True)

print("=== UNIVERSE ===")
r = conn.execute("""
    SELECT universe_tier, COUNT(*) AS n, SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active
    FROM companies GROUP BY universe_tier ORDER BY universe_tier
""").fetchall()
for x in r:
    print(f"  {x}")

print("\n=== SECTORS (primary tickers) ===")
r = conn.execute("""
    SELECT sector, COUNT(*) FROM companies WHERE universe_tier='primary' GROUP BY sector ORDER BY 2 DESC
""").fetchall()
for x in r:
    print(f"  {x}")

print("\n=== SCORE TIERS (latest run) ===")
r = conn.execute("""
    SELECT tier, COUNT(*) FROM scores
    WHERE as_of_date = (SELECT MAX(as_of_date) FROM scores)
    GROUP BY tier ORDER BY 2 DESC
""").fetchall()
for x in r:
    print(f"  {x}")

print("\n=== CANDIDATE_OUTCOMES ===")
r = conn.execute("""
    SELECT COUNT(*), MIN(as_of_date), MAX(as_of_date),
           SUM(CASE WHEN forward_return_1d IS NOT NULL THEN 1 ELSE 0 END) AS has_1d,
           SUM(CASE WHEN forward_return_5d IS NOT NULL THEN 1 ELSE 0 END) AS has_5d,
           SUM(CASE WHEN forward_return_20d IS NOT NULL THEN 1 ELSE 0 END) AS has_20d
    FROM candidate_outcomes
""").fetchone()
print(f"  total={r[0]}, from={r[1]}, to={r[2]}, has_1d={r[3]}, has_5d={r[4]}, has_20d={r[5]}")

print("\n=== MISSED EVENTS TOTAL ===")
r = conn.execute("""
    SELECT COUNT(*), MIN(event_date), MAX(event_date),
           SUM(CASE WHEN was_scored THEN 1 ELSE 0 END) AS was_scored
    FROM missed_opportunity_events
""").fetchone()
print(f"  total={r[0]}, from={r[1]}, to={r[2]}, was_scored={r[3]}")

print("\n=== MISSED EVENTS BY EVENT TYPE ===")
r = conn.execute("""
    SELECT event_type, COUNT(*) FROM missed_opportunity_events GROUP BY event_type ORDER BY 2 DESC
""").fetchall()
for x in r:
    print(f"  {x}")

print("\n=== DIS / PYPL / PLTR / TEAM events ===")
for t in ["DIS", "PYPL", "PLTR", "TEAM"]:
    r = conn.execute("""
        SELECT COUNT(*), MIN(event_date), MAX(event_date)
        FROM missed_opportunity_events WHERE ticker=?
    """, [t]).fetchone()
    print(f"  {t}: count={r[0]}, from={r[1]}, to={r[2]}")

print("\n=== SCORES: FINRA / SHORT INTEREST ===")
r = conn.execute("SELECT COUNT(*) FROM short_interest").fetchone()
print(f"  short_interest rows: {r[0]}")

print("\n=== FILINGS COVERAGE ===")
r = conn.execute("""
    SELECT COUNT(DISTINCT ticker), COUNT(*), MIN(filing_date), MAX(filing_date)
    FROM filings
""").fetchone()
print(f"  tickers={r[0]}, total_filings={r[1]}, from={r[2]}, to={r[3]}")

print("\n=== INVESTIGATIONS ===")
r = conn.execute("""
    SELECT primary_root_cause, COUNT(*) FROM missed_opportunity_investigations
    GROUP BY primary_root_cause ORDER BY 2 DESC
""").fetchall()
if r:
    for x in r:
        print(f"  {x}")
else:
    print("  (no investigations yet)")

print("\n=== EVENTS TABLE (earnings calendar) ===")
r = conn.execute("""
    SELECT event_type, COUNT(*) FROM events GROUP BY event_type ORDER BY 2 DESC LIMIT 5
""").fetchall()
for x in r:
    print(f"  {x}")

print("\n=== SCORECARD EXPERIMENTS ===")
r = conn.execute("SELECT status, COUNT(*) FROM scorecard_experiments GROUP BY status").fetchall()
if r:
    for x in r:
        print(f"  {x}")
else:
    print("  (none)")

print("\n=== CANDIDATE REVIEWS ===")
r = conn.execute("""
    SELECT review_status, COUNT(*) FROM candidate_reviews GROUP BY review_status ORDER BY 2 DESC
""").fetchall()
for x in r:
    print(f"  {x}")

print("\n=== PRICES DAILY ===")
r = conn.execute("""
    SELECT COUNT(DISTINCT ticker), MIN(trade_date), MAX(trade_date), COUNT(*)
    FROM prices_daily
""").fetchone()
print(f"  tickers={r[0]}, from={r[1]}, to={r[2]}, total_rows={r[3]}")

conn.close()
print("\nDone.")
