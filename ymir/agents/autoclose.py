"""Deterministic auto-close gate for not-affected conclusions.

The gate never calls an LLM or reads Jira state: every workflow fact that
decides whether a not-affected Jira issue may be closed automatically is
passed in explicitly, and every non-close is explainable by named blockers.
No LLM judges whether closing is safe — policy lives here, in versioned
code. The only deployment configuration read is the opt-in package list and
the protected-package extension (pass ``opted_in`` / ``sensitive`` to pin
them).

Auto-close is opt-in per package: an empty ``AUTO_CLOSE_PACKAGES`` means
nothing is eligible, so enabling JIRA_ALLOW_STATUS_CHANGES alone never
closes anything. The built-in protected list is a floor that opting in
cannot override.

Fail-safe principle (docs/adversarial_review_architecture.md): absence of
review is never approval. A skipped or failed review, an unassessed
confidence or an unposted justification comment each block the close.
"""

import os

from pydantic import BaseModel, Field

from ymir.common.models import (
    AdversarialReviewResult,
    AutoCloseBlocker,
    AutoCloseDecision,
    ConfidenceAssessment,
    ReviewVerdict,
)

# Packages that may auto-close. Empty by default: auto-close is opt-in per
# package via AUTO_CLOSE_PACKAGES (comma-separated), so turning on the
# platform-wide status-change flag is not by itself enough to close anything.
AUTO_CLOSE_PACKAGES: frozenset[str] = frozenset()

# Packages where a wrong not-affected call has the highest blast radius. A
# floor, not a default: opting a package in here still blocks it. Extend per
# environment via AUTO_CLOSE_SENSITIVE_PACKAGES rather than shrinking.
SENSITIVE_PACKAGES: frozenset[str] = frozenset(
    {
        "kernel",
        "kernel-rt",
        "glibc",
        "openssl",
        "openssl-libs",
        "gnutls",
        "nss",
        "nss-util",
        "cryptsetup",
        "openssh",
        "sudo",
        "systemd",
        "polkit",
        "selinux-policy",
    }
)

# Jira severity values (compared case-insensitively) eligible for auto-close.
# Critical and Important are never auto-closed; unknown or unset severity blocks.
AUTO_CLOSE_ALLOWED_SEVERITIES: frozenset[str] = frozenset({"low", "moderate"})


def _package_list(env: dict[str, str] | None, variable: str) -> frozenset[str]:
    source = os.environ if env is None else env
    return {package.strip().lower() for package in source.get(variable, "").split(",") if package.strip()}


def opted_in_packages(env: dict[str, str] | None = None) -> frozenset[str]:
    """Packages named by AUTO_CLOSE_PACKAGES; empty means none are eligible.

    ``env`` defaults to ``os.environ``; passing an explicit mapping keeps
    callers (and tests) pure.
    """
    return AUTO_CLOSE_PACKAGES | _package_list(env, "AUTO_CLOSE_PACKAGES")


def sensitive_packages(env: dict[str, str] | None = None) -> frozenset[str]:
    """Built-in protected list plus the AUTO_CLOSE_SENSITIVE_PACKAGES extension.

    ``env`` defaults to ``os.environ``; passing an explicit mapping keeps
    callers (and tests) pure.
    """
    return SENSITIVE_PACKAGES | _package_list(env, "AUTO_CLOSE_SENSITIVE_PACKAGES")


class AutoCloseEvaluationInput(BaseModel):
    """Everything the gate needs; assembled by the triage workflow.

    Defaults are the fail-safe choices: omitted review, confidence or
    comment each block the close on their own.
    """

    review: AdversarialReviewResult | None = Field(
        default=None, description="Adversarial review result; None means skipped or failed"
    )
    applicability_confidence: ConfidenceAssessment | None = Field(
        default=None, description="Applicability agent's self-assessment; None means unassessed"
    )
    used_fallback_sources: bool = Field(
        default=False, description="True when sources were analyzed unpatched (RPM prep failed)"
    )
    severity: str | None = Field(default=None, description="Raw Jira severity value, e.g. 'Low'")
    package: str = Field(default="", description="Source package under triage")
    pending_zstream_issues: list[str] = Field(
        default_factory=list, description="Unshipped z-stream siblings for the same CVE"
    )
    open_duplicate: str | None = Field(
        default=None, description="Open duplicate tracker for the same CVE, if any"
    )
    cve_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Every CVE named by the issue. Only the first is analyzed, so more "
            "than one leaves the others unassessed and blocks the close"
        ),
    )
    comment_posted: bool = Field(
        default=False, description="True only when the justification comment was posted this run"
    )
    status_changes_enabled: bool = Field(
        default=False,
        description=(
            "Whether the platform permits Jira status transitions "
            "(JIRA_ALLOW_STATUS_CHANGES); without it an issue cannot be closed"
        ),
    )


def evaluate_autoclose_decision(
    decision_input: AutoCloseEvaluationInput,
    sensitive: frozenset[str] | None = None,
    opted_in: frozenset[str] | None = None,
) -> AutoCloseDecision:
    """Decide whether a not-affected issue may auto-close.

    No LLM or Jira access and never raises: every workflow fact is passed
    in. The only configuration read is the package policy, which callers can
    pin by passing ``opted_in`` and ``sensitive``. Blockers accumulate in the
    order documented in docs/adversarial_review_architecture.md, so the
    decision is deterministic and reproducible from the recorded inputs.
    STATE_CHANGED is never emitted here — it belongs to the close step's
    runtime status re-check.
    """
    blockers: list[AutoCloseBlocker] = []

    review = decision_input.review
    if review is None:
        blockers.append(AutoCloseBlocker.NO_REVIEW)

    applicability_confidence = decision_input.applicability_confidence
    if applicability_confidence is None or applicability_confidence.level != "high":
        blockers.append(AutoCloseBlocker.CONFIDENCE)

    if review is not None:
        if review.verdict != ReviewVerdict.UPHELD:
            blockers.append(AutoCloseBlocker.REVIEW)
        if review.confidence.level != "high":
            blockers.append(AutoCloseBlocker.REVIEW_CONFIDENCE)
        # The schema cannot say "upheld but affected", and nothing stops a
        # model from emitting it. The prompt forbids it; the gate is where
        # that is actually enforced. A refuted claim means the reviewer
        # itself found the evidence against the conclusion.
        if review.verdict == ReviewVerdict.UPHELD and (
            review.independent_conclusion != "not_affected"
            or any(check.status == "refuted" for check in review.claim_checks)
        ):
            blockers.append(AutoCloseBlocker.REVIEW_INCONSISTENT)

    if decision_input.used_fallback_sources:
        blockers.append(AutoCloseBlocker.PREP_FALLBACK)

    severity = (decision_input.severity or "").strip().lower()
    if severity not in AUTO_CLOSE_ALLOWED_SEVERITIES:
        blockers.append(AutoCloseBlocker.SEVERITY)

    package = decision_input.package.strip().lower()

    resolved_sensitive = sensitive_packages() if sensitive is None else sensitive
    if package in resolved_sensitive:
        blockers.append(AutoCloseBlocker.SENSITIVE_PACKAGE)

    # Opt-in, not opt-out: a package nobody listed is never closed. An empty
    # list (the default) therefore closes nothing.
    resolved_opted_in = opted_in_packages() if opted_in is None else opted_in
    if package not in resolved_opted_in:
        blockers.append(AutoCloseBlocker.PACKAGE_NOT_OPTED_IN)

    if decision_input.pending_zstream_issues:
        blockers.append(AutoCloseBlocker.SIBLINGS)

    if decision_input.open_duplicate:
        blockers.append(AutoCloseBlocker.DUPLICATE)

    if len(decision_input.cve_ids) > 1:
        blockers.append(AutoCloseBlocker.MULTI_CVE)

    if not decision_input.comment_posted:
        blockers.append(AutoCloseBlocker.NO_COMMENT)

    if not decision_input.status_changes_enabled:
        blockers.append(AutoCloseBlocker.STATUS_CHANGES_DISABLED)

    return AutoCloseDecision(blockers=blockers)
