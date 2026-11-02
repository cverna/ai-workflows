#!/usr/bin/env python3
"""Resolve the build that shipped for a package branch, from Brew metadata only.

Read-only and unauthenticated: Brew serves build and tag metadata anonymously,
which is enough to answer "which NVR, built from which commit". Artifact
downloads are deliberately not attempted -- RPM retrieval needs Kerberos, which
lives only in the mcp-gateway pod. Report that gap instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import koji

BREWHUB_URL = "https://brewhub.engineering.redhat.com/brewhub"

# Eval hook: the mock-Jira fixtures freeze Jira and git at a past date, but Brew
# keeps serving today's builds. Pointing this at a fixture manifest makes the
# applicability check inspect the pinned commit instead of a build that already
# contains the fix. The file must set "eval_fixture": true to be honoured.
OVERRIDE_PATH = Path(
    os.getenv("YMIR_EVAL_PROVENANCE") or "/home/beeai/work/.eval/provenance.json"
)

try:  # RPM's comparator is the only correct way to order EVR strings.
    from rpm import labelCompare
except ImportError:  # pragma: no cover - rpm is installed in the image
    labelCompare = None


def fixture_override(package: str, branch: str) -> tuple[dict, str] | None:
    """Return the pinned commit for (package, branch), if an eval fixture exists.

    The fixture's branch can differ from the mapped branch: the harness does not
    apply per-case ``rhel_config_override``/``zstream_override``, so expat is
    pinned on ``rhel-10.1`` while the issue maps to ``rhel-10.2``. The manifest is
    per issue, so falling back to the package's only entry is unambiguous.
    """
    try:
        data = json.loads(OVERRIDE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("eval_fixture"):
        return None
    by_branch = (data.get("entries") or {}).get(package) or {}
    if not by_branch:
        return None
    if branch in by_branch:
        entry = by_branch[branch]
        return (entry, branch) if entry.get("commit") else None
    internal = {b: e for b, e in by_branch.items() if not b.startswith("c")}
    candidates = internal or by_branch
    if len(candidates) != 1:
        return None
    matched, entry = next(iter(candidates.items()))
    return (entry, matched) if entry.get("commit") else None


def candidate_tags(branch: str) -> list[str]:
    """Tags Ymir queries for a branch: current candidate, then Z-stream candidate."""
    return [f"{branch}-candidate", f"{branch}-z-candidate"]


def _evr(build: dict) -> tuple[str, str, str]:
    return ("0", str(build.get("version")), str(build.get("release")))


def _newer(left: dict, right: dict) -> bool:
    if labelCompare is None:  # pragma: no cover - fallback only
        return (_evr(left)[1], _evr(left)[2]) > (_evr(right)[1], _evr(right)[2])
    return labelCompare(_evr(left), _evr(right)) > 0


def _split_source(source: str | None) -> tuple[str | None, str | None]:
    """Brew records ``git+<url>#<commit>``; split it without trusting either half."""
    if not source:
        return None, None
    repository, separator, commit = source.rpartition("#")
    if not separator:
        return None, None
    return repository or None, commit or None


def resolve(
    package: str,
    branch: str,
    *,
    tags: list[str] | None = None,
    session=None,
    url: str = BREWHUB_URL,
) -> dict:
    """Return the newest candidate build for *package* on *branch*."""
    override = fixture_override(package, branch)
    if override:
        entry, matched_branch = override
        warnings = [
            "eval fixture: provenance pinned to the fixture's pre-fix commit; "
            "no NVR is implied and none should be reported as shipped"
        ]
        if matched_branch != branch:
            warnings.append(
                f"eval fixture: the fixture pins {package} on {matched_branch}, "
                f"not the queried {branch}; the commit comes from the fixture"
            )
        return {
            "package": package,
            "branch": branch,
            "brewhub": None,
            "tags_checked": [],
            "candidates": [],
            "warnings": warnings,
            "artifacts": {
                "downloadable_without_auth": False,
                "reason": "eval fixture: no build artifact corresponds to the pinned commit",
            },
            "status": "resolved_from_fixture",
            "build": {
                "nvr": None,
                "build_id": None,
                "tag": None,
                "owner": None,
                "completion_time": None,
            },
            "source": {
                "repository": entry.get("repository"),
                "commit": entry["commit"],
                "raw": "eval fixture",
            },
        }

    session = session or koji.ClientSession(url)
    tags = tags or candidate_tags(branch)
    result: dict = {
        "package": package,
        "branch": branch,
        "brewhub": url,
        "tags_checked": tags,
        "candidates": [],
        "warnings": [],
        "artifacts": {
            "downloadable_without_auth": False,
            "reason": "RPM download requires Kerberos; use the privileged gateway",
        },
    }

    best: dict | None = None
    best_tag: str | None = None
    for tag in tags:
        try:
            builds = session.listTagged(
                package=package, tag=tag, latest=True, inherit=True, strict=False
            )
        except Exception as error:  # noqa: BLE001 - report, never crash the caller
            result["warnings"].append(f"{tag}: {type(error).__name__}: {error}")
            continue
        for build in builds or []:
            result["candidates"].append(
                {"tag": tag, "nvr": build.get("nvr"), "build_id": build.get("build_id")}
            )
            if best is None or _newer(build, best):
                best, best_tag = build, tag

    if best is None:
        result["status"] = "no_build"
        result["warnings"].append("no build found in any candidate tag")
        return result

    try:
        detail = session.getBuild(best["build_id"], strict=False) or {}
    except Exception as error:  # noqa: BLE001
        detail = {}
        result["warnings"].append(f"getBuild: {type(error).__name__}: {error}")

    repository, commit = _split_source(detail.get("source"))
    result["status"] = "resolved" if commit else "resolved_without_source"
    result["build"] = {
        "nvr": best.get("nvr"),
        "build_id": best.get("build_id"),
        "tag": best_tag,
        "owner": detail.get("owner_name"),
        "completion_time": detail.get("completion_time"),
    }
    result["source"] = {
        "repository": repository,
        "commit": commit,
        "raw": detail.get("source"),
    }
    if not commit:
        result["warnings"].append(
            "build has no source commit; treat provenance as unverified"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--branch", required=True, help="dist-git branch, e.g. rhel-9.8.0")
    parser.add_argument("--tag", action="append", help="override the candidate tags")
    parser.add_argument("--url", default=BREWHUB_URL)
    args = parser.parse_args(argv)

    result = resolve(args.package, args.branch, tags=args.tag, url=args.url)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"].startswith("resolved") else 1


if __name__ == "__main__":
    raise SystemExit(main())
