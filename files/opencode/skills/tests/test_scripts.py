"""Tests for the OpenCode skill scripts.

The scripts live inside the skill directories so each skill is self-contained,
which means pytest cannot import them as modules. Load them by path instead.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, SKILLS / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provenance = _load("provenance", "build-provenance/scripts/provenance.py")
labels = _load("labels", "jira-labels/scripts/labels.py")
version_map = _load("version_map", "version-mapping/scripts/version_map.py")


class _FakeSession:
    """Minimal Koji stand-in: tags map to builds, build ids to details."""

    def __init__(self, tagged: dict, details: dict | None = None) -> None:
        self._tagged = tagged
        self._details = details or {}

    def listTagged(self, package, tag, latest=True, inherit=True, strict=False):
        return self._tagged.get(tag, [])

    def getBuild(self, build_id, strict=False):
        return self._details.get(build_id, {})


def _build(nvr, version, release, build_id, source=None):
    entry = {"nvr": nvr, "version": version, "release": release, "build_id": build_id}
    if source is not None:
        entry["source"] = source
    return entry


class TestProvenance:
    def test_picks_newest_across_candidate_tags(self):
        session = _FakeSession(
            tagged={
                "rhel-9.7.0-candidate": [_build("ignition-2.22.0-2.el9", "2.22.0", "2.el9", 1)],
                "rhel-9.7.0-z-candidate": [_build("ignition-2.23.0-2.el9_7", "2.23.0", "2.el9_7", 2)],
            },
            details={2: {"source": "git+https://pkgs.devel.redhat.com/git/rpms/ignition#abc123"}},
        )
        result = provenance.resolve("ignition", "rhel-9.7.0", session=session)

        assert result["status"] == "resolved"
        assert result["build"]["nvr"] == "ignition-2.23.0-2.el9_7"
        assert result["build"]["tag"] == "rhel-9.7.0-z-candidate"
        assert result["source"]["commit"] == "abc123"
        assert len(result["candidates"]) == 2

    def test_reports_no_build_rather_than_guessing(self):
        result = provenance.resolve("nope", "rhel-9.9.0", session=_FakeSession({}))
        assert result["status"] == "no_build"
        assert any("no build found" in w for w in result["warnings"])

    def test_missing_source_commit_lowers_the_claim(self):
        session = _FakeSession(
            tagged={"rhel-9.7.0-candidate": [_build("pkg-1-1.el9", "1", "1.el9", 7)]},
            details={7: {"source": "git+https://example/repo"}},
        )
        result = provenance.resolve("pkg", "rhel-9.7.0", session=session)
        assert result["status"] == "resolved_without_source"
        assert result["source"]["commit"] is None

    def test_states_artifacts_are_not_downloadable(self):
        session = _FakeSession({"t-candidate": [_build("p-1-1", "1", "1", 3)]}, {3: {}})
        result = provenance.resolve("p", "b", tags=["t-candidate"], session=session)
        assert result["artifacts"]["downloadable_without_auth"] is False

    def test_tag_errors_are_reported_not_raised(self):
        class Boom:
            def listTagged(self, **_kwargs):
                raise RuntimeError("network down")

        result = provenance.resolve("p", "b", tags=["x"], session=Boom())
        assert result["status"] == "no_build"
        assert any("network down" in w for w in result["warnings"])

    def test_candidate_tags_include_z_variant(self):
        assert provenance.candidate_tags("rhel-9.8.0") == [
            "rhel-9.8.0-candidate",
            "rhel-9.8.0-z-candidate",
        ]


class TestLabels:
    def test_not_affected_uses_the_real_label(self):
        result = labels.compute("not-affected", confidence="medium", current=())
        assert "ymir_triaged_not_affected" in result["add"]
        assert "ymir_applicability_confidence_medium" in result["add"]
        assert "ymir_not_affected" not in result["add"]

    def test_replaces_other_triage_labels(self):
        result = labels.compute(
            "backport", current=("ymir_triaged_rebase", "ymir_needs_attention")
        )
        assert result["add"] == ["ymir_triaged_backport"]
        assert result["remove"] == ["ymir_needs_attention", "ymir_triaged_rebase"]

    def test_clears_confidence_when_unassessed(self):
        result = labels.compute(
            "not-affected", confidence=None, current=("ymir_applicability_confidence_high",)
        )
        assert result["remove"] == ["ymir_applicability_confidence_high"]

    def test_preserves_non_ymir_labels(self):
        result = labels.compute(
            "rebuild", current=("CVE-2026-1", "SecurityTracking", "pscomponent:vim")
        )
        assert result["preserve"] == ["CVE-2026-1", "SecurityTracking", "pscomponent:vim"]
        assert result["remove"] == []

    def test_unknown_ymir_labels_are_flagged_not_removed(self):
        result = labels.compute("not-affected", current=("ymir_not_affected",))
        assert "ymir_not_affected" not in result["remove"]
        assert any("not valid" in w for w in result["warnings"])

    def test_unknown_resolution_is_rejected(self):
        with pytest.raises(ValueError, match="unknown resolution"):
            labels.compute("nonsense")

    def test_unknown_confidence_is_rejected(self):
        with pytest.raises(ValueError, match="unknown confidence"):
            labels.compute("backport", confidence="certain")


CONFIG = {
    "current_y_streams": {"9": "rhel-9.10", "10": "rhel-10.4"},
    "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.8.z", "10": "rhel-10.2.z"},
    "upcoming_z_streams": {"9": "rhel-9.9.z", "10": "rhel-10.3.z"},
}


class TestVersionMap:
    def test_stale_y_stream_normalizes_to_z(self):
        result = version_map.map_fix_version("rhel-9.8", config=CONFIG)
        assert result["normalized"] == "rhel-9.8.z"
        assert result["stream_kind"] == "current_z_stream"

    def test_y_stream_lands_on_centos_stream(self):
        # BeeAI's _map_version_to_branch defaults to c<N>s unless the CVE needs
        # an internal fix first; a Y-stream fixVersion is not an internal branch.
        result = version_map.map_fix_version("rhel-9.10", config=CONFIG)
        assert result["normalized"] == "rhel-9.10"
        assert result["stream_kind"] == "y_stream"
        assert result["target_branch"] == "c9s"

    def test_rhel10_branch_has_no_suffix(self):
        result = version_map.map_fix_version(
            "rhel-10.2.z", config=CONFIG, internal_branch_exists=True
        )
        assert result["target_branch"] == "rhel-10.2"
        assert result["internal_branch"] == "rhel-10.2"

    def test_missing_internal_branch_falls_back_to_stream(self):
        # The msgpack case: rhel-10.2.z, no internal rhel-10.2 branch. This must
        # route to CentOS Stream, not become a clarification.
        result = version_map.map_fix_version(
            "rhel-10.2.z", config=CONFIG, internal_branch_exists=False
        )
        assert result["target_branch"] == "c10s"
        assert "inherit" in result["resolution_reason"]

    def test_unknown_branch_existence_warns_and_defaults_to_stream(self):
        result = version_map.map_fix_version("rhel-10.2.z", config=CONFIG)
        assert result["target_branch"] == "c10s"
        assert any("not supplied" in w for w in result["warnings"])

    def test_internal_fix_uses_internal_branch_even_if_absent(self):
        # RHEL-first CVE on a major with a Y-stream: the branch is created.
        result = version_map.map_fix_version(
            "rhel-9.9.z", config=CONFIG, needs_internal_fix=True
        )
        assert result["target_branch"] == "rhel-9.9.0"
        assert "created if absent" in result["resolution_reason"]

    def test_internal_fix_on_maintenance_major_uses_stream(self):
        # RHEL 8 has no Y-stream, so the fix belongs on c8s even when internal.
        result = version_map.map_fix_version(
            "rhel-8.10.z", config=CONFIG, needs_internal_fix=True
        )
        assert result["target_branch"] == "c8s"

    def test_older_zstream_detected(self):
        result = version_map.map_fix_version("rhel-9.4.z", config=CONFIG)
        assert result["older_zstream"] is True
        assert result["stream_kind"] == "older_z_stream"
        assert result["target_branch"] == "rhel-9.4.0"

    def test_modular_branch_shape(self):
        result = version_map.map_fix_version(
            "rhel-9.8",
            summary="postgresql:12/postgresql: Security update",
            downstream_component="postgresql",
            config=CONFIG,
        )
        assert result["modular"] is True
        assert result["branch"].startswith("stream-postgresql-")

    def test_unparsed_version_is_reported(self):
        result = version_map.map_fix_version("not-a-version", config=CONFIG)
        assert result["status"] == "unparsed"

    def test_missing_config_path_is_an_error(self):
        with pytest.raises(FileNotFoundError):
            version_map.load_config("/nonexistent/rhel-config.json")


def test_scripts_are_json_cli_compatible():
    """Each module exposes main(argv) and can run without touching the network."""
    assert callable(provenance.main)
    assert callable(labels.main)
    assert callable(version_map.main)
    assert labels.main(["--resolution", "backport"]) == 0
    json.loads('{"ok": true}')
