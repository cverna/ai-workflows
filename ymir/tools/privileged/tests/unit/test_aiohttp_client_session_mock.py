import json
import re

import pytest

from ymir.tools.privileged.aiohttp_client_session_mock import aiohttpClientSessionMock


@pytest.fixture
def mock_session(monkeypatch, tmp_path):
    base_url = "https://jira.example.test"
    monkeypatch.setenv("JIRA_MOCK_FILES", str(tmp_path))

    regexes = {
        "issue_get_regex": rf"{re.escape(base_url)}/rest/api/3/issue/([A-Z0-9-]+)",
        "transitions_get_regex": rf"{re.escape(base_url)}/rest/api/3/issue/([A-Z0-9-]+)/transitions",
        "remote_link_get_regex": rf"{re.escape(base_url)}/rest/api/3/issue/([A-Z0-9-]+)/remotelink",
        "comment_post_regex": rf"{re.escape(base_url)}/rest/api/[2-3]/issue/([A-Z0-9-]+)/comment",
        "search_post_regex": rf"{re.escape(base_url)}/rest/api/3/search/jql",
        "user_get_regex": rf"{re.escape(base_url)}/rest/api/3/user",
    }
    for name, pattern in regexes.items():
        monkeypatch.setattr(aiohttpClientSessionMock, name, re.compile(pattern))

    return aiohttpClientSessionMock(), base_url, tmp_path


@pytest.mark.asyncio
async def test_get_issue_returns_namespace_response(mock_session):
    session, base_url, mock_dir = mock_session
    issue = {"key": "RHEL-12345", "fields": {"summary": "test", "labels": ["triage"]}}
    (mock_dir / "RHEL-12345").write_text(json.dumps(issue))

    async with session.get(
        f"{base_url}/rest/api/3/issue/RHEL-12345",
        params={"fields": "summary"},
    ) as response:
        assert response.status == 200
        response.raise_for_status()
        assert await response.json() == {"key": "RHEL-12345", "fields": {"summary": "test"}}


@pytest.mark.asyncio
async def test_put_issue_updates_mock_file(mock_session):
    session, base_url, mock_dir = mock_session
    issue_path = mock_dir / "RHEL-12345"
    issue_path.write_text(json.dumps({"key": "RHEL-12345", "fields": {"labels": []}}))

    async with session.put(
        f"{base_url}/rest/api/3/issue/RHEL-12345",
        json={"fields": {"summary": "updated"}},
    ) as response:
        response.raise_for_status()

    assert json.loads(issue_path.read_text())["fields"]["summary"] == "updated"


@pytest.mark.asyncio
async def test_post_comment_updates_mock_file(mock_session):
    session, base_url, mock_dir = mock_session
    issue_path = mock_dir / "RHEL-12345"
    issue_path.write_text(
        json.dumps(
            {
                "key": "RHEL-12345",
                "fields": {"comment": {"comments": [], "maxResults": 0, "total": 0}},
            }
        )
    )

    async with session.post(
        f"{base_url}/rest/api/3/issue/RHEL-12345/comment",
        json={"body": "comment"},
    ) as response:
        response.raise_for_status()

    comments = json.loads(issue_path.read_text())["fields"]["comment"]
    assert comments["total"] == 1
    assert comments["comments"][0]["body"] == "comment"


@pytest.mark.asyncio
async def test_dev_status_summary_reports_no_linked_work(mock_session):
    """An unmocked dev-status raised NotImplementedError, which reads as breakage."""
    session, base_url, _ = mock_session

    async with session.get(f"{base_url}/rest/dev-status/1.0/issue/summary?issueId=123") as response:
        response.raise_for_status()
        data = await response.json()

    assert data["summary"]["repository"]["byInstanceType"] == {}
    assert data["summary"]["pullrequest"]["byInstanceType"] == {}


@pytest.mark.asyncio
async def test_search_answers_key_restricted_jql_from_fixtures(mock_session):
    session, base_url, mock_dir = mock_session
    for key, summary in (("RHEL-1", "one"), ("RHEL-2", "two")):
        (mock_dir / key).write_text(
            json.dumps(
                {
                    "key": key,
                    "id": key.split("-")[1],
                    "fields": {
                        "summary": summary,
                        "labels": ["Security"],
                        "fixVersions": [{"name": "rhel-9.8.z"}],
                    },
                }
            )
        )

    async with session.post(
        f"{base_url}/rest/api/3/search/jql",
        json={"jql": "key in (RHEL-1, RHEL-2) ORDER BY key", "maxResults": 50},
    ) as response:
        response.raise_for_status()
        data = await response.json()

    assert [i["key"] for i in data["issues"]] == ["RHEL-1", "RHEL-2"]
    assert data["issues"][1]["fields"]["summary"] == "two"
    assert data["issues"][1]["fields"]["fixVersions"] == [{"name": "rhel-9.8.z"}]


@pytest.mark.asyncio
async def test_search_ignores_unknown_keys_and_unindexed_jql(mock_session):
    session, base_url, _ = mock_session

    async with session.post(
        f"{base_url}/rest/api/3/search/jql",
        json={"jql": "project = RHEL AND labels = Security ORDER BY created DESC"},
    ) as response:
        response.raise_for_status()
        assert await response.json() == {"issues": []}

    async with session.post(
        f"{base_url}/rest/api/3/search/jql",
        json={"jql": "key in (RHEL-404)"},
    ) as response:
        response.raise_for_status()
        assert await response.json() == {"issues": []}
