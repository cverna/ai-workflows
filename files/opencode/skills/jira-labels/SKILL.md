---
name: jira-labels
description: Compute the exact valid Ymir label change for a triage resolution instead of naming labels yourself
---

# Jira labels

Any time you would write a label on a Jira issue — or describe one in a proposed
comment — compute it first. Model-written label names are wrong often enough to
matter: `ymir_not_affected` was proposed twice; the real label is
`ymir_triaged_not_affected`. Jira accepts any string silently, so a wrong label
does not fail, it just leaves the issue in a state nothing downstream recognises.

## How

```
python3 /etc/opencode/skills/jira-labels/scripts/labels.py \
  --resolution not-affected --confidence medium \
  --current "CVE-2026-4421,Security,ymir_triaged_rebase"
```

```json
{"add": ["ymir_applicability_confidence_medium", "ymir_triaged_not_affected"],
 "remove": ["ymir_triaged_rebase"],
 "unchanged_ymir": [],
 "preserve": ["CVE-2026-4421", "Security"],
 "warnings": []}
```

`--resolution` takes the same values as the result contract: `rebase`,
`backport`, `rebuild`, `not-affected`, `clarification-needed`,
`open-ended-analysis`, and the four `postponed_*` values. `--confidence` is
optional; omit it when applicability was not assessed.

## Rules it encodes

- Exactly one triage label is added; every other triage label is removed.
- At most one confidence label survives, and all three are cleared when
  confidence is absent.
- It only ever removes labels it owns. Security, CVE, SLA, sustaining and any
  other label is passed through untouched in `preserve`.
- Unknown `ymir_*` labels already on the issue are reported in `warnings` and
  left alone rather than silently deleted.

## How to use the result

Apply `add` and `remove` verbatim; never invent a label beyond them. If the
script exits non-zero it means the resolution is not one Ymir understands —
report that instead of guessing a label.
