---
name: build-provenance
description: Resolve which Brew build shipped for a package branch and which source commit it was built from, when provenance would otherwise be reported as unverified
---

# Build provenance

Use this whenever a conclusion depends on **what actually shipped** rather than
what the branch currently contains: CVE applicability confidence, rebuild
readiness, or any claim of the form "the released package is/is not affected".

## When to use it

- You are about to report `not-affected` and cannot say which build you judged.
- A triage or rebuild result says "shipped provenance unverified".
- You need the NVR or source commit for a branch before comparing sources.

## How

```
python3 /etc/opencode/skills/build-provenance/scripts/provenance.py \
  --package <name> --branch <dist-git branch>
```

It prints JSON and exits non-zero when nothing resolved:

```json
{"status": "resolved",
 "build": {"nvr": "ignition-2.22.0-2.el9", "build_id": 2029097, "tag": "rhel-9.7.0-candidate"},
 "source": {"repository": "git+https://pkgs.devel.redhat.com/git/rpms/ignition",
            "commit": "3fdb9f0cbb2a7b52bd212da91f5e863289eca23f"},
 "candidates": [{"tag": "...-candidate", "nvr": "..."}],
 "warnings": []}
```

It checks `<branch>-candidate` then `<branch>-z-candidate`, picks the newest by
EVR, and reports every candidate it saw. Pass `--tag` to override.

## What it can and cannot tell you

- **Can**: the NVR, its build id, the source commit Brew recorded, and whether a
  source commit exists at all (`resolved_without_source` when it does not).
- **Cannot**: the buildroot contents or the shipped RPM payload. Those need
  authenticated access. The output says so explicitly — do not paper over it.
- It is **read-only and unauthenticated**. It never writes and never downloads.

## Eval fixtures

When `.eval/provenance.json` exists in the work directory and is marked
`"eval_fixture": true`, the script reports the fixture's pinned commit instead
of Brew (`status: resolved_from_fixture`, no NVR). That file is written only by
`files/opencode/eval`, which uses the mock-Jira fixtures: they freeze Jira and
git at a past date while Brew keeps serving today's build, so without it every
backport case would look already fixed. Production never has the file.

## How to use the result

Quote the NVR and commit in your answer, and compare that commit with the tree
you inspected. If they differ, you analysed the wrong source and your verdict is
provisional. If `status` is `no_build`, the branch has no candidate build — that
is a fact worth reporting, not a failure to work around.
