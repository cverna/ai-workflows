#!/usr/bin/env python3
"""Map a Jira fixVersion to its dist-git branch, namespace and stream type.

Getting this wrong invalidates everything downstream, and the model has been
observed conflating ``rhel-10.2`` with ``rhel-10.2.z`` and asking for branches
that do not exist. The rules themselves live in ``ymir.common.version_utils``;
this is the CLI wrapper that makes them usable from a skill.
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


def map_fix_version(
    fix_version: str,
    *,
    summary: str | None = None,
    downstream_component: str | None = None,
    config: dict | None = None,
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
    branch: str | None
    if modular:
        module = parse_module_stream(summary, downstream_component)
        if not module:
            branch = None
            warnings.append(
                "summary/component looks modular but the module:stream pair could not be parsed"
            )
        else:
            module_name, stream = module
            branch = f"stream-{module_name}-{stream}-rhel-{major}.{minor}.0"
    else:
        branch = construct_internal_branch_name(major, minor)

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
        "branch": branch,
        "namespace": "rhel",
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
    args = parser.parse_args(argv)

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
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
