#!/usr/bin/env python3
"""Compute the exact Jira label change for a triage resolution.

Models invent label names: two runs proposed ``ymir_not_affected``, which does
not exist (the real one is ``ymir_triaged_not_affected``). Jira accepts any
label string silently, so a hallucinated label is invisible. This maps the
resolution to the valid label set and computes add/remove against the labels an
issue already has.
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    from ymir.common.constants import JiraLabels

    VALID_LABELS: set[str] | None = {label.value for label in JiraLabels}
except Exception:  # pragma: no cover - only when run outside the image
    VALID_LABELS = None

RESOLUTION_LABELS: dict[str, str] = {
    "rebase": "ymir_triaged_rebase",
    "backport": "ymir_triaged_backport",
    "rebuild": "ymir_triaged_rebuild",
    "clarification-needed": "ymir_needs_attention",
    "open-ended-analysis": "ymir_triaged",
    "not-affected": "ymir_triaged_not_affected",
    "error": "ymir_triage_errored",
    "postponed_dependency": "ymir_postponed_dependency",
    "postponed_no_patch": "ymir_postponed_no_patch",
    "postponed_pr_pending": "ymir_postponed_pr_pending",
    "postponed_y_stream": "ymir_postponed_y_stream",
}

CONFIDENCE_LABELS: dict[str, str] = {
    "high": "ymir_applicability_confidence_high",
    "medium": "ymir_applicability_confidence_medium",
    "low": "ymir_applicability_confidence_low",
}

TRIAGE_LABELS = set(RESOLUTION_LABELS.values())
MANAGED = TRIAGE_LABELS | set(CONFIDENCE_LABELS.values())


def compute(resolution: str, *, confidence: str | None = None, current: tuple[str, ...] | list[str] = ()) -> dict:
    """Return ``{add, remove, ...}`` for re-triaging an issue."""
    if resolution not in RESOLUTION_LABELS:
        raise ValueError(
            f"unknown resolution {resolution!r}; expected one of {sorted(RESOLUTION_LABELS)}"
        )
    if confidence is not None and confidence not in CONFIDENCE_LABELS:
        raise ValueError(
            f"unknown confidence {confidence!r}; expected one of {sorted(CONFIDENCE_LABELS)}"
        )

    present = set(current)
    wanted = {RESOLUTION_LABELS[resolution]}
    if confidence:
        wanted.add(CONFIDENCE_LABELS[confidence])

    add = sorted(wanted - present)
    # Only ever remove labels this mapper owns; everything else is somebody's
    # state (security, SLA, sustaining) and must survive re-triage.
    remove = sorted((present & MANAGED) - wanted)

    warnings: list[str] = []
    unknown = sorted(
        label for label in present if label.startswith("ymir_") and label not in (VALID_LABELS or MANAGED)
    )
    if unknown:
        warnings.append(
            "issue carries ymir_* labels that are not valid; left untouched: "
            + ", ".join(unknown)
        )
    if VALID_LABELS is not None:
        invalid = sorted(label for label in wanted if label not in VALID_LABELS)
        if invalid:  # pragma: no cover - guards a typo in the map above
            raise ValueError(f"mapped labels are not valid Jira labels: {invalid}")

    return {
        "resolution": resolution,
        "confidence": confidence,
        "add": add,
        "remove": remove,
        "unchanged_ymir": sorted(present & MANAGED & wanted),
        "preserve": sorted(present - MANAGED),
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolution", required=True)
    parser.add_argument("--confidence", choices=sorted(CONFIDENCE_LABELS))
    parser.add_argument(
        "--current",
        default="",
        help="comma-separated labels the issue has now",
    )
    args = parser.parse_args(argv)

    current = tuple(label.strip() for label in args.current.split(",") if label.strip())
    try:
        result = compute(args.resolution, confidence=args.confidence, current=current)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
