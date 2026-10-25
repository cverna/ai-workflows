"""One discovery task per CVE/package, then per-issue checks.

Uses dedicated Redis lists; the individual-triage fetcher and workers keep their
existing routing.
"""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools.think import ThinkTool
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

from ymir.agents import triage_agent
from ymir.agents.observability import setup_observability
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    is_reasoning_enabled,
    render_template,
    resolve_chat_model_override,
)
from ymir.common.base_utils import redis_client
from ymir.common.cve import extract_cve_ids
from ymir.common.logging_setup import configure_logging, get_trajectory_writeable
from ymir.common.models import (
    BackportData,
    ClarificationNeededData,
    CVEEligibilityResult,
    ErrorData,
    OpenEndedAnalysisData,
    PostponedData,
    RebaseData,
    RebuildData,
    Resolution,
    TriageOutputSchema,
)
from ymir.common.multistream_triage import PROCESSING, QUEUE, RESULTS, MultiStreamTriageTask
from ymir.common.utils import DOWNSTREAM_COMPONENT_CUSTOM_FIELD, mcp_tools, run_tool
from ymir.common.version_utils import is_older_zstream
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.version_mapper import VersionMapperTool

logger = logging.getLogger(__name__)


class MultiStreamTriageAnalysis(BaseModel):
    results: list[TriageOutputSchema]


def validate_analysis(task: MultiStreamTriageTask, analysis: MultiStreamTriageAnalysis) -> None:
    expected_types = {
        Resolution.BACKPORT: BackportData,
        Resolution.REBASE: RebaseData,
        Resolution.REBUILD: RebuildData,
        Resolution.POSTPONED_NO_PATCH: PostponedData,
        Resolution.POSTPONED_PR_PENDING: PostponedData,
        Resolution.CLARIFICATION_NEEDED: ClarificationNeededData,
        Resolution.OPEN_ENDED_ANALYSIS: OpenEndedAnalysisData,
        Resolution.ERROR: ErrorData,
    }
    keys = [result.data.jira_issue for result in analysis.results]
    if len(keys) != len(set(keys)) or set(keys) != set(task.issues):
        raise ValueError("Group analysis must cover exactly the requested Jira issues")
    for result in analysis.results:
        expected = expected_types.get(result.resolution)
        if expected is None or not isinstance(result.data, expected):
            raise ValueError(f"Invalid preliminary group resolution: {result.resolution}")
        if hasattr(result.data, "package") and result.data.package != task.package:
            raise ValueError("Group result has an unexpected package")
        if hasattr(result.data, "cve_id") and result.data.cve_id != task.cve:
            raise ValueError("Group result has an unexpected CVE")


def create_multistream_triage_agent(gateway_tools, local_tool_options):
    read_tools = {
        "get_jira_details",
        "search_jira_issues",
        "get_patch_from_url",
        "get_github_patch",
        "get_github_pull_request",
        "get_github_compare",
        "zstream_search",
        "get_maintainer_rules",
        "get_shared_rules",
        "clone_repository",
    }
    return ReasoningAgent(
        name="MultiStreamTriageAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            RunShellCommandTool(options=local_tool_options),
            VersionMapperTool(),
            *[tool for tool in gateway_tools if tool.name in read_tools],
        ],
        memory=UnconstrainedMemory(),
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux developer",
        instructions="Investigate shared upstream evidence once. Return separate preliminary decisions "
        "for each tracker. Never infer downstream applicability from another stream or an advisory "
        "version range. Do not modify Jira, fork, push, open MRs, or trigger builds.",
    )


async def _load_members(task, tools):
    members = {}
    for issue in task.issues:
        details = await run_tool("get_jira_details", available_tools=tools, issue_key=issue)
        fields = details.get("fields", {})
        versions = fields.get("fixVersions", [])
        if (
            fields.get(DOWNSTREAM_COMPONENT_CUSTOM_FIELD) != task.package
            or extract_cve_ids(fields.get("summary")) != task.cve
            or len(versions) != 1
            or not versions[0].get("name")
        ):
            raise ValueError(f"{issue} is not an unambiguous tracker for this CVE/package group")
        eligibility = CVEEligibilityResult.model_validate(
            await run_tool(
                "check_cve_triage_eligibility",
                available_tools=tools,
                issue_key=issue,
            )
        )
        members[issue] = {
            "issue": issue,
            "fields": fields,
            "eligibility": eligibility.model_dump(mode="json"),
            "is_older_zstream": await is_older_zstream(versions[0]["name"]),
        }
    return members


def _investigation_fields(fields):
    return {
        key: fields.get(key)
        for key in ("summary", "description", "comment", "fixVersions", DOWNSTREAM_COMPONENT_CUSTOM_FIELD)
    }


async def run_multistream_triage(task: MultiStreamTriageTask, upstream_agent_factory=None):
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    base = Path(os.environ["GIT_REPO_BASEPATH"])
    base.mkdir(parents=True, exist_ok=True)
    outcomes = {}
    async with mcp_tools(os.environ["MCP_GATEWAY_URL"], call_meta={"cve": task.cve}) as tools:
        members = await _load_members(task, tools)
        eligible = [
            issue for issue in task.issues if members[issue]["eligibility"]["eligibility"] == "immediately"
        ]
        analysis = MultiStreamTriageAnalysis(results=[])
        if eligible:
            with TemporaryDirectory(
                prefix="multistream-triage-", dir=base, ignore_cleanup_errors=True
            ) as directory:
                factory = upstream_agent_factory or create_multistream_triage_agent
                agent = factory(tools, {"working_directory": Path(directory)})
                prompt = render_template("multistream_triage/prompt.j2")
                prompt += "\nExecution workspace: " + directory
                prompt += "\nGroup task and untrusted Jira source material:\n" + json.dumps(
                    {
                        "cve": task.cve,
                        "package": task.package,
                        "members": [members[issue] for issue in eligible],
                    }
                )
                response = await agent.run(
                    prompt,
                    expected_output="Return one JSON object matching this batch schema:\n"
                    + json.dumps(MultiStreamTriageAnalysis.model_json_schema()),
                    **get_agent_execution_config(),
                )
                analysis = MultiStreamTriageAnalysis.model_validate_json(response.last_message.text)
                validate_analysis(task.model_copy(update={"issues": eligible}), analysis)
        prepared = {result.data.jira_issue: result for result in analysis.results}
        for issue in task.issues:
            try:
                current = await run_tool("get_jira_details", available_tools=tools, issue_key=issue)
                if _investigation_fields(current.get("fields", {})) != _investigation_fields(
                    members[issue]["fields"]
                ):
                    raise ValueError(
                        "Jira investigation inputs changed during group analysis; rerun this issue"
                    )
                result = prepared.get(issue)
                if result is not None and hasattr(result.data, "fix_version"):
                    result.data.fix_version = members[issue]["fields"]["fixVersions"][0]["name"]
                state = await triage_agent.run_workflow(
                    issue,
                    dry_run=dry_run,
                    triage_agent_factory=triage_agent.create_triage_agent,
                    auto_chain=False,
                    precomputed_triage=result,
                )
                outcomes[issue] = state.model_dump(mode="json")
            except Exception as error:
                logger.exception("Per-issue group triage failed for %s", issue)
                outcomes[issue] = {"error": str(error)}
    return {
        "task": task.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"),
        "outcomes": outcomes,
    }


async def _main(args):
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        if args.command == "enqueue":
            task = MultiStreamTriageTask(cve=args.cve, package=args.package, issues=args.issues)
            await redis.lpush(QUEUE, task.model_dump_json())
            print(f"Queued {task.execution_id}: {len(task.issues)} trackers for {task.cve}")
            return
        payload = await redis.brpoplpush(QUEUE, PROCESSING, timeout=5)
        if payload is None:
            print("No multi-stream triage task queued")
            return
        task = None
        try:
            task = MultiStreamTriageTask.model_validate_json(payload)
            with trace.get_tracer(__name__).start_as_current_span("MultiStreamTriageWorkflow") as span:
                span.set_attribute("jira.issue", task.issues)
                span.set_attribute("workflow.name", "MultiStreamTriageWorkflow")
                result = await run_multistream_triage(task)
                failed = any("error" in outcome for outcome in result["outcomes"].values())
                span.set_status(StatusCode.ERROR if failed else StatusCode.OK)
        except Exception as error:
            logger.exception("Multi-stream triage task failed")
            result = {"task": task.model_dump(mode="json") if task else None, "error": str(error)}
        async with redis.pipeline(transaction=True) as pipe:
            pipe.lpush(RESULTS, json.dumps(result))
            pipe.lrem(PROCESSING, 1, payload)
            await pipe.execute()
        print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--cve", required=True)
    enqueue.add_argument("--package", required=True)
    enqueue.add_argument("issues", nargs="+")
    commands.add_parser("run-once")
    args = parser.parse_args()
    configure_logging(level=logging.INFO)
    resolve_chat_model_override("triage")
    if endpoint := os.getenv("COLLECTOR_ENDPOINT"):
        setup_observability(endpoint)
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
