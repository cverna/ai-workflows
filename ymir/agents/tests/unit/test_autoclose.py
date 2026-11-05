import pytest

from ymir.agents.autoclose import (
    AutoCloseEvaluationInput,
    evaluate_autoclose_decision,
    sensitive_packages,
)
from ymir.common.models import (
    AdversarialReviewResult,
    AutoCloseBlocker,
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


def _green_input(**overrides) -> AutoCloseEvaluationInput:
    payload = {
        "review": _review_result(),
        "applicability_confidence": ConfidenceAssessment(
            level="high", reason="Direct target-source evidence"
        ),
        "used_fallback_sources": False,
        "severity": "Low",
        "package": "some-leaf-package",
        "pending_zstream_issues": [],
        "open_duplicate": None,
        "comment_posted": True,
        "status_changes_enabled": True,
    }
    payload.update(overrides)
    return AutoCloseEvaluationInput(**payload)


def test_green_input_is_eligible():
    decision = evaluate_autoclose_decision(_green_input())
    assert decision.eligible
    assert decision.blockers == []


def test_missing_review_blocks_even_when_all_else_green():
    """Fail-safe: absence of review is never approval."""
    decision = evaluate_autoclose_decision(_green_input(review=None))
    assert decision.blockers == [AutoCloseBlocker.NO_REVIEW]
    assert not decision.eligible


@pytest.mark.parametrize("level", ["medium", "low"])
def test_applicability_confidence_below_high_blocks(level):
    decision = evaluate_autoclose_decision(
        _green_input(applicability_confidence=ConfidenceAssessment(level=level, reason="Partial evidence"))
    )
    assert decision.blockers == [AutoCloseBlocker.CONFIDENCE]


def test_unassessed_applicability_confidence_blocks():
    decision = evaluate_autoclose_decision(_green_input(applicability_confidence=None))
    assert decision.blockers == [AutoCloseBlocker.CONFIDENCE]


@pytest.mark.parametrize("verdict", [ReviewVerdict.OVERTURNED, ReviewVerdict.INCONCLUSIVE])
def test_non_upheld_review_blocks(verdict):
    decision = evaluate_autoclose_decision(
        _green_input(
            review=_review_result(verdict=verdict, independent_conclusion="inconclusive"),
        )
    )
    assert decision.blockers == [AutoCloseBlocker.REVIEW]


@pytest.mark.parametrize("level", ["medium", "low"])
def test_review_confidence_below_high_blocks(level):
    decision = evaluate_autoclose_decision(
        _green_input(
            review=_review_result(
                confidence=ConfidenceAssessment(level=level, reason="Verification gaps remain")
            ),
        )
    )
    assert decision.blockers == [AutoCloseBlocker.REVIEW_CONFIDENCE]


def test_overturned_low_confidence_review_accumulates_both_review_blockers():
    decision = evaluate_autoclose_decision(
        _green_input(
            review=_review_result(
                verdict=ReviewVerdict.OVERTURNED,
                independent_conclusion="affected",
                confidence=ConfidenceAssessment(level="low", reason="Conflicting evidence"),
            ),
        )
    )
    assert decision.blockers == [AutoCloseBlocker.REVIEW, AutoCloseBlocker.REVIEW_CONFIDENCE]


def test_fallback_sources_block():
    decision = evaluate_autoclose_decision(_green_input(used_fallback_sources=True))
    assert decision.blockers == [AutoCloseBlocker.PREP_FALLBACK]


@pytest.mark.parametrize("severity", ["Important", "Critical", "Informational", "None", "", None, "Unknown"])
def test_non_closable_severity_blocks(severity):
    decision = evaluate_autoclose_decision(_green_input(severity=severity))
    assert decision.blockers == [AutoCloseBlocker.SEVERITY]


@pytest.mark.parametrize("severity", ["Low", "Moderate", "low", "MODERATE", " moderate "])
def test_closable_severity_passes(severity):
    decision = evaluate_autoclose_decision(_green_input(severity=severity))
    assert AutoCloseBlocker.SEVERITY not in decision.blockers


@pytest.mark.parametrize("package", ["kernel", "OpenSSL", "GLIBC"])
def test_sensitive_builtin_package_blocks(package):
    decision = evaluate_autoclose_decision(_green_input(package=package))
    assert decision.blockers == [AutoCloseBlocker.SENSITIVE_PACKAGE]


def test_sensitive_packages_env_extension():
    extended = sensitive_packages(env={"AUTO_CLOSE_SENSITIVE_PACKAGES": "MyPackage, other-pkg,, "})
    assert "mypackage" in extended
    assert "other-pkg" in extended
    assert "kernel" in extended


def test_sensitive_packages_env_unset_adds_nothing():
    assert sensitive_packages(env={}) == sensitive_packages(env={"AUTO_CLOSE_SENSITIVE_PACKAGES": ""})


def test_injected_sensitive_set_is_used():
    decision = evaluate_autoclose_decision(
        _green_input(package="my-package"), sensitive=frozenset({"my-package"})
    )
    assert decision.blockers == [AutoCloseBlocker.SENSITIVE_PACKAGE]


@pytest.mark.parametrize("siblings", [["RHEL-1"], ["RHEL-1", "RHEL-2"]])
def test_pending_siblings_block(siblings):
    decision = evaluate_autoclose_decision(_green_input(pending_zstream_issues=siblings))
    assert decision.blockers == [AutoCloseBlocker.SIBLINGS]


def test_open_duplicate_blocks():
    decision = evaluate_autoclose_decision(_green_input(open_duplicate="RHEL-42"))
    assert decision.blockers == [AutoCloseBlocker.DUPLICATE]


def test_suppressed_comment_blocks():
    decision = evaluate_autoclose_decision(_green_input(comment_posted=False))
    assert decision.blockers == [AutoCloseBlocker.NO_COMMENT]


def test_disabled_status_changes_block():
    """The platform-wide JIRA_ALLOW_STATUS_CHANGES guard is a prerequisite."""
    decision = evaluate_autoclose_decision(_green_input(status_changes_enabled=False))
    assert decision.blockers == [AutoCloseBlocker.STATUS_CHANGES_DISABLED]


def test_blockers_accumulate_in_documented_order():
    decision = evaluate_autoclose_decision(
        _green_input(
            review=None,
            applicability_confidence=None,
            used_fallback_sources=True,
            severity="Critical",
            package="kernel",
            pending_zstream_issues=["RHEL-1"],
            open_duplicate="RHEL-2",
            comment_posted=False,
        )
    )
    assert decision.blockers == [
        AutoCloseBlocker.NO_REVIEW,
        AutoCloseBlocker.CONFIDENCE,
        AutoCloseBlocker.PREP_FALLBACK,
        AutoCloseBlocker.SEVERITY,
        AutoCloseBlocker.SENSITIVE_PACKAGE,
        AutoCloseBlocker.SIBLINGS,
        AutoCloseBlocker.DUPLICATE,
        AutoCloseBlocker.NO_COMMENT,
    ]


def test_gate_never_emits_state_changed():
    """STATE_CHANGED belongs to the close step's runtime status re-check."""
    decision = evaluate_autoclose_decision(_green_input())
    assert AutoCloseBlocker.STATE_CHANGED not in decision.blockers
