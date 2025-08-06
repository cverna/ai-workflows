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
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow, WorkflowReservedStepName

from tools.commands import RunShellCommandTool
from tools.patch_validator import PatchValidatorTool
from utils import mcp_tools, redis_client

logger = logging.getLogger(__name__)

# Type aliases for workflow steps
WorkflowStep: TypeAlias = Literal[
    "analyze_jira_issue",
    "identify_package", 
    "identify_branch",
    "investigate_fix",
    "make_decision",
    "validate_decision"
]

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
    justification: str = Field(description="Clear explanation of why this patch fixes the issue")
    jira_issue: str = Field(description="Jira issue identifier")

class ClarificationNeededData(BaseModel):
    findings: str = Field(description="Summary of the investigation")
    additional_info_needed: str = Field(description="Summary of missing information")
    jira_issue: str = Field(description="Jira issue identifier")

class NoActionData(BaseModel):
    reasoning: str = Field(description="Reason why the issue is intentionally non-actionable")
    jira_issue: str = Field(description="Jira issue identifier")

class ErrorData(BaseModel):
    details: str = Field(description="Specific details about an error")
    jira_issue: str = Field(description="Jira issue identifier")

# Workflow state
class TriageState(BaseModel):
    jira_issue: str = Field(description="Jira issue identifier")
    package: str | None = Field(default=None, description="Identified package name")
    branch: str | None = Field(default=None, description="Target branch")
    version: str | None = Field(default=None, description="Target version for rebase")
    patch_url: str | None = Field(default=None, description="URL to the fix patch")
    justification: str | None = Field(default=None, description="Justification for the fix")
    findings: str | None = Field(default=None, description="Investigation findings")
    additional_info_needed: str | None = Field(default=None, description="Additional info needed")
    reasoning: str | None = Field(default=None, description="Reasoning for no action")
    error_details: str | None = Field(default=None, description="Error details")
    resolution: Resolution | None = Field(default=None, description="Final resolution")
    resolution_data: Union[RebaseData, BackportData, ClarificationNeededData, NoActionData, ErrorData] | None = Field(
        default=None, description="Resolution data"
    )

class TriageAgentWorkflow:
    def __init__(self) -> None:
        self.llm = ChatModel.from_name(os.getenv("CHAT_MODEL"))
        self.tools = [ThinkTool(), RunShellCommandTool(), PatchValidatorTool()]
        self.memory = UnconstrainedMemory()
        
        # Create workflow
        self.workflow = Workflow[TriageState, WorkflowStep](
            name="TriageAgentWorkflow", 
            schema=TriageState
        )
        
        # Add workflow steps
        self.workflow.add_step("analyze_jira_issue", self._analyze_jira_issue)
        self.workflow.add_step("identify_package", self._identify_package)
        self.workflow.add_step("identify_branch", self._identify_branch)
        self.workflow.add_step("investigate_fix", self._investigate_fix)
        self.workflow.add_step("make_decision", self._make_decision)
        self.workflow.add_step("validate_decision", self._validate_decision)

    async def _analyze_jira_issue(self, state: TriageState) -> WorkflowStep | WorkflowReservedStepName:
        """Analyze the Jira issue to extract key information"""
        logger.info(f"Analyzing Jira issue: {state.jira_issue}")
        
        # This step would use the MCP tools to get Jira details
        # For now, we'll simulate the analysis
        logger.info("Jira issue analysis completed")
        return "identify_package"

    async def _identify_package(self, state: TriageState) -> WorkflowStep | WorkflowReservedStepName:
        """Identify the package name from the Jira issue"""
        logger.info(f"Identifying package for issue: {state.jira_issue}")
        
        # Simulate package identification logic
        # In a real implementation, this would analyze the Jira issue content
        # and extract the package name
        
        # For demonstration, we'll use a placeholder
        state.package = "example-package"
        logger.info(f"Identified package: {state.package}")
        
        return "identify_branch"

    async def _identify_branch(self, state: TriageState) -> WorkflowStep | WorkflowReservedStepName:
        """Identify the target branch from the Jira issue"""
        logger.info(f"Identifying branch for package: {state.package}")
        
        # Simulate branch identification logic
        # This would look at the fixVersion field in the Jira issue
        # and map it to the appropriate branch
        
        state.branch = "c9s"  # Example branch
        logger.info(f"Identified branch: {state.branch}")
        
        return "investigate_fix"

    async def _investigate_fix(self, state: TriageState) -> WorkflowStep | WorkflowReservedStepName:
        """Investigate potential fixes for the issue"""
        logger.info(f"Investigating fixes for {state.package}")
        
        # This step would:
        # 1. Search for upstream fixes
        # 2. Validate patch URLs
        # 3. Determine if a rebase or backport is needed
        
        # For demonstration, we'll simulate finding a backport opportunity
        state.patch_url = "https://example.com/fix.patch"
        state.justification = "This patch fixes the buffer overflow issue described in the Jira ticket"
        
        logger.info("Fix investigation completed")
        return "make_decision"

    async def _make_decision(self, state: TriageState) -> WorkflowStep | WorkflowReservedStepName:
        """Make the final decision on the resolution"""
        logger.info("Making final decision")
        
        # Decision logic based on the investigation
        # For demonstration, we'll choose backport
        state.resolution = Resolution.BACKPORT
        
        logger.info(f"Decision made: {state.resolution.value}")
        return "validate_decision"

    async def _validate_decision(self, state: TriageState) -> WorkflowReservedStepName:
        """Validate the decision and create the final output"""
        logger.info("Validating decision")
        
        # Create the appropriate resolution data based on the decision
        if state.resolution == Resolution.BACKPORT:
            state.resolution_data = BackportData(
                package=state.package,
                branch=state.branch,
                patch_url=state.patch_url,
                justification=state.justification,
                jira_issue=state.jira_issue
            )
        elif state.resolution == Resolution.REBASE:
            state.resolution_data = RebaseData(
                package=state.package,
                version=state.version,
                branch=state.branch,
                jira_issue=state.jira_issue
            )
        elif state.resolution == Resolution.CLARIFICATION_NEEDED:
            state.resolution_data = ClarificationNeededData(
                findings=state.findings,
                additional_info_needed=state.additional_info_needed,
                jira_issue=state.jira_issue
            )
        elif state.resolution == Resolution.NO_ACTION:
            state.resolution_data = NoActionData(
                reasoning=state.reasoning,
                jira_issue=state.jira_issue
            )
        elif state.resolution == Resolution.ERROR:
            state.resolution_data = ErrorData(
                details=state.error_details,
                jira_issue=state.jira_issue
            )
        
        logger.info("Decision validation completed")
        return Workflow.END

    async def run(self, jira_issue: str) -> TriageState:
        """Run the triage workflow for a given Jira issue"""
        initial_state = TriageState(jira_issue=jira_issue)
        
        async with mcp_tools(
            os.getenv("MCP_GATEWAY_URL"), 
            filter=lambda t: t == "get_jira_details"
        ) as gateway_tools:
            # Add MCP tools to the workflow tools
            workflow_tools = self.tools + gateway_tools
            
            # Run the workflow
            response = await self.workflow.run(initial_state)
            return response.state


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    
    agent = TriageAgentWorkflow()
    
    if jira_issue := os.getenv("JIRA_ISSUE", None):
        logger.info("Running in direct mode with environment variable")
        result = await agent.run(jira_issue)
        logger.info(f"Direct run completed: {result.model_dump_json(indent=4)}")
        return

    class Task(BaseModel):
        metadata: dict = Field(description="Task metadata")
        attempts: int = Field(default=0, description="Number of processing attempts")

    logger.info("Starting triage agent workflow in queue mode")
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
            logger.info(f"Processing triage for JIRA issue: {jira_issue}, "
                       f"attempt: {task.attempts + 1}")

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(f"Task failed (attempt {task.attempts}/{max_retries}), "
                                 f"re-queuing for retry: {jira_issue}")
                    await redis.lpush("triage_queue", task.model_dump_json())
                else:
                    logger.error(f"Task failed after {max_retries} attempts, "
                               f"moving to error list: {jira_issue}")
                    await redis.lpush("error_list", error)

            try:
                logger.info(f"Starting triage processing for {jira_issue}")
                result = await agent.run(jira_issue)
                logger.info(f"Triage processing completed for {jira_issue}, "
                          f"resolution: {result.resolution.value if result.resolution else 'None'}")
                
                # Handle the result based on resolution
                if result.resolution == Resolution.REBASE:
                    logger.info(f"Triage resolved as REBASE for {jira_issue}, "
                              f"adding to rebase queue")
                    task = Task(metadata=result.resolution_data.model_dump())
                    await redis.lpush("rebase_queue", task.model_dump_json())
                elif result.resolution == Resolution.BACKPORT:
                    logger.info(f"Triage resolved as BACKPORT for {jira_issue}, "
                              f"adding to backport queue")
                    task = Task(metadata=result.resolution_data.model_dump())
                    await redis.lpush("backport_queue", task.model_dump_json())
                elif result.resolution == Resolution.CLARIFICATION_NEEDED:
                    logger.info(f"Triage resolved as CLARIFICATION_NEEDED for {jira_issue}, "
                              f"adding to clarification needed queue")
                    task = Task(metadata=result.resolution_data.model_dump())
                    await redis.lpush("clarification_needed_queue", task.model_dump_json())
                elif result.resolution == Resolution.NO_ACTION:
                    logger.info(f"Triage resolved as NO_ACTION for {jira_issue}, "
                              f"adding to no action list")
                    await redis.lpush("no_action_list", result.resolution_data.model_dump_json())
                elif result.resolution == Resolution.ERROR:
                    logger.warning(f"Triage resolved as ERROR for {jira_issue}, retrying")
                    await retry(task, result.resolution_data.model_dump_json())
                    
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during triage processing for {jira_issue}: {error}")
                await retry(
                    task, ErrorData(details=error, jira_issue=jira_issue).model_dump_json()
                )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain()) 