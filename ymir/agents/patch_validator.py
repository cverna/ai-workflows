"""Patch Validator sub-agent.

Fetches and validates the candidate patch/commit URLs triage finds, so the
validation procedure and its fetching churn happen in the sub-agent's context.
Exposed to the triage agent through ``HandoffTool``; only the JSON verdict
returns to triage.
"""

import logging
import os

from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools.think import ThinkTool

from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    render_template,
)
from ymir.common.logging_setup import get_trajectory_writeable
from ymir.tools.unprivileged.commands import RunShellCommandTool

logger = logging.getLogger(__name__)

# Tools the patch validation needs: patch/PR fetching plus shell for git searches.
PATCH_VALIDATOR_TOOL_NAMES = (
    "get_github_patch",
    "get_github_pull_request",
    "get_github_compare",
    "get_patch_from_url",
)

# Optional per-specialist model; falls back to the parent's CHAT_MODEL.
PATCH_VALIDATOR_MODEL_ENV = "PATCH_VALIDATOR_MODEL"


def create_patch_validator(gateway_tools, local_tool_options=None) -> ReasoningAgent:
    """Build the patch validator target invoked via ``HandoffTool``."""
    tools = [
        ThinkTool(),
        RunShellCommandTool(options=local_tool_options) if local_tool_options else RunShellCommandTool(),
        *[tool for tool in gateway_tools if tool.name in PATCH_VALIDATOR_TOOL_NAMES],
    ]
    model_override = os.getenv(PATCH_VALIDATOR_MODEL_ENV)
    logger.info("Patch validator model: %s", model_override or os.environ.get("CHAT_MODEL"))
    return ReasoningAgent(
        name="PatchValidator",
        description=(
            "Validates candidate patch/commit URLs for a bug or CVE: fetches them, "
            "checks they are real merged patches that fix the issue, finds required "
            "follow-up commits, and returns a verdict with the patch URLs to use."
        ),
        llm=get_chat_model(model_override),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=tools,
        memory=UnconstrainedMemory(),
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux patch analyst",
        instructions=render_template("triage/patch_validator.j2"),
    )
