"""Tests for deterministic catalyst classification rules."""
import pytest

from missed.deterministic_catalyst_rules import DeterministicResult, classify_deterministic


def test_merger_agreement_detected():
    text = "the Company entered into a definitive merger agreement with Acquirer Corp."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "merger_acquisition"
    assert result.confidence > 0


def test_earnings_release_detected():
    text = "fourth quarter earnings per share of $2.15, exceeding analyst estimates."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "earnings"


def test_guidance_raised_detected():
    text = "The Company raises its full-year revenue guidance to $5.2 billion."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "guidance"
    assert result.sentiment == "bullish"


def test_guidance_lowered_detected():
    text = "The Company lowered its guidance for the fiscal year."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "guidance"
    assert result.sentiment == "bearish"


def test_ceo_appointment_detected():
    text = "John Smith has been appointed as Chief Executive Officer effective January 1."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "management_change"
    assert result.sentiment == "neutral"


def test_ceo_resignation_detected():
    text = "Jane Doe has resigned as Chief Executive Officer of the Company."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "management_change"
    assert result.sentiment == "bearish"


def test_clinical_trial_positive_detected():
    text = "Phase 3 trial met its primary endpoint with statistical significance."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "regulatory"
    assert result.sentiment == "bullish"


def test_fda_approval_detected():
    text = "The FDA approved the Company's new drug application."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "regulatory"
    assert result.sentiment == "bullish"


def test_fda_rejection_detected():
    text = "The FDA issued a Complete Response Letter for our pending NDA."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "regulatory"
    assert result.sentiment == "bearish"


def test_legal_settlement_detected():
    text = "agreed to settle the class action lawsuit for $450 million."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "regulatory"
    assert result.sentiment == "bearish"


def test_product_launch_detected():
    text = "The Company announced the general availability of its new cloud platform."
    result = classify_deterministic(text)
    assert result is not None
    assert result.catalyst_type == "product_launch"


def test_unrecognized_text_returns_none():
    text = "This is a routine quarterly filing with standard disclosures only."
    result = classify_deterministic(text)
    assert result is None


def test_empty_string_returns_none():
    result = classify_deterministic("")
    assert result is None


def test_none_equivalent_returns_none():
    result = classify_deterministic(None)
    assert result is None


def test_deterministic_result_is_frozen():
    result = DeterministicResult(
        catalyst_type="earnings", confidence=0.85, sentiment="neutral", matched_rule="test"
    )
    with pytest.raises(Exception):
        result.catalyst_type = "guidance"
