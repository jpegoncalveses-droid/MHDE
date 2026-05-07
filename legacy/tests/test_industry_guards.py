"""Industry-specific financial concept guards — experiment 5c873dcb5ac24a80."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from storage.db import get_connection, init_schema
from features.valuation import compute_valuation
from features.quality import compute_quality


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(c)
    yield c
    c.close()


def _company(conn, ticker, name):
    conn.execute(
        "INSERT INTO companies (ticker, company_name) VALUES (?, ?) ON CONFLICT DO NOTHING",
        [ticker, name],
    )


def _price(conn, ticker, close):
    conn.execute(
        "INSERT INTO prices_daily (id, ticker, trade_date, close, source) VALUES (?,?,?,?,'test')",
        [uuid.uuid4().hex[:16], ticker, date.today().isoformat(), close],
    )


def _fund(conn, ticker, concept, value, unit="USD", dt=None):
    conn.execute(
        "INSERT INTO fundamentals_raw (id, ticker, concept, value, unit, as_of_date) VALUES (?,?,?,?,?,?)",
        [uuid.uuid4().hex[:16], ticker, concept, value, unit,
         (dt or date.today()).isoformat()],
    )


def _feat(features, name):
    return next((f for f in features if f["feature_name"] == name), None)


# ── Industry detection ─────────────────────────────────────────────────────────

def test_bank_company_name_detected(conn):
    """Company with 'BANCORP' in name → is_bank=True."""
    from features.industry_utils import detect_industry
    _company(conn, "BANKX", "CITIZENS BANCORP INC")
    result = detect_industry(conn, "BANKX")
    assert result["is_bank"] is True
    assert result["is_insurer"] is False


def test_bank_xbrl_concepts_detected(conn):
    """Company with bank XBRL (NetInterestIncome) → is_bank=True."""
    from features.industry_utils import detect_industry
    _company(conn, "BANKX", "SOME CORP")
    _fund(conn, "BANKX", "us-gaap/NetInterestIncome", 1_000_000_000)
    result = detect_industry(conn, "BANKX")
    assert result["is_bank"] is True


def test_non_bank_interest_expense_not_detected_as_bank(conn):
    """InterestIncomeExpenseNet is negative for non-banks (interest expense) — not a bank signal."""
    from features.industry_utils import detect_industry
    _company(conn, "BKRX", "BAKER ENERGY CORP")
    _fund(conn, "BKRX", "us-gaap/InterestIncomeExpenseNet", -161_000_000)
    result = detect_industry(conn, "BKRX")
    assert result["is_bank"] is False, \
        f"Negative interest expense (non-bank XBRL) should not classify as bank, got {result}"


def test_insurer_xbrl_detected(conn):
    """Company with insurer XBRL → is_insurer=True, not misclassified as bank."""
    from features.industry_utils import detect_industry
    _company(conn, "AIGX", "AMERICAN INTERNATIONAL GROUP INC")
    _fund(conn, "AIGX", "us-gaap/SupplementaryInsuranceInformationPremiumRevenue", 23_000_000_000)
    _fund(conn, "AIGX", "us-gaap/NetInterestIncome", 500_000_000)  # insurers also have this
    result = detect_industry(conn, "AIGX")
    assert result["is_insurer"] is True
    assert result["is_bank"] is False  # insurer XBRL overrides bank XBRL


def test_insurer_name_detected(conn):
    """Company with 'INSURANCE' in name → is_insurer=True."""
    from features.industry_utils import detect_industry
    _company(conn, "AFLX", "AFLAC INC INSURANCE CO")
    result = detect_industry(conn, "AFLX")
    assert result["is_insurer"] is True


# ── Bank valuation guards ─────────────────────────────────────────────────────

def test_bank_fee_income_only_nulls_ps(conn):
    """Bank with only fee-income revenue concept → P/S null (bank_revenue_concept_missing)."""
    _company(conn, "CFG", "CITIZENS FINANCIAL GROUP INC")
    _price(conn, "CFG", 64.0)
    _fund(conn, "CFG", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 437_000_000)
    # Only fee-income concept available — NOT us-gaap/Revenues
    _fund(conn, "CFG", "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax", 1_635_000_000)

    feats = compute_valuation(conn, "run1", "CFG", date.today())
    ps = _feat(feats, "ps_proxy")
    assert ps["feature_score"] is None, \
        f"Bank with fee-income-only revenue should null P/S, got {ps}"
    meta = ps.get("metadata") or {}
    assert "bank_revenue" in meta.get("missing_reason", ""), \
        f"Missing reason should mention bank_revenue, got {meta}"


def test_bank_ps_valid_when_total_revenue_exists(conn):
    """Bank with us-gaap/Revenues available → P/S computes normally."""
    _company(conn, "BACC", "BANK OF AMERICA CORP")
    _price(conn, "BACC", 53.0)
    _fund(conn, "BACC", "us-gaap/WeightedAverageNumberOfDilutedSharesOutstanding", 7_680_000_000)
    _fund(conn, "BACC", "us-gaap/Revenues", 113_000_000_000)
    # Fee income also present (both exist — should use Revenues)
    _fund(conn, "BACC", "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax", 15_000_000_000)

    feats = compute_valuation(conn, "run1", "BACC", date.today())
    ps = _feat(feats, "ps_proxy")
    # P/S = 53 * 7.68B / 113B ≈ 3.6 → within bounds → should have a score
    assert ps["feature_score"] is not None, \
        f"Bank with Revenues concept should compute P/S normally, got {ps}"


# ── NI > Revenue concept mismatch ─────────────────────────────────────────────

def test_ni_exceeds_revenue_flags_concept_mismatch(conn):
    """Net income > revenue is impossible — flag as financial_concept_mismatch."""
    _company(conn, "CFG2", "CFG FINANCIAL")
    _fund(conn, "CFG2", "us-gaap/NetIncomeLoss", 1_831_000_000)
    _fund(conn, "CFG2", "us-gaap/Revenues", 1_600_000_000)  # revenue < NI

    feats = compute_quality(conn, "run1", "CFG2", date.today())
    margin = _feat(feats, "net_margin")
    assert margin["feature_score"] is None, \
        f"NI > revenue should null net_margin score, got {margin}"
    meta = margin.get("metadata") or {}
    assert "financial_concept_mismatch" in meta.get("missing_reason", ""), \
        f"Should flag financial_concept_mismatch, got {meta}"


# ── Bank quality confidence ────────────────────────────────────────────────────

def test_bank_quality_lowers_confidence(conn):
    """Bank → net_margin and revenue_growth confidence lowered to 'low'."""
    _company(conn, "BNKQ", "BNKQ BANCORP INC")
    _fund(conn, "BNKQ", "us-gaap/NetIncomeLoss", 500_000_000)
    _fund(conn, "BNKQ", "us-gaap/Revenues", 4_000_000_000, dt=date(2025, 12, 31))
    _fund(conn, "BNKQ", "us-gaap/Revenues", 3_500_000_000, dt=date(2024, 12, 31))

    feats = compute_quality(conn, "run1", "BNKQ", date.today())

    margin = _feat(feats, "net_margin")
    assert margin["confidence"] == "low", \
        f"Bank net_margin should have low confidence, got {margin['confidence']}"
    meta = margin.get("metadata") or {}
    assert "bank" in meta.get("quality_warning", ""), \
        f"Should have bank_specific_quality_required warning, got {meta}"

    rev_growth = _feat(feats, "revenue_growth_yoy")
    assert rev_growth["confidence"] == "low", \
        f"Bank revenue_growth should have low confidence, got {rev_growth['confidence']}"


# ── Insurer quality confidence ─────────────────────────────────────────────────

def test_insurer_generic_margin_lowers_confidence(conn):
    """Insurer → net_margin confidence lowered but value still computed."""
    _company(conn, "AIGX", "GREAT INSURANCE CORP")
    _fund(conn, "AIGX", "us-gaap/NetIncomeLoss", 217_000_000)
    _fund(conn, "AIGX", "us-gaap/Revenues", 26_775_000_000)
    # Seed insurer XBRL so detection works
    _fund(conn, "AIGX", "us-gaap/SupplementaryInsuranceInformationPremiumRevenue", 23_000_000_000)

    feats = compute_quality(conn, "run1", "AIGX", date.today())
    margin = _feat(feats, "net_margin")

    # Score is still computed (not auto-rejected)
    assert margin["feature_score"] is not None, \
        "Insurer margin should still compute a score (not auto-rejected)"
    # But confidence is lowered
    assert margin["confidence"] == "low", \
        f"Insurer net_margin should have low confidence, got {margin['confidence']}"
    meta = margin.get("metadata") or {}
    assert "insurance" in meta.get("quality_warning", ""), \
        f"Should have insurance_specific_quality_required warning, got {meta}"


# ── No auto-apply gate ─────────────────────────────────────────────────────────

def test_no_auto_apply_requires_approved(conn):
    """apply_experiment must raise if not in 'approved' status."""
    from learning.experiments import propose_experiment, apply_experiment
    eid = propose_experiment(conn, "Industry guard test", {}, ["features/quality.py"], "Test")
    with pytest.raises(ValueError, match="approved"):
        apply_experiment(conn, eid, "test_user")
