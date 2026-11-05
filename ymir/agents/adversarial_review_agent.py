"""Adversarial review of not-affected CVE applicability conclusions.

Mirrors cve_applicability_agent.py: a standalone ReasoningAgent factory plus
a prompt builder, with no workflow or Jira state of its own. The reviewer
runs against the same source tree the applicability agent analyzed and
returns an AdversarialReviewResult (see docs/adversarial_review_architecture.md).
"""

import os
from pathlib import Path
from textwrap import dedent

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool

from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import get_chat_model, get_tool_call_checker_config, is_reasoning_enabled
from ymir.common.logging_setup import get_trajectory_writeable
from ymir.common.models import AssessedApplicabilityResult
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.text import SearchTextTool, ViewTool

# Read-only Jira context; sibling z-stream keys are passed into the prompt.
REVIEW_GATEWAY_TOOLS = ("get_jira_details", "get_maintainer_rules", "get_shared_rules")


def create_adversarial_review_agent(
    gateway_tools: list[Tool],
    local_tool_options: dict,
) -> ReasoningAgent:
    extra_gateway_tools = [t for t in gateway_tools if t.name in REVIEW_GATEWAY_TOOLS]
    # REVIEW_CHAT_MODEL decorrelates the reviewer's errors from the
    # applicability agent's. Unlike CHAT_MODEL_<AGENT>, this is resolved per
    # construction: both agents share the triage worker process.
    model_name = os.getenv("REVIEW_CHAT_MODEL") or None
    return ReasoningAgent(
        name="AdversarialReviewAgent",
        llm=get_chat_model(model_name=model_name),
        unconstrained=is_reasoning_enabled(model_name=model_name),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            ViewTool(options=local_tool_options),
            SearchTextTool(options=local_tool_options),
            RunShellCommandTool(options=local_tool_options),
            *extra_gateway_tools,
        ],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                force_after=Tool,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Adversarial security reviewer",
    )


def build_adversarial_review_prompt(
    *,
    jira_issue: str,
    package: str,
    target_branch: str,
    cve_id: str | None,
    applicability: AssessedApplicabilityResult,
    unpacked_sources: Path,
    patch_files: list[str],
    prep_ok: bool = True,
    pending_zstream_issues: list[str] | None = None,
) -> str:
    cve_label = cve_id or "the CVE"
    justification = applicability.justification_category or "unspecified"

    if patch_files:
        patch_info = "Upstream fix patches are available at: " + ", ".join(patch_files)
    else:
        patch_info = (
            "No upstream fix patch is available. The absence of a patch is NOT "
            "evidence that the package is not affected."
        )

    fallback_warning = ""
    if not prep_ok:
        fallback_warning = (
            "IMPORTANT: RPM prep failed — this tree is unpatched upstream source "
            "(Source0 extraction only). Downstream patches from the dist-git spec "
            "are NOT applied here. Inspect the spec's Patch* entries and any .patch "
            "files in the clone: a not-affected conclusion drawn from this tree is "
            "weak unless you account for what those patches change (they may add "
            "the vulnerable component, remove a mitigation, or already contain the "
            "fix). Explain this limitation in your confidence reason."
        )

    sibling_context = ""
    if pending_zstream_issues:
        sibling_context = (
            "Sibling z-stream issues for the same CVE are still pending: "
            f"{', '.join(pending_zstream_issues)}. Use get_jira_details to check "
            "whether any of them concluded the package IS affected — a disagreement "
            "across streams is strong counter-evidence."
        )

    return dedent(f"""\
        You are an adversarial security reviewer. A previous agent concluded
        that {cve_label} does NOT affect package '{package}' at the version
        shipped in branch '{target_branch}' (Jira issue {jira_issue}).

        Your job is to try to PROVE THAT CONCLUSION WRONG: find evidence that
        the package IS affected. Do not rubber-stamp it. A false "not
        affected" on a security tracker is far worse than a false alarm, and
        the previous agent rated its own conclusion — that rating is not
        independent evidence.

        CONCLUSION UNDER REVIEW
          Justification category: {justification}
          Explanation: {applicability.explanation}
          Self-rated confidence: {applicability.confidence.level}
          Confidence reason: {applicability.confidence.reason}

        The package source tree is at: {unpacked_sources}
        {patch_info}
        {sibling_context}
        {fallback_warning}

        CRITICAL: Analyze ONLY the source tree at {unpacked_sources} — this is
        the version shipped in RHEL. Do NOT clone or inspect the latest
        upstream repository. If a shell command fails because the firewall
        blocked it and the data is essential, stop and report that; never
        guess or fabricate content you could not retrieve.

        PHASE 1 — VERIFY
        Extract every concrete evidence claim from the explanation above
        (e.g. "files X/Y are absent", "BuildNodeJS=false", "the patch does
        not apply", "the component is not shipped"). Re-run each check
        yourself against {unpacked_sources} with the shell, view and search
        tools. Record one claim_checks entry per claim with status
        "verified", "refuted" or "unverifiable" and the command/output that
        justifies it. You MUST actually run commands on the tree; never
        uphold a claim you have not checked.

        PHASE 2 — ATTACK
        Actively hunt for evidence that the package IS affected. Check every
        item below that applies, and report what you ran:
         1. Downstream patches: when RPM prep failed (see above), inspect the
            spec's Patch* entries and the .patch files in the clone. Do they
            add the vulnerable component or remove a mitigation? Are they the
            only reason the shipped version might differ from this tree?
         2. Binary RPM subpackages: "component not present" at source level
            does not mean it is not shipped. Check the spec's %package
            subpackage definitions, %files lists and build flags — a
            subpackage may ship the component even when the main build skips
            it.
         3. Vendored/bundled copies: use find/grep over the whole tree, not
            just manifests (package.json, go.mod, requirements.txt). Look in
            non-obvious directories (third_party/, vendor/, bundled/,
            node_modules/, static/, test fixtures/). Manifests can declare
            dependencies the build skips — and omit ones it bundles.
         4. Fix-patch application: if patches are available, run
            `git apply --check` or `patch --dry-run` against the tree. A fix
            patch that applies cleanly is strong evidence the vulnerable code
            is present and unfixed — i.e. the package IS affected.
         5. Build/execution path: verify the claimed disabling build flag or
            unused import in the spec and sources. If the vulnerable
            operation is reachable through a conditional feature, a
            transitive dependency or a build-time path, it counts.
         6. Jira context: use get_jira_details on {jira_issue}. Maintainer
            comments, linked issues or attachments may contradict the
            conclusion. Also call get_shared_rules and get_maintainer_rules
            for '{package}' — package-specific rules (e.g. "rebuilds are
            always relevant") override a not-affected classification.
         7. Sibling streams: for the same CVE, an issue on another z-stream
            marked affected is strong counter-evidence (see list above).

        VERDICT RULES
        - "upheld" ONLY if your independent conclusion is "not_affected",
          every material claim is verified, and the attack phase found no
          refuting evidence. Never uphold solely because the explanation
          sounds plausible or because the previous agent rated it high.
        - "overturned" if you found evidence the package IS affected, or a
          material claim is refuted, or the shipped subpackages/patches
          contradict the conclusion.
        - "inconclusive" if evidence is incomplete or conflicting and you
          cannot resolve it. Be conservative: an unresolved gap is never
          "upheld".
        - "independent_conclusion" is YOUR OWN assessment of whether the
          package is affected, not a restatement of the verdict.

        CONFIDENCE (rates YOUR conclusion, not the original one)
        "high" requires direct target-source evidence and verification of
        the build features or execution paths relevant to your conclusion.
        "medium" means the evidence supports it but an important inference
        or verification gap remains. "low" means evidence is incomplete or
        conflicting. Do not claim runtime testing unless you performed it.
        Confidence is not CVE severity or exploit probability.

        Return a single JSON object with these fields:
        - "verdict": one of "upheld", "overturned", "inconclusive"
        - "independent_conclusion": one of "affected", "not_affected",
          "inconclusive"
        - "claim_checks": a list of objects, each with "claim", "status"
          (one of "verified", "refuted", "unverifiable") and "evidence"
          (the check you ran and what it returned)
        - "counter_evidence": the strongest evidence against the
          not-affected conclusion, or null
        - "confidence": an object with "level" (high, medium, low) and
          "reason" (evidence and remaining uncertainty)
        """)
