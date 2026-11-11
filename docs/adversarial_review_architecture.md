# Adversarial Review Agent — Architecture and Design

## Problem Statement

The triage agent's CVE applicability check can conclude that a package is
**not affected** by a CVE. Since the confidence work
(`AssessedApplicabilityResult`, `ymir_applicability_confidence_*` labels),
each conclusion carries an evidence-based self-rating — but a single agent
rating its own conclusion is a weak basis for closing a security tracker:

- **Correlated errors.** The applicability agent's confidence comes from the
  same model, prompt framing and context that produced the conclusion. Its
  known failure modes (unpatched-source fallback, binary-RPM subpackages,
  vendored copies in unexpected paths) are exactly the ones it is least
  likely to notice in itself.
- **Humans are the only close path today.** Every not-affected tracker waits
  for a maintainer to read the comment and close it manually, which does not
  scale with triage volume.

The long-term goal is to **auto-close high-confidence not-affected issues**.
The missing piece is a second, adversarially-framed opinion that materially
reduces the probability of a wrong close, plus a deterministic policy that
makes every close decision explainable after the fact.

**Failure asymmetry governs the design.** A wrong rebase or backport wastes a
maintainer's afternoon; a wrongly closed CVE is a security escape. Therefore:

> **Fail-safe principle:** absence of review is never approval. Any failure,
> skip, timeout or ambiguity in the review path must degrade to today's
> behavior (comment + confidence label, no close) — never to a close.

## Goals / Non-Goals

Goals:

- Adversarially verify not-affected conclusions **before** they are posted
  to Jira, so a refuted conclusion is never communicated as final.
- A deterministic, auditable auto-close gate whose every decision is a list
  of named blockers.
- Auto-close not-affected issues under a conservative gate, with first-class
  dry-run support.
- Full audit trail: trajectories, metrics, labels.

Non-goals (v1):

- Reviewing the *resolution choice* (rebase vs backport vs rebuild). A wrong
  resolution wastes work; it does not close a CVE. Future work.
- Calibrated probabilities. Confidence remains an evidence rating, as
  documented in `README-agents.md`.
- Automatic reopen. A human reopens if evidence arrives later.
- A separate queue-driven reviewer process. The module is built standalone
  so this migration is possible later (see Future Work).

## High-Level Architecture

The reviewer is a **new state inside `TriageWorkflow`**, entered only when
the applicability check ran and concluded not-affected. The source tree is
already on disk (`state.applicability_local_clone`,
`state.applicability_unpacked_sources`), so the reviewer inspects the exact
tree the applicability agent analyzed — no re-clone, and the conclusion is
challenged before anything reaches Jira.

CVE resolutions that would otherwise bypass the applicability check are
routed through it. In particular, a CVE that triage could not resolve
(`clarification-needed`) is checked against the shipped source first: if the
package is not affected, the result becomes `not-affected` and reaches the
reviewer and gate instead of stalling in clarification. Non-CVE issues keep
the direct path.

```
┌───────────────────┐   not-affected        ┌──────────────────────────┐
│ check_cve_        │──────────────────────▶│ adversarial_review       │
│ applicability     │  (check ran,          │ (new state)              │
│                   │   not skipped)        │                          │
│                   │                       │  • build reviewer agent  │
│ ▼ other paths ───▶│                       │  • verify-then-attack    │
│ rebuild/rebase/   │                       │    on the same tree      │
│ comment_in_jira   │                       │  • AdversarialReview-    │
└───────────────────┘                       │    Result                │
                                            └───────────┬──────────────┘
                                                        │
                                            upheld      │      overturned
                                            ┌───────────▼──────────────┐
                                            │ keep NOT_AFFECTED        │
                                            │                          │
                                            │ overturned ──────────────┼──▶ downgrade to
                                            │                          │    CLARIFICATION_NEEDED
                                            └───────────┬──────────────┘    (both positions
                                                        │                   in the comment,
                                                        ▼                   existing routing)
                                            ┌──────────────────────────┐
                                            │ comment_in_jira          │
                                            │  • comment (+ review     │
                                            │    outcome)              │
                                            │  • confidence + review   │
                                            │    labels                │
                                            │  • auto-close gate ──────┼──▶ transition to
                                            │    (deterministic)       │    Closed +
                                            │  • close if eligible     │    ymir_autoclosed
                                            └──────────────────────────┘
```

The module layout mirrors `cve_applicability_agent.py`:

- `ymir/agents/adversarial_review_agent.py` —
  `create_adversarial_review_agent()` factory and
  `build_adversarial_review_prompt()`.
- `ymir/common/models.py` — `ReviewVerdict`, `ClaimCheck`,
  `AdversarialReviewResult`, `AutoCloseBlocker`, `AutoCloseDecision`.
- `ymir/agents/autoclose.py` — `evaluate_autoclose_decision()` (pure
  function), its input model, the opt-in package list and the protected
  floor; a sibling of the consolidation modules rather than more mass in
  `triage_agent.py`.
- `ymir/agents/triage_agent.py` — the `adversarial_review` state, label
  helpers, close step in `comment_in_jira`.

## The Review Agent

### Construction

`create_adversarial_review_agent()` follows the applicability agent factory,
with two deliberate differences:

- **Role**: adversarial security reviewer — instructed to try to *prove the
  package IS affected*, not to balanced-assess it.
- **Model**: `get_chat_model()` by default, overridable via
  `REVIEW_CHAT_MODEL`. Running the reviewer on a different model family than
  the applicability agent decorrelates their errors; same-family models
  repeat each other's mistakes.

Tools: `ThinkTool`, `ViewTool`, `SearchTextTool`, `RunShellCommandTool`
(same `local_tool_options` — the clone is on disk), plus gateway tools
`get_jira_details`, `get_maintainer_rules`, `get_shared_rules`. Sibling
z-stream context is passed *into the prompt* from
`state.cve_eligibility_result.pending_zstream_issues` rather than re-queried.

### Protocol: verify-then-attack

The reviewer sees the applicability agent's full result (justification
category, explanation, confidence and reason) and must work in two phases:

1. **VERIFY** — every concrete evidence claim in the applicability
   explanation is checked against the tree and rated
   `verified | refuted | unverifiable` with the evidence found. Applicability
   claims are mostly mechanically checkable: "files X/Y absent" (`find`),
   "BuildNodeJS=false" (read the spec), "patch does not apply"
   (`git apply --check`).
2. **ATTACK** — actively hunt refuting evidence along the applicability
   agent's known failure modes (the prompt's attack menu):
   - prep failed → unpatched Source0: do the **downstream dist-git patches**
     already fix the vulnerable code? Do the declared `Source*` vendor/bundled
     archives (extracted into the tree on fallback) actually contain the
     component?
   - source-level "component not present" vs **binary RPM subpackages** that
     do ship the component (`rpmspec`/subpackage definitions)
   - **vendored/bundled copies** in non-obvious paths (`find`, not manifests)
   - the **fix patch applying cleanly** to the tree = strong evidence of
     *affected*
   - maintainer comments or linked issues contradicting the conclusion
     (`get_jira_details`)
   - sibling z-stream issues for the same CVE marked affected (passed in
     from the eligibility result)

Then it emits its **own conclusion** — not agreement, an independent
assessment.

### Output model

```python
class ReviewVerdict(str, Enum):
    UPHELD = "upheld"
    OVERTURNED = "overturned"
    INCONCLUSIVE = "inconclusive"

class ClaimCheck(BaseModel):
    claim: str
    status: Literal["verified", "refuted", "unverifiable"]
    evidence: str

class AdversarialReviewResult(BaseModel):
    verdict: ReviewVerdict
    independent_conclusion: Literal["affected", "not_affected", "inconclusive"]
    claim_checks: list[ClaimCheck]
    counter_evidence: str | None
    confidence: ConfidenceAssessment  # reuse the existing model
```

### Verdict handling

| Verdict | Resolution | Comment | Auto-close eligible |
|---|---|---|---|
| `UPHELD` | stays `NOT_AFFECTED` | records independent verification | yes (subject to gate) |
| `OVERTURNED` | downgraded to `CLARIFICATION_NEEDED` | shows **both** positions: applicability explanation + reviewer counter-evidence | never |
| `INCONCLUSIVE` | stays `NOT_AFFECTED` | notes open questions | never |

An overturned verdict **never flips the resolution to affected** — two agents
disagreeing is human territory. The downgrade rides the existing
clarification-needed routing (`CLARIFICATION_NEEDED_QUEUE`), and
clarification-needed is in `_should_update_jira`'s always-commented set, so
the disagreement is always visible to the requester.

## The Auto-Close Gate (deterministic)

`evaluate_autoclose_decision()` is a **pure function** of its inputs and the
package policy (the opt-in list from `AUTO_CLOSE_PACKAGES` and the protected
floor from `AUTO_CLOSE_SENSITIVE_PACKAGES`, both passable explicitly so the
gate stays testable): inputs in,
`AutoCloseDecision{eligible: bool, blockers: list[AutoCloseBlocker]}` out.
No LLM judges whether closing is safe — policy lives in versioned code.
Every non-close is explainable by named blockers; every close is reproducible
from the recorded inputs.

Auto-close is **opt-in per package**. An empty `AUTO_CLOSE_PACKAGES` (the
default) means no issue is ever closed, so turning on the platform-wide
status-change flag cannot by itself close anything. The built-in protected
list is a floor that opting a package in cannot override: listing `kernel`
blocks it twice rather than closing it.

V1 blockers (all must be absent to close):

| Blocker | Condition |
|---|---|
| `NO_REVIEW` | applicability check skipped, review skipped or failed — **the fail-safe catch-all** |
| `CONFIDENCE` | applicability confidence is not `high` |
| `REVIEW` | review verdict is not `upheld` |
| `REVIEW_CONFIDENCE` | review confidence is not `high` |
| `PREP_FALLBACK` | sources were analyzed unpatched (`applicability_used_fallback`) |
| `SEVERITY` | severity is not `low` or `moderate` — **Critical and Important are never auto-closed in v1**; unknown severity blocks |
| `SENSITIVE_PACKAGE` | package is on the protected floor (kernel, openssl, glibc, …; constant list, extendable via env, never removable by opting in) |
| `PACKAGE_NOT_OPTED_IN` | package is not listed in `AUTO_CLOSE_PACKAGES` — **the opt-in gate**; an empty list blocks every package |
| `SIBLINGS` | pending z-stream sibling issues exist for the same CVE |
| `DUPLICATE` | an open duplicate tracker exists |
| `MULTI_CVE` | the issue names more than one CVE; only the first is analyzed, so the rest would close unassessed |
| `NO_COMMENT` | the justification comment was not posted this run — a close requires a visible justification, an explicit precondition rather than the incidental property that `NOT_AFFECTED` is always commented |
| `STATUS_CHANGES_DISABLED` | the platform-wide `JIRA_ALLOW_STATUS_CHANGES` flag is off, so no status transition is possible |
| `STATE_CHANGED` | pre-close status re-check found the issue no longer in the expected state (a human touched it); emitted by the close step, never by the gate |

Severity comes from the Jira custom field, read via `get_jira_details`
during the review step — no new tooling.

## Close Mechanics

Order matters — **comment first, close second**, so the justification is
visible the moment the issue closes:

1. Post the not-affected comment (existing behavior), with the review
   outcome appended.
2. Write confidence + review labels (existing pattern).
3. Evaluate the gate. If eligible and not `dry_run`:
   - re-read the issue status; skip with `STATE_CHANGED` if it is already
     `Closed`, `Done` or `Release Pending`,
   - `change_jira_status` to `JIRA_CLOSE_STATUS` (default `Closed`), passing
     `JIRA_CLOSE_RESOLUTION` (default `Not a Bug`) in the same transition
     payload — `ChangeJiraStatusTool` gained an optional `resolution` field
     for this,
   - add the `ymir_autoclosed` label.
4. A failed transition logs and sets the retry path; the conclusion is
   already communicated, only the close is missing.

`JIRA_ALLOW_STATUS_CHANGES` is a pre-existing platform guard on **all**
status transitions (it defaults to `false` in the deployment configmap). The
close goes through `tasks.change_jira_status`, which respects it; the gate
receives it as an explicit input and returns `STATUS_CHANGES_DISABLED` when
it is off. This is not a new auto-close control — it is a deployment
prerequisite shared with the other agents.

**Interaction with the reproducer agent:** reproducer tests are enqueued for
not-affected issues too (a confirming test is valuable there). Auto-close
does not wait for the reproducer result — the test MR landing on a closed
issue is fine — but a reproducer test that later *fails* on an auto-closed
issue is the strongest possible late signal that the close was wrong. v1:
metrics/log hook only; automated reopen is future work.

## Dry-Run Semantics

`DRY_RUN` is a first-class mode for the entire path, following the existing
triage pattern (`comment_in_jira` returns early, label helpers no-op): the
full analysis runs, nothing is written to Jira.

- The applicability check and the adversarial review run normally — the
  review only reads (local tree, `get_jira_details`, maintainer rules), so
  it is safe in dry-run.
- The gate is evaluated exactly as in a real run.
- The comment, confidence/review labels and the close transition are
  suppressed; each suppressed action is logged instead — including the gate
  decision ("would close", or the blockers that prevented it).

This makes `DRY_RUN=true` the primary way to exercise the complete
review → gate → close path against real issues (and mock Jira in dev)
without side effects.

## Labels and State Machine

Review-outcome labels get exactly the confidence-label treatment from the
confidence work — they are assessment metadata, not workflow state:

- `ymir_review_upheld` / `ymir_review_overturned` / `ymir_review_inconclusive`:
  mutually exclusive, replaced on each completed run, dry-run aware, written
  independently of comment suppression, **non-terminal** in
  `_terminal_ymir_labels` and **ignorable** in
  `_find_stale_in_flight_label` (a worker killed between the review write
  and the terminal label must still be retried).
- `ymir_autoclosed`: audit marker for closed issues; classified terminal.

`jira_label_workflow_routing.md` label tables and `README-agents.md` gain
sections for all of the above.

## Failure Semantics

| Failure | Behavior |
|---|---|
| Review agent raises / times out / invalid output | `adversarial_review_skipped = True`; today's behavior: comment + confidence label, no close (`NO_REVIEW`) |
| Gate evaluation error | no close, logged |
| Close transition fails after comment | retry path (`ymir_retry_needed`); conclusion still communicated |
| Worker killed between comment and close | issue open with not-affected comment + labels; a `ymir_todo` re-run re-triages |
| Reviewer disagrees (`OVERTURNED`) | clarification-needed for a human; never a close, never a silent flip |
| Triage LLM emits `NOT_AFFECTED` directly | no applicability check ran, so `adversarial_review_skipped = True`; the comment states the gap and the gate blocks the close with `NO_REVIEW` |

## Observability

- Reviewer runs carry `GlobalTrajectoryMiddleware` trajectories (Phoenix),
  tagged with issue and verdict for offline analysis.
- Every gate decision is logged with its blocker list, and every close with
  its status and resolution; the comment, labels and trajectories provide a
  complete audit trail of why an issue was or was not closed.
- Custom counters (`autoclose_eligible`, `autoclose_executed`,
  `autoclose_blocked{blocker}`) are future work — the repo currently has no
  metrics sink for agent-level counters.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `REVIEW_CHAT_MODEL` | `CHAT_MODEL` | reviewer model override (decorrelation) |
| `AUTO_CLOSE_PACKAGES` | — (empty) | **opt-in list**: packages allowed to auto-close; empty closes nothing |
| `AUTO_CLOSE_SENSITIVE_PACKAGES` | — | extends the built-in protected floor (never removable) |
| `JIRA_CLOSE_STATUS` | `Closed` | target status |
| `JIRA_CLOSE_RESOLUTION` | `Not a Bug` | target resolution |
| `JIRA_ALLOW_STATUS_CHANGES` | `false` (platform default) | pre-existing platform guard; auto-close only runs when it is `true` |

## Testing

Tests first, `flexmock` for mocking, per `CONTRIBUTING.md`:

- **Unit — models**: `AdversarialReviewResult` validation (verdict literals,
  non-empty evidence, claim-check statuses).
- **Unit — prompt builder**: attack menu present, applicability claims and
  paths passed through, prep-failure context included.
- **Unit — gate truth table**: parametrize every blocker; assert
  eligible only when all inputs are green; assert `NO_REVIEW` on every
  skip/failure combination (the fail-safe property).
- **Unit — labels**: dry-run and user-triggered semantics, replacement of
  previous review labels, non-terminal classification, recovery-ignorable
  classification.
- **Unit — fail-safe**: review raises → comment posted, confidence labels
  written, no transition call.
- **Unit — overturn**: resolution downgraded to clarification-needed, both
  positions in the comment, clarification queue routing, no close.
- **Unit — close mechanics**: comment-before-close ordering, pre-close
  status re-check, idempotency (no double transition), dry-run suppression
  (gate evaluated and logged, no transition call).
- **Golden adversarial test**: a temporary source tree contains the
  vulnerable code while the applicability result claims "Component not
  Present". The test asserts the prompt exposes the false claim, that the
  reviewer's own `SearchTextTool` can find the contradiction on that tree,
  and that a parsed `OVERTURNED` review makes the auto-close gate refuse the
  close despite the applicability agent's high self-rating. The reviewer's
  LLM decision itself is exercised by the e2e/eval path (the repo's
  `RUN_LLM_JUDGE` pattern), not by a unit test.
- **Workflow-level**: follow the
  `test_branch_creation_notice_uses_final_resolution` pattern — mocked
  agents, label/comment recorders, parametrized dry-run.
- `make check-in-container`; full-pipeline dry runs with mock Jira
  (`MOCK_JIRA=true DRY_RUN=true`).

## Operations and Rollout

The feature ships complete (review + gate + close) and active — the gate
itself is the control; there is no separate enable flag:

1. **Dev/staging**: exercise the full path with `DRY_RUN=true` and mock
   Jira (`MOCK_JIRA=true`) — the review runs, the gate evaluates, and the
   logs show exactly what would be commented, labeled and closed.
2. **Calibrate** (recommended before or alongside the first real closes):
   a backfill sweep over historical not-affected issues (the `ymir/sweep`
   pattern, fresh clone+prep per issue — the original workspaces are gone)
   measures the overturn rate on real conclusions. If it is high, tighten
   the gate requirements — not a runtime switch.
3. **Audit**: weekly sampled human review of auto-closed issues (escape
   rate), plus overturn-rate trends from metrics.

What stands between a not-affected conclusion and a closed issue is the
gate's requirements themselves: dual high confidence (applicability and
review), fully-prepared sources, the severity ceiling, the per-package
opt-in list, the protected floor, and the fail-safe `NO_REVIEW` catch-all —
backed by the calibration and audit above.

## Future Work

- Migrate the reviewer to a separate queue-driven agent if its latency on
  the not-affected path ever matters — the module boundary already allows it.
- A/B verify-then-attack vs blind dual-review (independent re-assessment,
  conclusion hidden) using the backfill sweep, to pick the mode by
  errors-caught per cost.
- Extend review to the triage resolution choice (rebase vs backport vs
  rebuild).
- Automated reopen when a reproducer test fails on an auto-closed issue, or
  when contradicting evidence lands in the tracker.
- Learn the sensitive-package list from overturn data instead of curating it.
