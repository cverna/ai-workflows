import asyncio
import logging
import os
import sys
import traceback
from enum import Enum
from typing import Literal, TypeAlias, Union

from pydantic import BaseModel, Field

from beeai_framework.backend import ChatModel
from beeai_framework.errors import FrameworkError
from beeai_framework.workflows.agent import AgentWorkflow, AgentWorkflowInput
from beeai_framework.tools.think import ThinkTool
from tools.commands import RunShellCommandTool
from tools.patch_validator import PatchValidatorTool
from utils import redis_client, mcp_tools
from observability import setup_observability

logger = logging.getLogger(__name__)


# Resolution enum
class Resolution(Enum):
    REBASE = "rebase"
    BACKPORT = "backport"
    CLARIFICATION_NEEDED = "clarification-needed"
    NO_ACTION = "no-action"
    ERROR = "error"


# Data models for different resolution types
class RebaseData(BaseModel):
    package: str = Field(description="Package name")
    version: str = Field(description="Target version")
    branch: str = Field(description="Target branch")
    jira_issue: str = Field(description="Jira issue identifier")


class BackportData(BaseModel):
    package: str = Field(description="Package name")
    branch: str = Field(description="Target branch")
    patch_url: str = Field(description="URL or reference to the source of the fix")
    justification: str = Field(
        description="Clear explanation of why this patch fixes the issue"
    )
    jira_issue: str = Field(description="Jira issue identifier")


class ClarificationNeededData(BaseModel):
    findings: str = Field(description="Summary of the investigation")
    additional_info_needed: str = Field(description="Summary of missing information")
    jira_issue: str = Field(description="Jira issue identifier")


class NoActionData(BaseModel):
    reasoning: str = Field(
        description="Reason why the issue is intentionally non-actionable"
    )
    jira_issue: str = Field(description="Jira issue identifier")


class ErrorData(BaseModel):
    details: str = Field(description="Specific details about an error")
    jira_issue: str = Field(description="Jira issue identifier")


class TriageResult(BaseModel):
    resolution: Resolution = Field(description="Triage resolution")
    data: Union[
        RebaseData, BackportData, ClarificationNeededData, NoActionData, ErrorData
    ] = Field(description="Associated data")


class TriageMultiAgentWorkflow:
    def __init__(self) -> None:
        self.llm = ChatModel.from_name(os.getenv("CHAT_MODEL"))

        # Create multi-agent workflow
        self.workflow = AgentWorkflow(name="TriageMultiAgentWorkflow")

        # Add specialized agents
        self._setup_agents()

    def _setup_agents(self, mcp_tools_list=None) -> None:
        """Setup specialized agents for different aspects of triage"""

        # Prepare tools for JiraAnalyzer (include MCP tools if provided)
        jira_analyzer_tools = [ThinkTool()]
        if mcp_tools_list:
            jira_analyzer_tools.extend(mcp_tools_list)
            logger.info(f"Added {len(mcp_tools_list)} MCP tools to JiraAnalyzer")

        # Jira Issue Analyzer Agent
        self.workflow.add_agent(
            name="JiraAnalyzer",
            role="A specialized agent for analyzing Jira issues",
            instructions="""
            You are an expert at analyzing Jira issues for RHEL packages. Your role is to thoroughly analyze the Jira issue and extract all relevant information.

            **Analysis Steps:**
            1. Open the Jira issue and thoroughly analyze it:
               * Extract key details from the title, description, fields, and comments
               * **CRITICAL: Check the "Issue Links" section for any GitHub commits, pull requests, or upstream references**
               * **CRITICAL: Check the "remote_links" field for external links to GitHub PRs, issues, or other upstream references**
               * Pay special attention to comments as they often contain crucial information such as:
                 - Additional context about the problem
                 - Links to upstream fixes or patches
                 - Clarifications from reporters or developers
               * Look for keywords indicating the root cause of the problem
               * Identify specific error messages, log snippets, or CVE identifiers
               * Note any functions, files, or methods mentioned
               * **Pay special attention to any direct links to fixes provided in the issue, especially in Issue Links and Remote Links**

            2. Identify the package name that must be updated:
               * Determine the name of the package from the issue details (usually component name)
               * Be precise - this is critical for the next steps
               * Look for package names in the component field, description, or comments

            3. Identify the target branch for updates:
               * Look at the fixVersion field in the Jira issue to determine the target branch
               * Apply the mapping rule: fixVersion named rhel-N maps to branch named cNs
               * This branch information will be needed for both rebases and backports

            **Output Format:**
            Return your analysis in JSON format with these exact fields:
            - package_name: The exact package name identified
            - target_branch: The target branch from fixVersion mapping
            - direct_links: Array of any direct links to fixes found in the issue (including GitHub commits, pull requests, or upstream references from Issue Links and Remote Links)
            - keywords: Array of keywords indicating the root cause
            - root_cause: Detailed description of the root cause

            IMPORTANT: You MUST use the get_jira_details tool to access the actual Jira issue data. Do not make assumptions based on the issue number alone.
            """,
            llm=self.llm,
            tools=jira_analyzer_tools,
        )

        # Package Validator Agent
        self.workflow.add_agent(
            name="PackageValidator",
            role="A specialized agent for validating package information",
            instructions="""
            You are an expert at validating package information for RHEL. Your role is to verify that the identified package and branch exist and are accessible.

            **Validation Steps:**
            1. Verify that the identified package exists in GitLab repositories:
               * Run: `git ls-remote https://gitlab.com/redhat/centos-stream/rpms/<package_name>`
               * A successful command (exit code 0) confirms the package exists
               * If the package does not exist, re-examine the Jira issue for the correct package name

            2. Confirm branch existence and validity:
               * Verify the target branch exists on GitLab
               * Check if the branch is accessible and valid

            3. Validate package naming conventions:
               * Ensure the package name follows RHEL naming conventions
               * Check for any obvious naming issues

            **Output Format:**
            Return validation results in JSON format with these exact fields:
            - package_exists: boolean indicating if package exists
            - branch_exists: boolean indicating if branch exists
            - validation_status: "SUCCESS" or "FAILED"
            - error_message: null if successful, error details if failed

            Use the RunShellCommandTool to execute git commands for validation.
            
            IMPORTANT: Base your validation on the actual package name and branch identified by the JiraAnalyzer agent.
            """,
            llm=self.llm,
            tools=[ThinkTool(), RunShellCommandTool()],
        )

        # Fix Investigator Agent
        self.workflow.add_agent(
            name="FixInvestigator",
            role="A specialized agent for investigating potential fixes",
            instructions="""
            You are an expert at investigating potential fixes for RHEL issues. Your role is to systematically search for and validate upstream fixes.

            **Investigation Steps:**

            1. **Deep Analysis of the Issue:**
               * Use the details extracted from the JiraAnalyzer's analysis
               * Focus on keywords and root cause identification
               * **CRITICAL: If the JiraAnalyzer found direct_links, prioritize those as your primary leads**
               * If the Jira issue already provides a direct link to the fix, use that as your primary lead
                 (e.g. in the commit hash field or comment)

            2. **Systematic Source Investigation:**
               * Identify the official upstream project and corresponding Fedora package source
               * Even if the Jira issue provides a direct link to a fix, you need to validate it
               * When no direct link is provided, you must proactively search for fixes - do not give up easily
               * Using the details from your analysis, search these sources:
                 - Bug Trackers (for fixed bugs matching the issue description)
                 - Git / Version Control (for commit messages, using keywords, CVE IDs, function names, etc.)
               * Be thorough in your search - try multiple search terms and approaches based on the issue details
               * Advanced investigation techniques:
                 - If you can identify specific files, functions, or code sections mentioned in the issue,
                   locate them in the source code
                 - Use git history (git log, git blame) to examine changes to those specific code areas
                 - Look for commits that modify the problematic code, especially those with relevant keywords in commit messages
                 - Check git tags and releases around the time when the issue was likely fixed
                 - Search for commits by date ranges when you know approximately when the issue was resolved
                 - Utilize dates strategically in your search if needed, using the version/release date of the package
                   currently used in RHEL
                   - Focus on fixes that came after the RHEL package version date, as earlier fixes would already be included
                   - For CVEs, use the CVE publication date to narrow down the timeframe for fixes
                   - Check upstream release notes and changelogs after the RHEL package version date

            3. **Validate the Fix and URL:**
               * Use the PatchValidatorTool to fetch content from any patch/commit URL you intend to use
               * The tool will verify the URL is accessible and not an issue reference, then return the content
               * Once you have the content, you must validate two things:
                 1. **Is it a patch/diff?** Look for diff indicators like:
                    - `diff --git` headers
                    - `--- a/file +++ b/file` unified diff headers
                    - `@@...@@` hunk headers
                    - `+` and `-` lines showing changes
                 2. **Does it fix the issue?** Examine the actual code changes to verify:
                    - The fix directly addresses the root cause identified in your analysis
                    - The code changes align with the symptoms described in the Jira issue
                    - The modified functions/files match those mentioned in the issue
               * Only proceed with URLs that contain valid patch content AND address the specific issue
               * If the content is not a proper patch or doesn't fix the issue, continue searching for other fixes

            **Output Format:**
            Return investigation results in JSON format with these exact fields:
            - found_fixes: Array of found fixes with commit details
            - patch_urls: Array of validated patch URLs
            - validation_results: Array of validation results for each URL
            - rebase_candidate: boolean indicating if rebase is appropriate
            - backport_candidate: boolean indicating if backport is appropriate

            Use the PatchValidatorTool to validate patch URLs and content.
            
            IMPORTANT: Base your investigation on the actual package name, branch, and issue details identified by the JiraAnalyzer agent.
            """,
            llm=self.llm,
            tools=[ThinkTool(), PatchValidatorTool()],
        )

        # Decision Maker Agent
        self.workflow.add_agent(
            name="DecisionMaker",
            role="A specialized agent for making triage decisions",
            instructions="""
            You are an expert at making triage decisions for RHEL issues. Your role is to analyze all collected information and determine the most appropriate resolution.

            **Decision Guidelines:**

            You must decide between one of 5 actions. Follow these guidelines to make your decision:

            1. **Rebase**
               * A Rebase is only to be chosen when the issue explicitly instructs you to "rebase" or "update"
                 to a newer/specific upstream version. Do not infer this.
               * Identify the <package_version> the package should be updated or rebased to.

            2. **Backport a Patch**
               * This path is for issues that represent a clear bug or CVE that needs a targeted fix.
               * If your investigation successfully identifies a specific fix that passes validation, your decision is backport
               * You must be able to justify why the patch is correct and how it addresses the issue
               * Use the validated patch URL from the FixInvestigator

            3. **Clarification Needed**
               * If your investigation confirms a valid bug/CVE but fails to locate a specific fix, your decision
                 is clarification-needed
               * This is the correct choice when you are sure a problem exists but cannot find the solution yourself

            4. **No Action**
               * A No Action decision is appropriate for issues that are intentionally non-actionable:
                 - The request is too vague to be understood
                 - It's a feature request
                 - There is insufficient information to even begin an investigation
                 - Note: This is not for valid bugs where you simply can't find the patch

            5. **Error**
               * An Error decision is appropriate when there are processing issues that prevent proper analysis, e.g.:
                 - The package mentioned in the issue cannot be found or identified
                 - The issue cannot be accessed

            **Output Format:**
            Return your decision in JSON format with these exact fields:
            - resolution_type: "rebase" | "backport" | "clarification-needed" | "no-action" | "error"
            - package_name: The package name from analysis
            - branch: The target branch from analysis
            - version: Target version (for rebase only)
            - patch_url: URL to the fix (for backport only)
            - justification: Clear explanation of why this patch fixes the issue
            - findings: Summary of the investigation
            - additional_info_needed: What information is missing (for clarification-needed)
            - reasoning: Reason for no action (for no-action)
            - error_details: Error details (for error)

            Consider all available information from the previous agents and make the most appropriate decision.
            
            IMPORTANT: Base your decision on the actual package name, branch, and investigation results from the previous agents.
            """,
            llm=self.llm,
            tools=[ThinkTool()],
        )

    async def run(self, jira_issue: str) -> TriageResult:
        """Run the multi-agent triage workflow for a given Jira issue"""

        # Define the workflow inputs for each agent
        inputs = [
            AgentWorkflowInput(
                prompt=f"Analyze the Jira issue {jira_issue} and extract key information including package name, target branch, and any direct links to fixes. Return your analysis in JSON format with fields: package_name, target_branch, direct_links, keywords, root_cause.",
                context=jira_issue,
                expected_output="A structured analysis of the Jira issue with extracted key information in JSON format.",
            ),
            AgentWorkflowInput(
                prompt="Validate the package information and repository structure based on the analysis. Use shell commands to verify package existence. Return validation results in JSON format with fields: package_exists, branch_exists, validation_status, error_message.",
                expected_output="Validation results confirming package existence and repository structure in JSON format.",
            ),
            AgentWorkflowInput(
                prompt="Investigate potential fixes for the issue, searching for upstream patches and validating any provided URLs. Return investigation results in JSON format with fields: found_fixes, patch_urls, validation_results, rebase_candidate, backport_candidate.",
                expected_output="Investigation results including found fixes, patch URLs, and validation results in JSON format.",
            ),
            AgentWorkflowInput(
                prompt="Based on all the collected information, make a final triage decision and provide the appropriate resolution data. Return your decision in JSON format with fields: resolution_type (rebase|backport|clarification-needed|no-action|error), package_name, branch, version (for rebase), patch_url (for backport), justification, findings, additional_info_needed, reasoning, error_details.",
                expected_output="A final triage decision with resolution type and associated data in JSON format.",
            ),
        ]

        # Run the workflow with MCP tools
        async with mcp_tools(
            os.getenv("MCP_GATEWAY_URL"), filter=lambda t: t == "get_jira_details"
        ) as gateway_tools:
            logger.info(
                f"Retrieved {len(gateway_tools)} MCP tools: {[t.name for t in gateway_tools]}"
            )

            # Recreate the workflow with MCP tools integrated
            logger.info("Recreating workflow with MCP tools integration...")
            self.workflow = AgentWorkflow(name="TriageMultiAgentWorkflow")
            self._setup_agents(gateway_tools)

            # Run the workflow
            logger.info("Starting workflow execution...")
            response = await self.workflow.run(inputs)

        logger.info(f"Workflow response: {response}")

        # Log each agent's output for debugging
        if response and hasattr(response, "steps") and response.steps:
            logger.info("=== AGENT OUTPUTS FOR DEBUGGING ===")
            for i, step in enumerate(response.steps):
                logger.info(f"\n--- Agent {i+1}: {step.name} ---")
                if hasattr(step, "final_answer"):
                    logger.info(f"Final Answer: {step.final_answer}")
                if hasattr(step, "result"):
                    logger.info(f"Result: {step.result}")
                logger.info(f"State: {step.state}")
            logger.info("=== END AGENT OUTPUTS ===")

        # Extract the final decision from the last agent (DecisionMaker)
        if response and hasattr(response, "steps") and response.steps:
            final_step = response.steps[-1]
            logger.info(f"Final step result: {final_step}")

            # Try to extract the decision from the final agent's response
            decision_text = ""
            if hasattr(final_step, "final_answer"):
                decision_text = final_step.final_answer
            elif hasattr(final_step, "result") and final_step.result:
                if hasattr(final_step.result, "text"):
                    decision_text = final_step.result.text
                elif hasattr(final_step.result, "content"):
                    decision_text = final_step.result.content
                else:
                    decision_text = str(final_step.result)

            # If still empty, try to get from the response directly
            if not decision_text and hasattr(response, "final_answer"):
                decision_text = response.final_answer

            # If still empty, try to get from the response state
            if (
                not decision_text
                and hasattr(response, "state")
                and hasattr(response.state, "final_answer")
            ):
                decision_text = response.state.final_answer

            logger.info(f"Decision text: {decision_text}")

            # Parse the JSON response from the DecisionMaker agent
            logger.info("=== PARSING DECISION ===")
            try:
                import json
                import re

                # Try to extract JSON from the response
                json_match = re.search(r"\{.*\}", decision_text, re.DOTALL)
                if json_match:
                    decision_data = json.loads(json_match.group())
                    logger.info(
                        f"Successfully parsed JSON decision data: {decision_data}"
                    )

                    # Create the appropriate result based on resolution type
                    resolution_type = decision_data.get(
                        "resolution_type", "error"
                    ).lower()
                else:
                    logger.warning(
                        f"No JSON pattern found in decision text. Text length: {len(decision_text)}"
                    )
                    logger.warning(f"Decision text preview: {decision_text[:200]}...")
                    decision_data = {}
                    resolution_type = "error"

                # Create the appropriate result based on resolution type
                if resolution_type == "backport":
                    result = TriageResult(
                        resolution=Resolution.BACKPORT,
                        data=BackportData(
                            package=decision_data.get("package_name", ""),
                            branch=decision_data.get("branch", ""),
                            patch_url=decision_data.get("patch_url", ""),
                            justification=decision_data.get("justification", ""),
                            jira_issue=jira_issue,
                        ),
                    )
                elif resolution_type == "rebase":
                    result = TriageResult(
                        resolution=Resolution.REBASE,
                        data=RebaseData(
                            package=decision_data.get("package_name", ""),
                            version=decision_data.get("version", ""),
                            branch=decision_data.get("branch", ""),
                            jira_issue=jira_issue,
                        ),
                    )
                elif resolution_type == "clarification-needed":
                    result = TriageResult(
                        resolution=Resolution.CLARIFICATION_NEEDED,
                        data=ClarificationNeededData(
                            findings=decision_data.get("findings", ""),
                            additional_info_needed=decision_data.get(
                                "additional_info_needed", ""
                            ),
                            jira_issue=jira_issue,
                        ),
                    )
                elif resolution_type == "no-action":
                    result = TriageResult(
                        resolution=Resolution.NO_ACTION,
                        data=NoActionData(
                            reasoning=decision_data.get("reasoning", ""),
                            jira_issue=jira_issue,
                        ),
                    )
                else:
                    result = TriageResult(
                        resolution=Resolution.ERROR,
                        data=ErrorData(
                            details=decision_data.get("error_details", "Unknown error"),
                            jira_issue=jira_issue,
                        ),
                    )

            except Exception as e:
                logger.error(f"Error parsing decision: {e}")
                result = TriageResult(
                    resolution=Resolution.ERROR,
                    data=ErrorData(
                        details=f"Error parsing decision: {e}",
                        jira_issue=jira_issue,
                    ),
                )
        else:
            logger.error("No workflow response or steps found")
            result = TriageResult(
                resolution=Resolution.ERROR,
                data=ErrorData(
                    details="No workflow response received",
                    jira_issue=jira_issue,
                ),
            )

        return result


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Setup observability
    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))

    agent = TriageMultiAgentWorkflow()

    if jira_issue := os.getenv("JIRA_ISSUE", None):
        logger.info("Running in direct mode with environment variable")
        result = await agent.run(jira_issue)
        logger.info(f"Direct run completed: {result.model_dump_json(indent=4)}")
        return

    class Task(BaseModel):
        metadata: dict = Field(description="Task metadata")
        attempts: int = Field(default=0, description="Number of processing attempts")

    logger.info("Starting triage multi-agent workflow in queue mode")
    async with redis_client(os.getenv("REDIS_URL")) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        logger.info(f"Connected to Redis, max retries set to {max_retries}")

        while True:
            logger.info("Waiting for tasks from triage_queue (timeout: 30s)...")
            element = await redis.brpop("triage_queue", timeout=30)
            if element is None:
                logger.info("No tasks received, continuing to wait...")
                continue

            _, payload = element
            logger.info(f"Received task from queue")

            task = Task.model_validate_json(payload)
            jira_issue = task.metadata.get("issue")
            logger.info(
                f"Processing triage for JIRA issue: {jira_issue}, "
                f"attempt: {task.attempts + 1}"
            )

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {jira_issue}"
                    )
                    await redis.lpush("triage_queue", task.model_dump_json())
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, "
                        f"moving to error list: {jira_issue}"
                    )
                    await redis.lpush("error_list", error)

            try:
                logger.info(f"Starting triage processing for {jira_issue}")
                result = await agent.run(jira_issue)
                logger.info(
                    f"Triage processing completed for {jira_issue}, "
                    f"resolution: {result.resolution.value}"
                )

                # Handle the result based on resolution
                if result.resolution == Resolution.REBASE:
                    logger.info(
                        f"Triage resolved as REBASE for {jira_issue}, "
                        f"adding to rebase queue"
                    )
                    task = Task(metadata=result.data.model_dump())
                    await redis.lpush("rebase_queue", task.model_dump_json())
                elif result.resolution == Resolution.BACKPORT:
                    logger.info(
                        f"Triage resolved as BACKPORT for {jira_issue}, "
                        f"adding to backport queue"
                    )
                    task = Task(metadata=result.data.model_dump())
                    await redis.lpush("backport_queue", task.model_dump_json())
                elif result.resolution == Resolution.CLARIFICATION_NEEDED:
                    logger.info(
                        f"Triage resolved as CLARIFICATION_NEEDED for {jira_issue}, "
                        f"adding to clarification needed queue"
                    )
                    task = Task(metadata=result.data.model_dump())
                    await redis.lpush(
                        "clarification_needed_queue", task.model_dump_json()
                    )
                elif result.resolution == Resolution.NO_ACTION:
                    logger.info(
                        f"Triage resolved as NO_ACTION for {jira_issue}, "
                        f"adding to no action list"
                    )
                    await redis.lpush("no_action_list", result.data.model_dump_json())
                elif result.resolution == Resolution.ERROR:
                    logger.warning(
                        f"Triage resolved as ERROR for {jira_issue}, retrying"
                    )
                    await retry(task, result.data.model_dump_json())

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(
                    f"Exception during triage processing for {jira_issue}: {error}"
                )
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=jira_issue).model_dump_json(),
                )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
