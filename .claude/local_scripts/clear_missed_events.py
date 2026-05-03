"""Clear missed-opportunity events and investigations for a fresh clustered re-run."""
from storage.db import get_connection

conn = get_connection()
conn.execute("DELETE FROM missed_opportunity_investigations")
conn.execute("DELETE FROM missed_opportunity_events")
n = conn.execute("SELECT COUNT(*) FROM missed_opportunity_events").fetchone()[0]
print(f"Events remaining: {n} (should be 0)")
conn.close()
