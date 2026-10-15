"""Rebuild specialist sub-agent.

Checks whether a package's dependency fix has shipped, so triage can decide
between ``rebuild``, ``postponed_dependency``, ``not-affected`` and
``clarification-needed``. Exposed to the triage agent through ``HandoffTool``:
the dependency search (and its tool churn) happens in the sub-agent's context,
and only the verdict returns to triage.
"""

import logging

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

logger = logging.getLogger(__name__)

# Jira tools the dependency-readiness check needs, served by the MCP gateway.
REBUILD_SPECIALIST_TOOL_NAMES = ("search_jira_issues", "get_jira_details")


def create_rebuild_specialist(gateway_tools, local_tool_options=None) -> ReasoningAgent:
    """Build the rebuild specialist target invoked via ``HandoffTool``."""
    tools = [
        ThinkTool(),
        *[tool for tool in gateway_tools if tool.name in REBUILD_SPECIALIST_TOOL_NAMES],
    ]
    return ReasoningAgent(
        name="RebuildSpecialist",
        description=(
            "Checks whether a package's dependency fix has shipped: searches Jira for the "
            "dependency ticket, validates its fixVersion against the triaged stream, and "
            "returns a verdict for the rebuild decision."
        ),
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=tools,
        memory=UnconstrainedMemory(),
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux dependency analyst",
        instructions=render_template("triage/rebuild_specialist.j2"),
    )
