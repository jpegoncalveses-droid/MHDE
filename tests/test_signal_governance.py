"""Tests for production scoring governance."""
import json
import os

import pytest

from governance.signal_governance import (
    ProposalStatus,
    approve_proposal,
    create_proposal,
    load_audit_log,
    rollback_proposal,
)


def test_create_proposal_returns_id(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    pid = create_proposal(
        signal_name="earnings_surprise_boost",
        evidence_period="2026-01-01 to 2026-03-31",
        sample_size=120,
        precision=0.72,
        recall=0.58,
        avg_return=0.095,
        rollback_criteria="precision < 0.60 over 30 days",
        audit_path=audit_path,
        actor="jpcg",
    )
    assert pid is not None
    assert len(pid) > 0


def test_create_proposal_writes_to_log(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    pid = create_proposal(
        signal_name="test_signal",
        evidence_period="2026-01",
        sample_size=50,
        precision=0.7,
        recall=0.5,
        avg_return=0.08,
        rollback_criteria="criteria",
        audit_path=audit_path,
        actor="jpcg",
    )
    log = load_audit_log(audit_path)
    assert len(log) == 1
    assert log[0]["proposal_id"] == pid
    assert log[0]["status"] == ProposalStatus.PROPOSED.value
    assert log[0]["signal_name"] == "test_signal"


def test_approve_writes_to_log(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    pid = create_proposal("s", "2026", 10, 0.7, 0.5, 0.08, "c", audit_path, "jpcg")
    approve_proposal(pid, actor="jpcg", audit_path=audit_path)
    log = load_audit_log(audit_path)
    approved = [e for e in log if e.get("status") == ProposalStatus.APPROVED.value]
    assert len(approved) == 1
    assert approved[0]["proposal_id"] == pid


def test_rollback_writes_to_log(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    pid = create_proposal("s", "2026", 10, 0.7, 0.5, 0.08, "c", audit_path, "jpcg")
    rollback_proposal(pid, reason="precision dropped", actor="jpcg", audit_path=audit_path)
    log = load_audit_log(audit_path)
    rolled = [e for e in log if e.get("status") == ProposalStatus.ROLLED_BACK.value]
    assert len(rolled) == 1
    assert rolled[0]["reason"] == "precision dropped"


def test_load_audit_log_returns_empty_if_missing(tmp_path):
    path = str(tmp_path / "nonexistent.jsonl")
    log = load_audit_log(path)
    assert log == []


def test_audit_log_is_append_only(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    pid1 = create_proposal("s1", "2026", 10, 0.7, 0.5, 0.08, "c", audit_path, "a")
    pid2 = create_proposal("s2", "2026", 20, 0.8, 0.6, 0.10, "c", audit_path, "a")
    log = load_audit_log(audit_path)
    assert len(log) == 2
    ids = {e["proposal_id"] for e in log}
    assert pid1 in ids and pid2 in ids


def test_proposal_status_enum_values():
    assert ProposalStatus.PROPOSED.value == "proposed"
    assert ProposalStatus.APPROVED.value == "approved"
    assert ProposalStatus.ROLLED_BACK.value == "rolled_back"
    assert ProposalStatus.REJECTED.value == "rejected"


def test_approve_note_mentions_feature_flag(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    pid = create_proposal("s", "2026", 10, 0.7, 0.5, 0.08, "c", audit_path, "a")
    approve_proposal(pid, actor="a", audit_path=audit_path)
    log = load_audit_log(audit_path)
    approved = [e for e in log if e.get("status") == "approved"][0]
    assert "feature" in approved.get("note", "").lower() or "flag" in approved.get("note", "").lower()
