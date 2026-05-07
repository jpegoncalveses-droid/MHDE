"""Schema-side regressions: every CREATE TABLE has at least one
reader and writer in active code; the nginx /review/ block is in
place; trained-model artifact path exists.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# Shared helpers ---------------------------------------------------------------


def _grep_repo(pattern: str, exclude_dirs: tuple[str, ...] = ("legacy", ".venv", "venv",
                                                                "__pycache__", ".git",
                                                                ".claude", "tests")) -> list[str]:
    """Use `git grep` for speed; only matches files git tracks."""
    cmd = ["git", "grep", "-l", pattern]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
    if out.returncode > 1:  # 1 = no match, >1 = error
        return []
    files = [f for f in out.stdout.splitlines() if f]
    return [f for f in files
            if not any(d in Path(f).parts for d in exclude_dirs)]


def _create_table_names(text: str) -> list[str]:
    return re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", text)


# KI-117: models/saved/ exists --------------------------------------------------


def test_models_saved_path_exists():
    """The four hardcoded `"models/saved"` references in active code
    expect this directory to exist on disk. KI-117."""
    p = REPO / "models" / "saved"
    assert p.is_dir(), f"{p} missing — would break ml/train.py:26 et al"


# KI-001: nginx /review/ → 404 block in conf -----------------------------------


@pytest.mark.skipif(
    not Path("/home/jpcg/homeboard/nginx/nginx.conf").exists(),
    reason="not on the deployment host",
)
def test_nginx_review_returns_404():
    """The nginx config that nginx serves must contain an explicit
    `location /review/ { return 404; }` block. KI-001.

    Note: edits to this file need a `docker compose restart nginx` to
    propagate (single-file bind mount inode trap — see KI-001 lesson).
    """
    conf = Path("/home/jpcg/homeboard/nginx/nginx.conf").read_text()
    assert "location /review/" in conf
    # The block must contain `return 404`
    block_match = re.search(r"location /review/\s*\{[^}]*\}", conf, re.DOTALL)
    assert block_match, "/review/ location block malformed"
    assert "return 404" in block_match.group(0), (
        "/review/ location block must `return 404` (KI-001)"
    )


# Schema migration: every table has reader + writer in active code -------------


def test_every_engine_table_has_reader_and_writer():
    """For each CREATE TABLE in {ml,crypto,fx}/schema.py, at least one
    file in the active tree references it (read or write). Catches
    "we created a table but never read it"-style drift.
    """
    schema_files = [
        REPO / "ml" / "schema.py",
        REPO / "crypto" / "schema.py",
        REPO / "fx" / "schema.py",
    ]
    tables: list[str] = []
    for sf in schema_files:
        tables.extend(_create_table_names(sf.read_text()))
    assert tables, "no tables found in engine schemas — parse broken?"

    orphans: list[str] = []
    for tbl in tables:
        files = _grep_repo(rf"\b{tbl}\b")
        # Filter to active code only (test exclusion via _grep_repo).
        active = [f for f in files if not f.endswith("schema.py")
                  and not f.startswith("docs/")]
        if not active:
            orphans.append(tbl)
    assert not orphans, (
        f"Tables defined in schema sources but not referenced in active "
        f"code: {orphans}. Either drop the table or wire it up."
    )


def test_storage_schema_sql_tables_referenced():
    """Same check for storage/schema.sql (the legacy equity tables)."""
    sql = (REPO / "storage" / "schema.sql").read_text()
    tables = _create_table_names(sql)
    assert tables

    orphans: list[str] = []
    # Some legacy tables (`scorecard_experiments`, `promotion_gate_results`)
    # are dormant per legacy/README.md — exclude from the orphan check.
    DORMANT = {"scorecard_experiments", "promotion_gate_results"}
    for tbl in tables:
        if tbl in DORMANT:
            continue
        files = _grep_repo(rf"\b{tbl}\b")
        active = [f for f in files if not f.endswith("schema.sql")]
        if not active:
            orphans.append(tbl)
    assert not orphans, (
        f"storage/schema.sql tables not referenced in active code: {orphans}"
    )
