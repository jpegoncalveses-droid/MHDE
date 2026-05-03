import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from storage.db import get_connection
from storage.migrations import run_migrations

conn = get_connection("data/mhde.duckdb")
run_migrations(conn)

r = conn.execute(
    "SELECT COUNT(*), COUNT(DISTINCT sector), COUNT(DISTINCT industry), "
    "SUM(CASE WHEN market_cap IS NULL THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN sector IS NULL THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN universe_tier = 'primary' THEN 1 ELSE 0 END) "
    "FROM companies WHERE is_active=true"
).fetchone()

print(f"Active companies:    {r[0]}")
print(f"Distinct sectors:    {r[1]}")
print(f"Distinct industries: {r[2]}")
print(f"NULL market_cap:     {r[3]}")
print(f"NULL sector:         {r[4]}")
print(f"Primary tier:        {r[5]}")

top = conn.execute(
    "SELECT ticker, company_name, universe_tier, market_cap, sector "
    "FROM companies WHERE is_active=true ORDER BY ticker LIMIT 20"
).fetchall()

print("\nSample (first 20 active, sorted by ticker):")
for row in top:
    name = (row[1] or "")[:30]
    tier = row[2] or "-"
    print(f"  {row[0]:8} {name:30} tier={tier:8} mkt={row[3]} sector={row[4]}")
conn.close()
