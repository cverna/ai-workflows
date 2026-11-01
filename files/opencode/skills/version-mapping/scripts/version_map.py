#!/usr/bin/env python3
"""Map a Jira fixVersion to the branch to work on, and explain why.

Getting this wrong invalidates everything downstream, and the model has been
observed conflating ``rhel-10.2`` with ``rhel-10.2.z`` and asking for branches
that do not exist. The parsing and branch construction live in
``ymir.common.version_utils``; the routing rules mirror
``ymir.agents.triage_agent._map_version_to_branch`` so the answer matches what
production does.

The important one: **a missing branch is not a blocker.** RHEL-first fixes
create the internal branch, and stream-first fixes land on CentOS Stream and are
inherited. Asking for clarification because a branch does not exist yet is a
routing error, not caution.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ymir.common.version_utils import (
    construct_internal_branch_name,
    get_maintenance_majors,
    is_modular,
    normalize_fix_version,
    parse_module_stream,
    parse_rhel_version,
)

DEFAULT_CONFIG_PATHS = (
    Path("rhel-config.json"),
    Path("/home/beeai/rhel-config.json"),
    Path("/home/mcp/rhel-config.json"),
)


def load_config(path: str | None = None) -> dict:
    candidates = [Path(path)] if path else list(DEFAULT_CONFIG_PATHS)
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise FileNotFoundError(
        "rhel-config.json not found; pass --rhel-config (looked in "
        + ", ".join(str(c) for c in candidates)
        + ")"
    )


def resolve_target_branch(
    *,
    major: str,
    minor: str,
    is_zstream: bool,
    older_zstream: bool,
    current_y_stream: str | None,
    needs_internal_fix: bool,
    internal_branch_exists: bool | None,
) -> tuple[str, str]:
    """Pick the branch to work on, mirroring ``_map_version_to_branch``.

    Returns ``(branch, reason)``. A branch that does not exist yet is *never* a
    reason to stop: RHEL-first fixes create it, and stream-first fixes land on
    CentOS Stream and are inherited.
    """
    internal = construct_internal_branch_name(major, minor)
    stream = f"c{major}s"

    if needs_internal_fix and not older_zstream:
        if current_y_stream:
            return internal, "RHEL-first CVE: internal fix needed; branch is created if absent"
        return stream, f"RHEL-first CVE, but RHEL {major} has no Y-stream; the fix lands on {stream}"

    if older_zstream:
        return internal, "older Z-stream: internal RHEL branch, created if absent"

    if is_zstream:
        if internal_branch_exists is True:
            return internal, "internal Z-stream branch already exists"
        if internal_branch_exists is False:
            return stream, f"no internal Z-stream branch; work on {stream} and inherit it"
        return stream, f"internal branch existence unconfirmed; defaulting to {stream}"

    return stream, f"stream-first/Y-stream: work on {stream}"


def map_fix_version(
    fix_version: str,
    *,
    summary: str | None = None,
    downstream_component: str | None = None,
    config: dict | None = None,
    needs_internal_fix: bool = False,
    internal_branch_exists: bool | None = None,
) -> dict:
    """Resolve one fixVersion into branch/namespace/stream facts."""
    config = config or {}
    warnings: list[str] = []
    normalized = normalize_fix_version(fix_version, config)

    parsed = parse_rhel_version(normalized)
    if parsed is None:
        return {
            "fix_version": fix_version,
            "normalized": normalized,
            "status": "unparsed",
            "warnings": [f"cannot parse {fix_version!r} as a RHEL fix version"],
        }

    major, minor, is_zstream = parsed
    maintenance = get_maintenance_majors(config)
    current_y = (config.get("current_y_streams") or {}).get(major)
    current_z = (config.get("current_z_streams") or {}).get(major)

    modular = is_modular(summary, downstream_component)
    module_branch: str | None = None
    if modular:
        module = parse_module_stream(summary, downstream_component)
        if not module:
            warnings.append(
                "summary/component looks modular but the module:stream pair could not be parsed"
            )
        else:
            module_name, stream = module
            module_branch = f"stream-{module_name}-{stream}-rhel-{major}.{minor}.0"

    if current_z is None:
        warnings.append(f"no current_z_streams entry for RHEL {major}; older-Z detection skipped")

    older_zstream = False
    z_parsed = parse_rhel_version(current_z) if current_z else None
    if z_parsed:
        older_zstream = int(minor) < int(z_parsed[1])

    if current_y and normalized == current_y:
        stream_kind = "y_stream"
    elif current_z and normalized == current_z:
        stream_kind = "current_z_stream"
    elif older_zstream:
        stream_kind = "older_z_stream"
    else:
        stream_kind = "z_stream" if is_zstream else "y_stream"

    if major in maintenance:
        warnings.append(
            f"RHEL {major} is in maintenance (Z-stream only); a Y-stream branch is not expected"
        )

    internal_branch: str | None = None
    if modular:
        target_branch = module_branch
        namespace = (
            "rhel" if older_zstream or (needs_internal_fix and current_y) else "centos-stream"
        )
        reason = (
            "modular package: the module stream selects the branch"
            if module_branch
            else "modular package, but the module:stream pair could not be parsed"
        )
    else:
        internal_branch, reason = resolve_target_branch(
            major=major,
            minor=minor,
            is_zstream=is_zstream,
            older_zstream=older_zstream,
            current_y_stream=current_y,
            needs_internal_fix=needs_internal_fix,
            internal_branch_exists=internal_branch_exists,
        )
        target_branch = internal_branch
        namespace = "rhel"

    if (
        not modular
        and is_zstream
        and not older_zstream
        and not needs_internal_fix
        and internal_branch_exists is None
    ):
        warnings.append(
            "internal branch existence was not supplied; defaulted to CentOS Stream. "
            "Check get_internal_rhel_branches and re-run with "
            "--internal-branch-exists yes if the branch exists"
        )

    return {
        "fix_version": fix_version,
        "normalized": normalized,
        "status": "ok",
        "major": major,
        "minor": minor,
        "is_zstream": is_zstream,
        "stream_kind": stream_kind,
        "older_zstream": older_zstream,
        "modular": modular,
        "branch": target_branch,
        "target_branch": target_branch,
        "internal_branch": internal_branch,
        "namespace": namespace,
        "resolution_reason": reason,
        "needs_internal_fix": needs_internal_fix,
        "internal_branch_exists": internal_branch_exists,
        "current_y_stream": current_y,
        "current_z_stream": current_z,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix-version", required=True)
    parser.add_argument("--summary")
    parser.add_argument("--component", help="downstream component name from Jira")
    parser.add_argument("--rhel-config")
    parser.add_argument(
        "--needs-internal-fix",
        action="store_true",
        help="CVE eligibility says the internal RHEL fix must come first",
    )
    parser.add_argument(
        "--internal-branch-exists",
        choices=("yes", "no", "unknown"),
        default="unknown",
        help="whether the internal RHEL branch exists (from get_internal_rhel_branches)",
    )
    args = parser.parse_args(argv)

    exists = {"yes": True, "no": False, "unknown": None}[args.internal_branch_exists]

    try:
        config = load_config(args.rhel_config)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    result = map_fix_version(
        args.fix_version,
        summary=args.summary,
        downstream_component=args.component,
        config=config,
        needs_internal_fix=args.needs_internal_fix,
        internal_branch_exists=exists,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
