"""Canonical SHA256 of a strategy spec dict.

Byte-for-byte identical to /home/jpcg/crypto-trading-engine/engine/spec/
hash.py and to INTERFACE.md §2.3. Do NOT modify the canonicalization
without coordinating with the engine repo and updating INTERFACE.md.
"""
from __future__ import annotations

import hashlib
import json


def compute_spec_hash(spec_dict: dict) -> str:
    """Return ``sha256:<hex>`` over a deterministic byte stream of the
    spec, with the ``spec_hash`` field substituted by the empty string.

    The canonicalization is fixed: ``sort_keys=True``, compact
    separators ``(",", ":")``, default ``ensure_ascii=True``,
    ``utf-8`` encoding. See INTERFACE.md §2.3.
    """
    spec_copy = {**spec_dict, "spec_hash": ""}
    canonical = json.dumps(spec_copy, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
