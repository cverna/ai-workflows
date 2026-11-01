---
name: version-mapping
description: Map a Jira fixVersion to its RHEL dist-git branch, stream kind and namespace, including older Z-streams and modular branches
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
  --fix-version rhel-9.8 --component ignition
```

```json
{"fix_version": "rhel-9.8", "normalized": "rhel-9.8.z",
 "stream_kind": "current_z_stream", "older_zstream": false,
 "branch": "rhel-9.8.0", "namespace": "rhel", "modular": false,
 "current_y_stream": "rhel-9.10", "current_z_stream": "rhel-9.8.z",
 "warnings": []}
```

Add `--summary "<issue summary>"` when the package might be modular. Pass
`--rhel-config` if the config is not at `rhel-config.json` in the working
directory or `/home/beeai/rhel-config.json`.

## What it reads

`rhel-config.json` supplies `current_y_streams`, `current_z_streams` and, by
difference, the maintenance majors. The parsing and branch construction are
`ymir.common.version_utils`, the same code the BeeAI agents use, so the answer
matches production routing.

## How to use the result

- Use `branch` for clone and branch checks. If it does not exist in the remote,
  report that — do not pick a neighbouring branch.
- `stream_kind` decides routing: `older_z_stream` and `y_stream` need an internal
  fix first; `current_z_stream` may inherit from CentOS Stream.
- Treat any `warnings` entry as a reason to lower confidence, especially
  "RHEL N is in maintenance" and a missing `current_z_streams` entry.
