---
name: version-mapping
description: Map a Jira fixVersion to the branch to actually work on, including older Z-streams, RHEL-first fixes, missing branches and modular packages
---

# Version mapping

Fix versions are ambiguous: `rhel-9.8` may be the current Y-stream or a stale
name for `rhel-9.8.z`, `rhel-10.2` and `rhel-10.2.z` are different questions,
RHEL 9 branches carry a `.0` suffix and RHEL 10 ones do not, and modular packages
use `stream-<module>-<stream>-rhel-<major>.<minor>.0`. Do not reason this out;
compute it.

## How

```
python3 /etc/opencode/skills/version-mapping/scripts/version_map.py \
  --fix-version rhel-8.10.z --component resource-agents \
  --needs-internal-fix --internal-branch-exists no
```

```json
{"fix_version": "rhel-8.10.z", "stream_kind": "current_z_stream",
 "needs_internal_fix": true, "internal_branch_exists": false,
 "internal_branch": "rhel-8.10.0",
 "target_branch": "c8s", "branch": "c8s",
 "resolution_reason": "RHEL-first CVE, but RHEL 8 has no Y-stream; the fix lands on c8s",
 "warnings": []}
```

Two flags matter and both are easy to skip:

- `--needs-internal-fix` — take it from the eligibility check
  (`check_cve_triage_eligibility` → `needs_internal_fix`).
- `--internal-branch-exists yes|no|unknown` — take it from
  `get_internal_rhel_branches` for the package. Only Z-stream, stream-first
  routing uses it.

## Rules it encodes

These mirror `ymir.agents.triage_agent._map_version_to_branch`, so the answer is
what production would do:

| Situation | Target |
|---|---|
| RHEL-first CVE (`needs_internal_fix`), major has a Y-stream | internal `rhel-X.Y[.0]` — created if absent |
| RHEL-first CVE, major is maintenance (no Y-stream, e.g. RHEL 8) | `c<N>s` |
| Older Z-stream | internal `rhel-X.Y[.0]` — created if absent |
| Current/upcoming Z-stream, stream-first, internal branch exists | internal `rhel-X.Y[.0]` |
| Current/upcoming Z-stream, stream-first, no internal branch | `c<N>s` (inherited) |
| Y-stream / any other | `c<N>s` |
| Modular | `stream-<module>-<stream>-rhel-X.Y.0`, namespace `rhel` or `centos-stream` |

## A missing branch is not a blocker

This is the mistake to avoid. Absence means the work happens elsewhere, not that
the request is unanswerable:

- RHEL-first and older-Z fixes **create** the internal branch in the backport
  stage. "Branch does not exist" is expected for a Z-stream that has not shipped.
- Stream-first fixes land on `c<N>s` and are inherited by the Z-stream.

Never escalate to `clarification-needed` because a branch is missing, and never
substitute a *different* minor branch. If `internal_branch_exists` is unknown and
the case needs it, look it up with `get_internal_rhel_branches` and re-run with
`--internal-branch-exists`; that is the only lookup required.

## How to use the result

- Clone and work on `target_branch` (also returned as `branch`).
- `stream_kind` and `needs_internal_fix` decide who owns the fix; `namespace`
  selects the forge project for modular packages.
- Treat `warnings` as a reason to state an assumption, not to stop — except an
  unparsed fix version, which is a genuine reason to ask.
