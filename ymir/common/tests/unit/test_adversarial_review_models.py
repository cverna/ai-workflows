import pytest
from pydantic import ValidationError

from ymir.common.models import (
    AdversarialReviewResult,
    AutoCloseBlocker,
    AutoCloseDecision,
    ClaimCheck,
    ConfidenceAssessment,
    ReviewVerdict,
)


def _review_result(**overrides) -> AdversarialReviewResult:
    payload = {
        "verdict": ReviewVerdict.UPHELD,
        "independent_conclusion": "not_affected",
        "claim_checks": [
            ClaimCheck(
                claim="Vulnerable files absent", status="verified", evidence="find returned no matches"
            )
        ],
        "confidence": ConfidenceAssessment(level="high", reason="Re-checked the source tree directly"),
    }
    payload.update(overrides)
    return AdversarialReviewResult(**payload)


@pytest.mark.parametrize("verdict", ["upheld", "overturned", "inconclusive"])
def test_review_result_round_trip(verdict):
    review = _review_result(verdict=verdict)
    assert AdversarialReviewResult.model_validate_json(review.model_dump_json()) == review


@pytest.mark.parametrize("verdict", ["agree", "UPHELD", 1, None])
def test_invalid_verdict_rejected(verdict):
    with pytest.raises(ValidationError):
        _review_result(verdict=verdict)


@pytest.mark.parametrize("conclusion", ["affected", "not_affected", "inconclusive"])
def test_independent_conclusion_literals_accepted(conclusion):
    review = _review_result(independent_conclusion=conclusion)
    assert review.independent_conclusion == conclusion


@pytest.mark.parametrize("conclusion", ["affected ", "NOT_AFFECTED", "unknown", ""])
def test_invalid_independent_conclusion_rejected(conclusion):
    with pytest.raises(ValidationError):
        _review_result(independent_conclusion=conclusion)


@pytest.mark.parametrize("status", ["verified", "refuted", "unverifiable"])
def test_claim_check_round_trip(status):
    check = ClaimCheck(claim="Patches do not apply", status=status, evidence="git apply --check failed")
    assert ClaimCheck.model_validate_json(check.model_dump_json()) == check


@pytest.mark.parametrize("status", ["checked", "VERIFIED", True, None])
def test_invalid_claim_status_rejected(status):
    with pytest.raises(ValidationError):
        ClaimCheck(claim="Patches do not apply", status=status, evidence="git apply --check failed")


@pytest.mark.parametrize("text", ["", " ", "\n\t"])
def test_claim_check_requires_substantive_text(text):
    with pytest.raises(ValidationError):
        ClaimCheck(claim=text, status="verified", evidence="found")
    with pytest.raises(ValidationError):
        ClaimCheck(claim="Patches do not apply", status="verified", evidence=text)


def test_counter_evidence_is_optional():
    assert _review_result().counter_evidence is None
    review = _review_result(counter_evidence="Fix patch applies cleanly to the vendored copy")
    assert review.counter_evidence == "Fix patch applies cleanly to the vendored copy"


def test_review_requires_confidence():
    payload = _review_result().model_dump(exclude={"confidence"})
    with pytest.raises(ValidationError):
        AdversarialReviewResult.model_validate(payload)


def test_claim_checks_default_to_empty():
    review = AdversarialReviewResult(
        verdict=ReviewVerdict.INCONCLUSIVE,
        independent_conclusion="inconclusive",
        confidence=ConfidenceAssessment(level="low", reason="Could not verify the build flags"),
    )
    assert review.claim_checks == []


def test_decision_eligible_only_without_blockers():
    assert AutoCloseDecision().eligible is True
    assert AutoCloseDecision(blockers=[]).eligible is True
    assert AutoCloseDecision(blockers=[AutoCloseBlocker.NO_REVIEW]).eligible is False


@pytest.mark.parametrize("blocker", list(AutoCloseBlocker))
def test_blocker_values_are_stable_vocabulary(blocker):
    """Blocker names are log and metric vocabulary; a change is observable."""
    assert blocker.value == blocker.name.lower()
