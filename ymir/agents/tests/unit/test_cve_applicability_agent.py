from pathlib import Path

from ymir.agents.cve_applicability_agent import build_applicability_prompt
from ymir.common.models import Resolution


def _prompt(**overrides) -> str:
    kwargs = {
        "jira_issue": "RHEL-1",
        "package": "demo",
        "target_branch": "c10s",
        "resolution": Resolution.BACKPORT,
        "cve_id": "CVE-2026-12345",
        "dep_component": None,
        "dep_issue_key": None,
        "patch_files": [],
        "unpacked_sources": Path("/tmp/demo"),
        "prep_ok": True,
    }
    kwargs.update(overrides)
    return build_applicability_prompt(**kwargs)


def test_prep_failure_requires_inspecting_extra_source_archives():
    """A Source0-only fallback must not yield 'Component not Present' while
    vendor/bundled Source* archives remain uninspected."""
    prompt = _prompt(prep_ok=False)

    assert "RPM prep failed" in prompt
    assert "Source* archive" in prompt
    assert "vendor/bundled dependency tarballs" in prompt
    assert "tar -tf" in prompt


def test_prep_success_omits_the_fallback_warning():
    prompt = _prompt(prep_ok=True)
    assert "RPM prep failed" not in prompt


def test_physical_presence_rule_has_a_vendor_tarball_exception():
    prompt = _prompt()

    assert "Exception: if the spec declares a separate Source* archive" in prompt
    assert "does not establish" in prompt
    assert "Inconclusive" in prompt
