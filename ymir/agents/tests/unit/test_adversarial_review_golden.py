"""Deterministic golden case for the adversarial reviewer.

The reviewer's LLM decision itself is exercised by the e2e/eval path, not a
unit test. This covers the chain around it: a tree that contradicts the
applicability claim, a prompt that exposes the contradiction, the reviewer's
own tools finding it, and a parsed overturn that the auto-close gate refuses.
"""

from pathlib import Path

import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware

from ymir.agents.adversarial_review_agent import build_adversarial_review_prompt
from ymir.agents.autoclose import AutoCloseEvaluationInput, evaluate_autoclose_decision
from ymir.common.models import (
    AdversarialReviewResult,
    AssessedApplicabilityResult,
    AutoCloseBlocker,
    ConfidenceAssessment,
    ReviewVerdict,
)
from ymir.tools.unprivileged.text import SearchTextTool, SearchTextToolInput

VULNERABLE_CODE = """\
/* CVE-2026-12345: attacker-controlled input copied into a fixed buffer */
int vulnerable_operation(const char *input) {
    char buf[64];
    strcpy(buf, input);
    return 0;
}
"""


@pytest.mark.asyncio
async def test_golden_false_component_not_present_claim_is_overturnable(tmp_path: Path):
    """A tree where the vulnerable code IS present refutes a 'Component not
    Present' claim; the reviewer's tools can see it; the parsed overturn
    blocks the close even though the applicability agent rated itself high.
    """
    source_dir = tmp_path / "unpacked"
    (source_dir / "src").mkdir(parents=True)
    vulnerable_file = source_dir / "src" / "vulnerable_lib.c"
    vulnerable_file.write_text(VULNERABLE_CODE)

    applicability = AssessedApplicabilityResult(
        is_affected=False,
        justification_category="Component not Present",
        explanation=(
            "The vulnerable library is absent from the source tree: find returned "
            "no matches for vulnerable_operation and the build skips the component."
        ),
        confidence=ConfidenceAssessment(
            level="high", reason="Direct source inspection found no vulnerable files"
        ),
    )

    prompt = build_adversarial_review_prompt(
        jira_issue="RHEL-12345",
        package="demo-pkg",
        target_branch="rhel-10.2",
        cve_id="CVE-2026-12345",
        applicability=applicability,
        unpacked_sources=source_dir,
        patch_files=[],
        prep_ok=True,
    )

    # The prompt exposes the exact false claim and the tree to check it against.
    assert "find returned no matches for vulnerable_operation" in prompt
    assert str(source_dir) in prompt
    assert "PHASE 1 — VERIFY" in prompt
    assert "Vendored/bundled copies" in prompt

    # The reviewer's own tools can actually find the contradiction.
    tool = SearchTextTool(options={"working_directory": source_dir})
    output = await tool.run(
        input=SearchTextToolInput(file=vulnerable_file, pattern="vulnerable_operation")
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    assert "vulnerable_operation" in output.result

    # What an overturning reviewer emits, parsed through the real schema.
    review = AdversarialReviewResult.model_validate(
        {
            "verdict": "overturned",
            "independent_conclusion": "affected",
            "claim_checks": [
                {
                    "claim": "find returned no matches for vulnerable_operation",
                    "status": "refuted",
                    "evidence": (
                        f"{vulnerable_file.name} defines vulnerable_operation "
                        "with an unbounded strcpy of attacker-controlled input"
                    ),
                }
            ],
            "counter_evidence": (
                "src/vulnerable_lib.c contains the vulnerable operation; "
                "the component IS present in the shipped source"
            ),
            "confidence": {
                "level": "high",
                "reason": "Re-ran the search on the target tree and found the symbol",
            },
        }
    )
    assert review.verdict == ReviewVerdict.OVERTURNED
    assert review.claim_checks[0].status == "refuted"

    # A self-rated 'high' applicability result must not be enough to close.
    decision = evaluate_autoclose_decision(
        AutoCloseEvaluationInput(
            review=review,
            applicability_confidence=applicability.confidence,
            severity="Low",
            package="demo-pkg",
            comment_posted=True,
            status_changes_enabled=True,
        ),
        opted_in=frozenset({"demo-pkg"}),
    )
    assert AutoCloseBlocker.REVIEW in decision.blockers
    assert not decision.eligible
