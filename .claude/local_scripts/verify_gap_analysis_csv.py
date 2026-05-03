import csv
import sys

path = "data/processed/mhde_gap_analysis.csv"
required_cols = {
    "question_id", "question", "capability_status",
    "current_support", "gaps", "recommended_source", "priority"
}

with open(path, newline="") as f:
    reader = csv.DictReader(f)
    rows = list(reader)
    fieldnames = set(reader.fieldnames or [])

missing_cols = required_cols - fieldnames
if missing_cols:
    print(f"FAIL: Missing columns: {missing_cols}")
    sys.exit(1)

if len(rows) < 10:
    print(f"FAIL: Too few rows ({len(rows)}), expected >=10")
    sys.exit(1)

valid_statuses = {"SUPPORTED", "PARTIAL", "MISSING", "STUB", "—"}
bad = [r["question_id"] for r in rows if r["capability_status"] not in valid_statuses]
if bad:
    print(f"FAIL: Invalid capability_status in: {bad}")
    sys.exit(1)

print(f"OK: {len(rows)} rows, {len(fieldnames)} columns")
for r in rows:
    print(f"  {r['question_id']:8} [{r['capability_status']:10}] {r['question'][:55]}")
