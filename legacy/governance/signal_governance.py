"""Production scoring governance — propose, approve, and rollback signals.

All operations are recorded in an append-only JSONL audit log. No automatic
promotion: approval requires an explicit config change to enable the corresponding
feature flag in config/settings.yaml.
"""
from __future__ import annotations

import datetime
import json
import uuid
from enum import Enum
from typing import Optional


class ProposalStatus(Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


def _utc_now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _append_entry(audit_path: str, entry: dict) -> None:
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def load_audit_log(audit_path: str) -> list[dict]:
    """Load all entries from the audit log. Returns empty list if file absent."""
    try:
        with open(audit_path, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except FileNotFoundError:
        return []


def create_proposal(
    signal_name: str,
    evidence_period: str,
    sample_size: int,
    precision: float,
    recall: float,
    avg_return: float,
    rollback_criteria: str,
    audit_path: str,
    actor: str,
) -> str:
    """Record a new signal promotion proposal. Returns proposal_id."""
    proposal_id = str(uuid.uuid4())[:8]
    entry = {
        "proposal_id": proposal_id,
        "signal_name": signal_name,
        "evidence_period": evidence_period,
        "sample_size": sample_size,
        "precision": precision,
        "recall": recall,
        "avg_return": avg_return,
        "rollback_criteria": rollback_criteria,
        "status": ProposalStatus.PROPOSED.value,
        "actor": actor,
        "timestamp": _utc_now(),
    }
    _append_entry(audit_path, entry)
    return proposal_id


def approve_proposal(proposal_id: str, actor: str, audit_path: str) -> None:
    """Record approval of a proposal. Caller must still enable the feature flag in config."""
    _append_entry(audit_path, {
        "proposal_id": proposal_id,
        "status": ProposalStatus.APPROVED.value,
        "actor": actor,
        "timestamp": _utc_now(),
        "note": (
            "Approved. To activate: set the corresponding flag to true "
            "in config/settings.yaml under feature_flags."
        ),
    })


def rollback_proposal(proposal_id: str, reason: str, actor: str, audit_path: str) -> None:
    """Record rollback of an approved proposal."""
    _append_entry(audit_path, {
        "proposal_id": proposal_id,
        "status": ProposalStatus.ROLLED_BACK.value,
        "reason": reason,
        "actor": actor,
        "timestamp": _utc_now(),
        "note": "Rollback recorded. Disable the corresponding feature flag in config/settings.yaml.",
    })
