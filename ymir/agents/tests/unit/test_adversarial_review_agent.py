from pathlib import Path

import pytest
from flexmock import flexmock

import ymir.agents.adversarial_review_agent as ar_agent
from ymir.agents.adversarial_review_agent import (
    REVIEW_GATEWAY_TOOLS,
    build_adversarial_review_prompt,
    create_adversarial_review_agent,
)
from ymir.common.models import AssessedApplicabilityResult, ConfidenceAssessment
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.text import SearchTextTool, ViewTool


def _captured_factory(monkeypatch) -> dict:
    """Stub LLM construction and capture the ReasoningAgent kwargs."""
    captured = {}

    def fake_get_chat_model(*, model_name=None):
        captured["get_chat_model_model_name"] = model_name
        return flexmock(allow_parallel_tool_calls=False)

    def fake_is_reasoning_enabled(*, model_name=None):
        captured["is_reasoning_enabled_model_name"] = model_name
        return False

    def reasoning_agent_factory(**kwargs):
        captured["kwargs"] = kwargs
        return flexmock()

    monkeypatch.setattr(ar_agent, "get_chat_model", fake_get_chat_model)
    monkeypatch.setattr(ar_agent, "is_reasoning_enabled", fake_is_reasoning_enabled)
    monkeypatch.setattr(ar_agent, "get_tool_call_checker_config", lambda: None)
    monkeypatch.setattr(ar_agent, "ReasoningAgent", reasoning_agent_factory)
    return captured


def _applicability(**overrides) -> AssessedApplicabilityResult:
    payload = {
        "is_affected": False,
        "justification_category": "Component not Present",
        "explanation": (
            "The vulnerable library is absent from the source tree: find returned "
            "no matches for vulnerable_operation and the build skips the component."
        ),
        "confidence": ConfidenceAssessment(
            level="high", reason="Direct source inspection of the target tree"
        ),
    }
    payload.update(overrides)
    return AssessedApplicabilityResult(**payload)


def _prompt(**overrides) -> str:
    kwargs = {
        "jira_issue": "RHEL-12345",
        "package": "demo-pkg",
        "target_branch": "rhel-10.2",
        "cve_id": "CVE-2026-12345",
        "applicability": _applicability(),
        "unpacked_sources": Path("/src/tree"),
        "patch_files": [],
        "prep_ok": True,
        "pending_zstream_issues": None,
    }
    kwargs.update(overrides)
    return build_adversarial_review_prompt(**kwargs)


def test_factory_builds_reviewer_with_local_and_gateway_tools(monkeypatch):
    captured = _captured_factory(monkeypatch)
    gateway_tools = [flexmock(name=name) for name in (*REVIEW_GATEWAY_TOOLS, "set_jira_labels")]

    create_adversarial_review_agent(gateway_tools, {"working_directory": "/src/tree"})

    kwargs = captured["kwargs"]
    assert kwargs["name"] == "AdversarialReviewAgent"
    assert "adversarial" in kwargs["role"].lower()
    assert kwargs["unconstrained"] is False
    assert kwargs["llm"] is not None
    assert "requirements" not in kwargs
    assert len(kwargs["middlewares"]) == 1

    tools = kwargs["tools"]
    assert [type(tool) for tool in tools[:3]] == [
        ViewTool,
        SearchTextTool,
        RunShellCommandTool,
    ]
    assert [tool.name for tool in tools[3:]] == list(REVIEW_GATEWAY_TOOLS)
    for tool in tools[:3]:
        assert tool.options.get("working_directory") == "/src/tree"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("anthropic:claude-opus-4-8", "anthropic:claude-opus-4-8"),
        (None, None),
        ("", None),
    ],
)
def test_factory_resolves_review_model_override(monkeypatch, env_value, expected):
    if env_value is None:
        monkeypatch.delenv("REVIEW_CHAT_MODEL", raising=False)
    else:
        monkeypatch.setenv("REVIEW_CHAT_MODEL", env_value)
    captured = _captured_factory(monkeypatch)

    create_adversarial_review_agent([], {})

    assert captured["get_chat_model_model_name"] == expected
    assert captured["is_reasoning_enabled_model_name"] == expected


def test_prompt_contains_the_conclusion_under_review():
    prompt = _prompt()

    assert "PROVE THAT CONCLUSION WRONG" in prompt
    assert "Component not Present" in prompt
    assert "find returned no matches for vulnerable_operation" in prompt
    assert "Self-rated confidence: high" in prompt
    assert "Direct source inspection of the target tree" in prompt
    assert "/src/tree" in prompt
    assert "demo-pkg" in prompt
    assert "rhel-10.2" in prompt
    assert "CVE-2026-12345" in prompt
    assert "RHEL-12345" in prompt


def test_prompt_requires_verification_before_upholding():
    prompt = _prompt()

    assert "PHASE 1 — VERIFY" in prompt
    assert "PHASE 2 — ATTACK" in prompt
    assert "You MUST actually run commands on the tree" in prompt
    assert "uphold a claim you have not checked" in prompt
    assert "Never uphold solely because the explanation" in prompt


@pytest.mark.parametrize(
    "fragment",
    [
        "Downstream patches",
        "Binary RPM subpackages",
        "Vendored/bundled copies",
        "Fix-patch application",
        "Build/execution path",
        "Jira context",
        "Sibling streams",
    ],
)
def test_prompt_lists_every_attack_menu_item(fragment):
    assert fragment in _prompt()


def test_prompt_names_the_concrete_attack_checks():
    prompt = _prompt()

    assert "git apply --check" in prompt
    assert "Patch*" in prompt
    assert "%package" in prompt
    assert "third_party/" in prompt
    assert "get_jira_details" in prompt
    assert "get_shared_rules" in prompt
    assert "get_maintainer_rules" in prompt


def test_prompt_describes_verdicts_and_output_schema():
    prompt = _prompt()

    for literal in ("upheld", "overturned", "inconclusive"):
        assert f'"{literal}"' in prompt
    for literal in ("verified", "refuted", "unverifiable"):
        assert f'"{literal}"' in prompt
    assert '"independent_conclusion"' in prompt
    assert '"claim_checks"' in prompt
    assert '"counter_evidence"' in prompt
    assert '"confidence"' in prompt
    assert "rates YOUR conclusion" in prompt


def test_prep_failure_warns_about_downstream_patches():
    failed = _prompt(prep_ok=False)
    prepared = _prompt(prep_ok=True)

    assert "IMPORTANT: RPM prep failed" in failed
    assert "Downstream patches from the dist-git spec" in failed
    assert "Patch*" in failed
    assert "confidence reason" in failed
    assert "IMPORTANT: RPM prep failed" not in prepared


@pytest.mark.parametrize(
    ("patch_files", "expected"),
    [
        (["/tmp/fix.patch", "/tmp/second.patch"], "/tmp/fix.patch"),
        ([], "absence of a patch is NOT evidence"),
    ],
)
def test_prompt_handles_patch_availability(patch_files, expected):
    assert expected in _prompt(patch_files=patch_files)


def test_prompt_includes_sibling_zstream_context():
    prompt = _prompt(pending_zstream_issues=["RHEL-1", "RHEL-2"])

    assert "RHEL-1" in prompt
    assert "RHEL-2" in prompt
    assert "disagreement across streams" in prompt


def test_prompt_forbids_upstream_cloning_and_fabrication():
    prompt = _prompt()

    assert "Do NOT clone" in prompt
    assert "guess or fabricate" in prompt
    assert "firewall" in prompt
