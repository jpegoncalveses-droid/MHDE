import duckdb

conn = duckdb.connect(":memory:")
conn.execute("CREATE TABLE t (id INTEGER, x VARCHAR)")

# Test VARCHAR column already exists
try:
    conn.execute("ALTER TABLE t ADD COLUMN x VARCHAR")
except Exception as e:
    print(f"VARCHAR duplicate caught: {e}")

try:
    result = conn.execute("SELECT * FROM t").fetchall()
    print(f"Query after failed VARCHAR ALTER works: {result}")
except Exception as e:
    print(f"Query after failed VARCHAR ALTER FAILED: {e}")

# Test BOOLEAN DEFAULT column already exists
conn2 = duckdb.connect(":memory:")
conn2.execute("CREATE TABLE u (id INTEGER, flag BOOLEAN DEFAULT true)")
try:
    conn2.execute("ALTER TABLE u ADD COLUMN flag BOOLEAN DEFAULT true")
except Exception as e:
    print(f"BOOLEAN DEFAULT duplicate caught: {e}")

try:
    result2 = conn2.execute("SELECT * FROM u").fetchall()
    print(f"Query after failed BOOLEAN ALTER works: {result2}")
except Exception as e:
    print(f"Query after failed BOOLEAN ALTER FAILED: {e}")

conn.close()
conn2.close()
