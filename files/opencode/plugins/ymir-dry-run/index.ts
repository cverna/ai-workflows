import { Plugin } from "@opencode/plugin"

/**
 * Enforces Ymir's DRY_RUN contract inside the OpenCode server.
 *
 * Layer 0 is the hard floor: mcp-gateway refuses every mutating GitLab, Jira,
 * errata and lookaside write on its own. This plugin is the harness half: it
 * tells the model that dry run is active, blocks the mutating calls that would
 * otherwise leave the box, and refuses local publishes.
 *
 * It deliberately *stubs* rather than fails. A hard denial makes an agent
 * abandon the run; a successful no-op keeps the workflow traceable and lets the
 * model report what it would have published.
 *
 * Set YMIR_DRY_RUN_DEBUG=true to log the registered tool ids and every tool
 * invocation, which is how the names below were verified.
 */

const DRY_RUN = process.env.DRY_RUN === "true"
const DEBUG = process.env.YMIR_DRY_RUN_DEBUG === "true"

// Normalised MCP tool ids: <server>_<tool>. `-` is a permitted character, so
// the server name keeps its hyphen.
//
// `fork_repository` is deliberately absent: the gateway's dry-run no-op returns
// the original repository URL, which the agent still needs in order to clone and
// inspect sources. Blocking it here would only remove a useful result.
const BLOCKED_TOOLS = new Set([
  "ymir-privileged_push_to_remote_repository",
  "ymir-privileged_open_merge_request",
  "ymir-privileged_add_merge_request_comment",
  "ymir-privileged_add_blocking_merge_request_comment",
  "ymir-privileged_add_merge_request_labels",
  "ymir-privileged_set_merge_request_reviewers",
  "ymir-privileged_retry_pipeline_job",
])

// The container holds no git credentials, so these would fail anyway. This
// catches a future change that mounts a token.
const BLOCKED_SHELL = [
  { label: "git push", re: /(^|[;&|]\s*)git\s+push\b/ },
  { label: "git send-email", re: /(^|[;&|]\s*)git\s+send-email\b/ },
  { label: "centpkg push/commit", re: /(^|[;&|]\s*)centpkg\s+(push|commit)\b/ },
  { label: "rhpkg push/commit", re: /(^|[;&|]\s*)rhpkg\s+(push|commit)\b/ },
  { label: "osc commit", re: /(^|[;&|]\s*)osc\s+commit\b/ },
]

const DRY_RUN_NOTICE = `DRY RUN IS ACTIVE.

- Jira, GitLab, errata and lookaside writes are suppressed by the gateway.
  Merge requests cannot be created and branches cannot be pushed.
- Local work still runs for real: editing files, git commits, %prep, SRPM
  builds and Copr builds are unaffected, because a dry run must still prove
  the change builds.
- When you reach a step that would publish, do not claim it succeeded. State
  plainly what you would have pushed and which merge request you would have
  opened, so a reviewer can judge the result.

A tool result of the form "Dry run, not <action> (this is expected, not an
error)" is the expected outcome, not a failure. Carry on.`

export default Plugin.define({
  id: "ymir-dry-run",
  async setup(ctx) {
    if (!DRY_RUN) {
      if (DEBUG) console.log("[ymir-dry-run] DRY_RUN is not set; no suppression active")
      return
    }

    console.log("[ymir-dry-run] DRY_RUN active; mutating tools will be suppressed")

    if (DEBUG) {
      try {
        const ids = (await ctx.tool.list()).map((tool) => tool.id)
        const matched = ids.filter((id) => BLOCKED_TOOLS.has(id))
        console.log(`[ymir-dry-run] ${ids.length} tools at setup; matched ${matched.length}`)
        console.log(`[ymir-dry-run] ids: ${ids.join(", ")}`)
        const unmatched = ids.filter(
          (id) => /privileged/i.test(id) && !BLOCKED_TOOLS.has(id),
        )
        if (unmatched.length) {
          console.log(`[ymir-dry-run] unmatched privileged tools: ${unmatched.join(", ")}`)
        }
      } catch (error) {
        console.log(`[ymir-dry-run] could not list tools: ${String(error)}`)
      }
    }

    // 1. Re-state dry run on every model call, so compaction cannot drop it.
    await ctx.session.hook("context", (event) => {
      event.system.push({ type: "text", text: DRY_RUN_NOTICE })
    })

    // 2. Suppress mutating MCP tools.
    await ctx.tool.hook("execute.before", (event) => {
      if (DEBUG) {
        console.log(`[ymir-dry-run] execute.before tool=${JSON.stringify(event.tool)}`)
      }
      if (!BLOCKED_TOOLS.has(event.tool)) return
      console.log(
        `[ymir-dry-run] suppressed ${event.tool} input=${JSON.stringify(event.input)}`,
      )
      throw new Error(
        `Dry run, not running ${event.tool} (this is expected, not an error). ` +
          `Report what this call would have done instead of retrying it.`,
      )
    })

    // 3. Refuse local publishes.
    await ctx.shell.hook("create.before", (event) => {
      const blocked = BLOCKED_SHELL.find((entry) => entry.re.test(event.command))
      if (!blocked) return
      console.log(`[ymir-dry-run] blocked shell: ${event.command}`)
      throw new Error(
        `Dry run, refusing to run "${blocked.label}" (this is expected, not an error).`,
      )
    })
  },
})
