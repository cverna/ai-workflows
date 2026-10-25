import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from flexmock import flexmock
from pydantic import ValidationError

from ymir.agents import multistream_triage_agent as multistream
from ymir.agents.triage_agent import TriageState
from ymir.common import config
from ymir.common.models import PostponedData, Resolution, TriageOutputSchema


@pytest.fixture
def stream_config(monkeypatch):
    async def load_config():
        return {
            "current_z_streams": {"9": "rhel-9.8.z", "10": "rhel-10.2.z"},
            "upcoming_z_streams": {"9": "rhel-9.9.z", "10": "rhel-10.3.z"},
        }

    monkeypatch.setattr(config, "load_rhel_config", load_config)


def _task(**overrides):
    return multistream.MultiStreamTriageTask.model_validate(
        {
            "cve": "CVE-2026-115484",
            "package": "iscsi-initiator-utils",
            "issues": ["RHEL-1", "RHEL-2"],
            **overrides,
        }
    )


def _details(issue):
    return {
        "key": issue,
        "fields": {
            "summary": "CVE-2026-115484 iscsi-initiator-utils: parser defect",
            "description": "Reported upstream defect",
            "comment": {"comments": []},
            "customfield_10669": "iscsi-initiator-utils",
            "fixVersions": [{"name": "rhel-9.9.z" if issue == "RHEL-1" else "rhel-10.3.z"}],
        },
    }


def _analysis():
    return multistream.MultiStreamTriageAnalysis(
        results=[
            TriageOutputSchema(
                resolution=Resolution.POSTPONED_NO_PATCH,
                data=PostponedData(
                    summary="No accepted upstream fix found",
                    pending_issues=[issue],
                    jira_issue=issue,
                    package="iscsi-initiator-utils",
                    cve_id="CVE-2026-115484",
                ),
            )
            for issue in ["RHEL-1", "RHEL-2"]
        ]
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"cve": "not-a-cve"},
        {"cve": "CVE-2026-115484,CVE-2026-115485"},
        {"package": "../package"},
        {"issues": []},
        {"issues": ["RHEL-1", "rhel-1"]},
        {"issues": ["../RHEL-1"]},
    ],
)
def test_invalid_group_tasks_are_rejected(overrides):
    with pytest.raises(ValidationError):
        _task(**overrides)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("QUEUE", "multistream_triage_queue"),
        ("PROCESSING", "multistream_triage_processing"),
        ("RESULTS", "multistream_triage_results"),
    ],
)
def test_multistream_redis_key_names(name, expected):
    assert getattr(multistream, name) == expected


def test_group_task_survives_redis_serialization():
    task = _task(issues=["rhel-1", "RHEL-2"])
    restored = multistream.MultiStreamTriageTask.model_validate_json(task.model_dump_json())
    assert restored == task
    assert restored.issues == ["RHEL-1", "RHEL-2"]


@pytest.mark.parametrize(
    "variant", ["missing", "duplicate", "foreign", "not-affected", "wrong-package", "wrong-cve"]
)
def test_group_analysis_must_cover_exactly_its_own_trackers(variant):
    task = _task()
    analysis = _analysis()
    if variant == "missing":
        analysis.results.pop()
    elif variant == "duplicate":
        analysis.results[1] = analysis.results[0].model_copy(deep=True)
    elif variant == "foreign":
        analysis.results[0].data.jira_issue = "RHEL-3"
    elif variant == "not-affected":
        analysis.results[0].resolution = Resolution.NOT_AFFECTED
    elif variant == "wrong-package":
        analysis.results[0].data.package = "another-package"
    else:
        analysis.results[0].data.cve_id = "CVE-2026-115485"
    with pytest.raises(ValueError):
        multistream.validate_analysis(task, analysis)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["normal", "one-fails", "changed-input"])
@pytest.mark.parametrize("dry_run_env", [None, "false", "true", "TRUE"])
async def test_group_investigates_once_then_runs_independent_issue_workflows(
    tmp_path, monkeypatch, scenario, stream_config, dry_run_env
):
    if dry_run_env is None:
        monkeypatch.delenv("DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("DRY_RUN", dry_run_env)
    dry_run = (dry_run_env or "false").lower() == "true"
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://unused")
    investigations = []
    processed = []
    reads = {}

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    async def run_tool(name, **kwargs):
        if name == "get_jira_details":
            issue = kwargs["issue_key"]
            reads[issue] = reads.get(issue, 0) + 1
            details = _details(issue)
            if scenario == "changed-input" and issue == "RHEL-1" and reads[issue] > 1:
                details["fields"]["description"] = "New evidence arrived"
            return details
        assert name == "check_cve_triage_eligibility"
        return {"is_cve": True, "eligibility": "immediately", "reason": "Eligible"}

    async def investigate(prompt, **kwargs):
        investigations.append(prompt)
        assert "RHEL-1" in prompt and "RHEL-2" in prompt
        payload = json.loads(prompt.split("Group task and untrusted Jira source material:\n", 1)[1])
        assert all(member["is_older_zstream"] is False for member in payload["members"])
        assert "upcoming" in prompt
        return flexmock(last_message=flexmock(text=_analysis().model_dump_json()))

    async def process(issue, **kwargs):
        assert kwargs["dry_run"] is dry_run
        assert kwargs["auto_chain"] is False
        result = kwargs["precomputed_triage"]
        assert result.data.jira_issue == issue
        assert result.data.fix_version == _details(issue)["fields"]["fixVersions"][0]["name"]
        processed.append(issue)
        if scenario == "one-fails" and issue == "RHEL-1":
            raise RuntimeError("Cannot prepare this stream")
        return TriageState(jira_issue=issue, triage_result=result)

    agent = flexmock()
    agent.should_receive("run").replace_with(investigate)
    monkeypatch.setattr(multistream, "mcp_tools", gateway)
    monkeypatch.setattr(multistream, "run_tool", run_tool)
    monkeypatch.setattr(multistream, "get_agent_execution_config", dict)
    monkeypatch.setattr(multistream.triage_agent, "run_workflow", process)
    result = await multistream.run_multistream_triage(_task(), upstream_agent_factory=lambda *_: agent)
    assert len(investigations) == 1
    assert processed == (["RHEL-2"] if scenario == "changed-input" else ["RHEL-1", "RHEL-2"])
    assert set(result["outcomes"]) == {"RHEL-1", "RHEL-2"}
    assert ("error" in result["outcomes"]["RHEL-1"]) is (scenario != "normal")
    assert "error" not in result["outcomes"]["RHEL-2"]
    assert not list(tmp_path.glob("multistream-triage-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["enqueue", "run-once"])
async def test_worker_commands_do_not_require_dry_run(monkeypatch, command):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    task = _task()
    redis = flexmock()

    async def push(queue, payload):
        assert queue == multistream.QUEUE
        assert multistream.MultiStreamTriageTask.model_validate_json(payload).issues == task.issues

    async def pop(*args, **kwargs):
        return None

    redis.should_receive("lpush").replace_with(push)
    redis.should_receive("brpoplpush").replace_with(pop)

    @asynccontextmanager
    async def redis_context(*args, **kwargs):
        yield redis

    monkeypatch.setattr(multistream, "redis_client", redis_context)
    await multistream._main(
        SimpleNamespace(command=command, cve=task.cve, package=task.package, issues=task.issues)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_deferred_group_uses_eligibility_workflows_without_shared_investigation(
    tmp_path, monkeypatch, stream_config, dry_run
):
    monkeypatch.setenv("DRY_RUN", str(dry_run))
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://unused")

    @asynccontextmanager
    async def gateway(*args, **kwargs):
        yield []

    async def run_tool(name, **kwargs):
        if name == "get_jira_details":
            return _details(kwargs["issue_key"])
        return {"is_cve": True, "eligibility": "pending-dependencies", "reason": "Waiting"}

    async def process(issue, **kwargs):
        assert kwargs["precomputed_triage"] is None
        assert kwargs["dry_run"] is dry_run
        return TriageState(jira_issue=issue)

    monkeypatch.setattr(multistream, "mcp_tools", gateway)
    monkeypatch.setattr(multistream, "run_tool", run_tool)
    monkeypatch.setattr(multistream.triage_agent, "run_workflow", process)
    result = await multistream.run_multistream_triage(
        _task(), upstream_agent_factory=lambda *_: pytest.fail("model called")
    )
    assert result["analysis"]["results"] == []
    assert set(result["outcomes"]) == {"RHEL-1", "RHEL-2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fix_version, expected",
    [
        ("rhel-9.8.z", False),
        ("rhel-10.2.z", False),
        ("rhel-9.6.z", True),
        ("rhel-10.0.z", True),
    ],
)
async def test_group_classifies_against_current_not_upcoming_streams(
    monkeypatch, stream_config, fix_version, expected
):
    async def run_tool(name, **kwargs):
        if name == "get_jira_details":
            details = _details(kwargs["issue_key"])
            details["fields"]["fixVersions"] = [{"name": fix_version}]
            return details
        assert name == "check_cve_triage_eligibility"
        return {"is_cve": True, "eligibility": "immediately", "reason": "Eligible"}

    monkeypatch.setattr(multistream, "run_tool", run_tool)
    members = await multistream._load_members(_task(), [])
    assert all(member["is_older_zstream"] is expected for member in members.values())


def test_group_prompt_uses_computed_stream_policy():
    prompt = multistream.render_template("multistream_triage/prompt.j2")
    assert "is_older_zstream" in prompt
    assert "upcoming" in prompt
    assert "configuration/tool error" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["another-issue", "not-affected"])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_precomputed_triage_handoff_safety(case, dry_run):
    result = _analysis().results[0]
    if case == "another-issue":
        result.data.jira_issue = "RHEL-2"
    if case == "not-affected":
        result.resolution = Resolution.NOT_AFFECTED
    with pytest.raises(ValueError):
        await multistream.triage_agent.run_workflow("RHEL-1", dry_run, None, precomputed_triage=result)


def test_group_agent_excludes_gateway_writes(monkeypatch):
    captured = {}

    def agent_constructor(**kwargs):
        captured.update(kwargs)
        return flexmock()

    monkeypatch.setattr(multistream, "ReasoningAgent", agent_constructor)
    monkeypatch.setattr(multistream, "get_chat_model", lambda: None)
    monkeypatch.setattr(multistream, "is_reasoning_enabled", lambda: False)
    tools = [flexmock(name=name) for name in ["clone_repository", "set_jira_fields", "open_merge_request"]]
    multistream.create_multistream_triage_agent(tools, {})
    assert captured["name"] == "MultiStreamTriageAgent"
    names = {tool.name for tool in captured["tools"]}
    assert "clone_repository" in names
    assert "set_jira_fields" not in names
    assert "open_merge_request" not in names
