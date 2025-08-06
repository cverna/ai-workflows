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
from utils import redis_client

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

class TriageResult(BaseModel):
    resolution: Resolution = Field(description="Triage resolution")
    data: Union[RebaseData, BackportData, ClarificationNeededData, NoActionData, ErrorData] = Field(
        description="Associated data"
    )

class TriageMultiAgentWorkflow:
    def __init__(self) -> None:
        self.llm = ChatModel.from_name(os.getenv("CHAT_MODEL"))
        
        # Create multi-agent workflow
        self.workflow = AgentWorkflow(name="TriageMultiAgentWorkflow")
        
        # Add specialized agents
        self._setup_agents()

    def _setup_agents(self) -> None:
        """Setup specialized agents for different aspects of triage"""
        
        # Jira Issue Analyzer Agent
        self.workflow.add_agent(
            name="JiraAnalyzer",
            role="A specialized agent for analyzing Jira issues",
            instructions="""
            You are an expert at analyzing Jira issues for RHEL packages. Your role is to:
            1. Extract key details from the issue title, description, and comments
            2. Identify the package name and component
            3. Determine the target branch from fixVersion field
            4. Look for direct links to fixes or patches
            5. Identify keywords indicating the root cause
            
            Focus on extracting factual information that will be used by other agents.
            """,
            llm=self.llm,
        )

        # Package Validator Agent
        self.workflow.add_agent(
            name="PackageValidator",
            role="A specialized agent for validating package information",
            instructions="""
            You are an expert at validating package information for RHEL. Your role is to:
            1. Verify that identified packages exist in GitLab repositories
            2. Confirm branch existence and validity
            3. Check package repository structure
            4. Validate package naming conventions
            
            Use shell commands to verify package existence and repository structure.
            """,
            llm=self.llm,
        )

        # Fix Investigator Agent
        self.workflow.add_agent(
            name="FixInvestigator",
            role="A specialized agent for investigating potential fixes",
            instructions="""
            You are an expert at investigating potential fixes for RHEL issues. Your role is to:
            1. Search for upstream fixes based on issue details
            2. Validate patch URLs and content
            3. Determine if a rebase or backport is appropriate
            4. Search bug trackers and git repositories for relevant commits
            5. Analyze commit messages and code changes
            
            Be thorough in your search and validation of potential fixes.
            """,
            llm=self.llm,
        )

        # Decision Maker Agent
        self.workflow.add_agent(
            name="DecisionMaker",
            role="A specialized agent for making triage decisions",
            instructions="""
            You are an expert at making triage decisions for RHEL issues. Your role is to:
            1. Analyze all collected information from other agents
            2. Determine the appropriate resolution (rebase, backport, clarification needed, no action, error)
            3. Provide clear justification for the decision
            4. Create the appropriate data structure for the chosen resolution
            
            Consider all available information and make the most appropriate decision.
            """,
            llm=self.llm,
        )

    async def run(self, jira_issue: str) -> TriageResult:
        """Run the multi-agent triage workflow for a given Jira issue"""
        
        # Define the workflow inputs for each agent
        inputs = [
            AgentWorkflowInput(
                prompt=f"Analyze the Jira issue {jira_issue} and extract key information including package name, target branch, and any direct links to fixes.",
                context=jira_issue,
                expected_output="A structured analysis of the Jira issue with extracted key information."
            ),
            AgentWorkflowInput(
                prompt="Validate the package information and repository structure based on the analysis.",
                expected_output="Validation results confirming package existence and repository structure."
            ),
            AgentWorkflowInput(
                prompt="Investigate potential fixes for the issue, searching for upstream patches and validating any provided URLs.",
                expected_output="Investigation results including found fixes, patch URLs, and validation results."
            ),
            AgentWorkflowInput(
                prompt="Based on all the collected information, make a final triage decision and provide the appropriate resolution data.",
                expected_output="A final triage decision with resolution type and associated data."
            ),
        ]
        
        # Run the workflow
        response = await self.workflow.run(inputs)
        
        # Parse the final result from the DecisionMaker agent
        # This is a simplified version - in practice, you'd need to parse the actual response
        # and create the appropriate TriageResult object
        
        # For demonstration, we'll create a sample result
        result = TriageResult(
            resolution=Resolution.BACKPORT,
            data=BackportData(
                package="example-package",
                branch="c9s",
                patch_url="https://example.com/fix.patch",
                justification="This patch fixes the buffer overflow issue described in the Jira ticket",
                jira_issue=jira_issue
            )
        )
        
        return result


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    
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
                          f"resolution: {result.resolution.value}")
                
                # Handle the result based on resolution
                if result.resolution == Resolution.REBASE:
                    logger.info(f"Triage resolved as REBASE for {jira_issue}, "
                              f"adding to rebase queue")
                    task = Task(metadata=result.data.model_dump())
                    await redis.lpush("rebase_queue", task.model_dump_json())
                elif result.resolution == Resolution.BACKPORT:
                    logger.info(f"Triage resolved as BACKPORT for {jira_issue}, "
                              f"adding to backport queue")
                    task = Task(metadata=result.data.model_dump())
                    await redis.lpush("backport_queue", task.model_dump_json())
                elif result.resolution == Resolution.CLARIFICATION_NEEDED:
                    logger.info(f"Triage resolved as CLARIFICATION_NEEDED for {jira_issue}, "
                              f"adding to clarification needed queue")
                    task = Task(metadata=result.data.model_dump())
                    await redis.lpush("clarification_needed_queue", task.model_dump_json())
                elif result.resolution == Resolution.NO_ACTION:
                    logger.info(f"Triage resolved as NO_ACTION for {jira_issue}, "
                              f"adding to no action list")
                    await redis.lpush("no_action_list", result.data.model_dump_json())
                elif result.resolution == Resolution.ERROR:
                    logger.warning(f"Triage resolved as ERROR for {jira_issue}, retrying")
                    await retry(task, result.data.model_dump_json())
                    
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