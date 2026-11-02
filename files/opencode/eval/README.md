# Mock-Jira evaluation

Scores the OpenCode triage agent against the same fixtures BeeAI's e2e suite
uses, so the two harnesses can be compared on identical, offline input.

## Running

```bash
make start-opencode-mock      # gateway serves testing-jiras, MOCK_JIRA=true, DRY_RUN=true
make eval-opencode-mock       # enqueue the scored issues and print a scorecard
make eval-opencode-mock EVAL_REPEAT=3   # three passes, for verdict variance
```

`make eval-opencode-mock` writes a JSON report under `eval-results/` (ignored by
git) with per-issue verdict, target branch, cost and duration.

## Where the expectations come from

`ymir/agents/tests/e2e/test_triage.py` — parsed with `ast` at run time, so the
score cannot drift from what BeeAI asserts. Cases carrying `skip_reason` are
excluded by default (`--include-skipped` to override); two of them need
reproduction and one hits an auth-walled upstream.

Current set: 12 issues — 7 backport, 3 not-affected, 1 rebase, 1
postponed_dependency.

## What is mocked, and what is not

| Dependency | Mocked | Notes |
|---|---|---|
| issue fields, remote links, transitions, comments | yes | from `jiras/<KEY>` |
| JQL search | **key clauses only** | `key = X` / `key in (...)` answered from fixtures; anything else returns empty, as before |
| dev-status (linked repos/MRs) | yes, empty | an unmocked endpoint used to raise `NotImplementedError` |
| package repositories | **no** | clones hit live GitLab; use Tier 2 (below) for pre-fix state |

Because JQL only answers key clauses, both harnesses see "no work history".
That is fair but it is not the production condition: the real agent can find
existing MRs and duplicates. Treat the score as *routing quality on issue
content only*, not as a measure of reuse behaviour.

## Reading the scorecard

- `exact resolution` is the primary metric — the verdict the queue would act on.
- `target` is informational: expectations live in the fixture's `fix_version`,
  not in a branch name.
- `$` is the parent session cost from OpenCode; applicability subagents are
  billed on their own sessions and are not included.
- `FLIPPED` in the multi-run report is the verdict-variance signal.

## Tier 2 — pre-fix repositories

`make eval-opencode-mock` runs with `--pre-fix`, which pins clones to each
fixture's `pre_fix_ref`. Without it, backport cases mostly come back
`not-affected`, because today's branches already contain the fix: a first pass
scored 2/12 for exactly that reason, not because the agent was wrong.

How it works: the gateway already runs with
`GIT_CONFIG_GLOBAL=/git-repos/.mock_gitconfig`, so `prepare_mock_repos.py`
clones every needed repository into the git-repos volume as a base mirror, then
gives each issue its **own** bare clone containing only the branches that
fixture models, pinned to its `pre_fix_ref`. It rewrites the config before each
issue to point at that clone.

Per-issue clones are not cosmetic: with a single shared clone, a lookup for a
branch the current fixture does not model falls through to whatever ref the
previous issue left behind. RHEL-114607 asks for `rhel-10.2` (its fixture pins
`rhel-10.1` only) and inherited RHEL-177992's ref, so applicability inspected
the wrong source. Unmodelled branches now return "does not exist", which the
branch rules already know how to handle.

Only dist-git URLs are rewritten. Fedora, upstream and GitHub clones still go
to the network — which is what the agent should see, and what BeeAI's suite
does too.

## Provenance override

Pinning git is not enough on its own: the fixtures freeze Jira and the branches
at a past date, but Brew answers with *today's* candidate build, which already
contains the fix. A provenance-first agent would then correctly say
`not-affected` and disagree with every backport expectation.

`prepare_mock_repos.py` therefore also writes `.eval/provenance.json` into the
agent's work volume, and `build-provenance` honours it when the file is marked
`"eval_fixture": true`:

```json
{"eval_fixture": true, "issue": "RHEL-112546",
 "entries": {"libtiff": {"rhel-9.2.0": {"commit": "a983ca34…", "repository": "https://…"}}}}
```

The script then reports `status: resolved_from_fixture`, the pinned commit and
**no NVR**, with a warning that no build is implied. Production never has this
file, so real runs still read Brew.

## Known gap: per-case RHEL config overrides

Most fixtures also carry `zstream_override` / `rhel_config_override`, which
BeeAI applies in-process per test case. The harness applies neither, so
"older z-stream" detection and Y/Z-stream mapping follow today's config rather
than the fixture's. RHEL-114607 is the case this blocks: its fixture pins
`current_y_streams[10] = rhel-10.2` and the repo on `rhel-10.1`, so without the
override the branch looks absent and the run stops at `clarification-needed`
instead of `rebase`. Treat that case as unscoreable until the overrides are
applied; the gateway reads `MOCK_ZSTREAMS` at startup, so it needs a restart per
issue.

RHEL-186838 is a different kind of outlier: the agent escalates to
`clarification-needed` because the payload evidence is unavailable, where BeeAI
concludes `not-affected`. That is a judgement difference worth keeping in the
score rather than explaining away.
