import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from flexmock import flexmock
from pydantic import ValidationError

from ymir.agents import tasks, triage_agent
from ymir.common.models import TriageInputSchema
from ymir.tools.privileged.utils import ACTIVE_WORKSPACE_MARKER


@pytest.mark.asyncio
@pytest.mark.parametrize("second_issue", ["RHEL-1", "RHEL-2"])
async def test_concurrent_triage_workflows_own_separate_workspaces(tmp_path, monkeypatch, second_issue):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    shared_clone = tmp_path / "libmodulemd-upstream"
    shared_clone.mkdir()
    sentinel = shared_clone / "keep"
    sentinel.write_text("another run")
    workspaces = []
    both_running = asyncio.Event()

    async def run_isolated(issue):
        async with triage_agent._triage_workspace(issue) as working_directory:
            workspaces.append(working_directory)
            assert (working_directory / ACTIVE_WORKSPACE_MARKER).is_file()
            clone = working_directory / "libmodulemd-upstream"
            clone.mkdir()
            (clone / "keep").write_text(issue)
            if len(workspaces) == 2:
                both_running.set()
            await both_running.wait()
            assert (clone / "keep").read_text() == issue
            return issue

    result = await asyncio.wait_for(
        asyncio.gather(run_isolated("RHEL-1"), run_isolated(second_issue)), timeout=5
    )

    assert result == ["RHEL-1", second_issue]
    assert len(set(workspaces)) == 2
    assert all(path.is_relative_to(tmp_path / "Triage") for path in workspaces)
    assert all(path.name.startswith("run-") for path in workspaces)
    assert all(not path.exists() for path in workspaces)
    assert sentinel.read_text() == "another run"


@pytest.mark.asyncio
async def test_cancelled_triage_cleans_only_its_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    other_workspace = tmp_path / "Triage" / "RHEL-2" / "other"
    other_workspace.mkdir(parents=True)
    workspaces = []

    with pytest.raises(asyncio.CancelledError):
        async with triage_agent._triage_workspace("RHEL-1") as working_directory:
            workspaces.append(working_directory)
            raise asyncio.CancelledError

    assert not workspaces[0].exists()
    assert other_workspace.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", ["", ".", "..", "../RHEL-1", "/RHEL-1", "RHEL-1/other"])
async def test_triage_rejects_invalid_workspace_issue(tmp_path, monkeypatch, issue):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    with pytest.raises(ValueError, match="Invalid jira_issue"):
        await triage_agent.run_workflow(issue, True, None)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_triage_workspace_rejects_parent_symlink_escape(tmp_path, monkeypatch):
    base = tmp_path / "git-repos"
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "Triage").mkdir(parents=True)
    (base / "Triage" / "RHEL-1").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(base))

    with pytest.raises(ValueError, match="under GIT_REPO_BASEPATH"):
        async with triage_agent._triage_workspace("RHEL-1"):
            pytest.fail("escaped the shared volume")
    assert not list(outside.iterdir())


@pytest.mark.asyncio
async def test_triage_sets_shell_workspace_without_changing_process_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://unused")
    mock_env = {"GIT_CONFIG_GLOBAL": "/mock/gitconfig"}
    monkeypatch.setattr(triage_agent, "get_mock_local_tool_env", lambda _: mock_env)
    original_cwd = Path.cwd()
    captured = {}

    @asynccontextmanager
    async def gateway(_url, *, call_meta):
        captured["meta"] = call_meta
        yield []

    async def eligibility(*args, **kwargs):
        return {
            "is_cve": True,
            "eligibility": "pending-dependencies",
            "reason": "Waiting for sibling",
            "pending_zstream_issues": ["RHEL-2"],
        }

    def factory(_tools, options):
        captured["options"] = options
        return flexmock()

    monkeypatch.setattr(triage_agent, "mcp_tools", gateway)
    monkeypatch.setattr(triage_agent, "run_tool", eligibility)
    await triage_agent.run_workflow("RHEL-1", True, factory)

    workspace = captured["options"]["working_directory"]
    assert captured["meta"] == {"jira_issue": "RHEL-1"}
    assert captured["options"]["env"] == mock_env
    assert Path.cwd() == original_cwd
    assert not workspace.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["gateway", "factory"])
async def test_triage_entry_failure_cleans_workspace(tmp_path, monkeypatch, failure_stage):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://unused")
    workspace_paths = []

    @asynccontextmanager
    async def failing_gateway(_url, *, call_meta):
        workspace_paths.append(next((tmp_path / "Triage" / "RHEL-1").iterdir()))
        if failure_stage == "gateway":
            raise ConnectionError("gateway unavailable")
        yield []

    def failing_factory(*args):
        raise ConnectionError("factory unavailable")

    monkeypatch.setattr(triage_agent, "mcp_tools", failing_gateway)
    monkeypatch.setattr(triage_agent, "get_mock_local_tool_env", lambda _: None)
    with pytest.raises(ConnectionError):
        await triage_agent.run_workflow("RHEL-1", True, failing_factory)
    assert not workspace_paths[0].exists()


@pytest.mark.asyncio
async def test_triage_prompt_requires_workspace():
    with pytest.raises(ValidationError, match="working_directory"):
        triage_agent.TriagePromptInput(issue="RHEL-1")
    with pytest.raises(TypeError, match="working_directory"):
        await triage_agent.render_prompt(TriageInputSchema(issue="RHEL-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [None, Path("relative/workspace")])
async def test_triage_prompt_rejects_invalid_workspace(workspace):
    with pytest.raises(ValidationError, match="working_directory"):
        await triage_agent.render_prompt(TriageInputSchema(issue="RHEL-1"), working_directory=workspace)


@pytest.mark.asyncio
async def test_prompt_uses_execution_workspace_for_every_clone(tmp_path, monkeypatch):
    async def not_older(_version):
        return False

    monkeypatch.setattr(triage_agent, "is_older_zstream", not_older)
    workspace = tmp_path / "Triage" / "RHEL-1" / "execution"
    prompt = await triage_agent.render_prompt(TriageInputSchema(issue="RHEL-1"), working_directory=workspace)
    assert str(workspace) in prompt
    assert "Do not use shared package-level paths" in prompt
    assert "including shell commands" in prompt


@pytest.mark.asyncio
async def test_applicability_requires_execution_workspace():
    with pytest.raises(TypeError, match="workspace_dir"):
        await tasks.clone_and_prep_sources(
            package="bash", dist_git_branch="c10s", available_tools=[], jira_issue="RHEL-1"
        )


@pytest.mark.asyncio
async def test_applicability_clone_stays_in_execution_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    workspace = tmp_path / "Triage" / "RHEL-1" / "execution"
    expected_clone = workspace / "applicability" / "bash"
    prep_tool = flexmock(options={})
    monkeypatch.setattr(tasks, "RunPackagePrepTool", lambda: prep_tool)

    async def run_tool(name, **kwargs):
        if name == "clone_repository":
            assert Path(kwargs["clone_path"]) == expected_clone
        return "Prep succeeded"

    monkeypatch.setattr(tasks, "run_tool", run_tool)
    monkeypatch.setattr(tasks, "get_unpacked_sources", lambda *args, **kwargs: expected_clone / "sources")
    clone, _, success, _ = await tasks.clone_and_prep_sources(
        package="bash",
        dist_git_branch="c10s",
        available_tools=[],
        jira_issue="RHEL-1",
        workspace_dir=workspace,
    )
    assert clone == expected_clone
    assert success
