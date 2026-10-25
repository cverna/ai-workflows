import json
from contextlib import asynccontextmanager

import pytest
from flexmock import flexmock
from redis.exceptions import WatchError

from ymir.common.multistream_triage import PROCESSING, QUEUE, RESULTS, MultiStreamTriageTask
from ymir.jira_issue_fetcher import multistream_triage_fetcher as multistream
from ymir.jira_issue_fetcher.jira_issue_fetcher import JiraIssueFetcher


def _issue(key="RHEL-1", package="curl", cve="CVE-2026-87863", **fields):
    return {
        "key": key,
        "fields": {
            "summary": f"{cve} {package}: vulnerability",
            "customfield_10669": package,
            "fixVersions": [{"name": "rhel-9.9.z"}],
            **fields,
        },
    }


def test_group_by_cve_and_downstream_package_including_singletons():
    tasks = multistream.group_trackers(
        [
            _issue("rhel-2"),
            _issue(),
            _issue(),  # Jira pages may repeat a tracker.
            _issue("RHEL-3", package="openssl"),
            _issue("RHEL-4", cve="CVE-2026-84784"),
        ]
    )
    assert [(t.cve, t.package, t.issues) for t in tasks] == [
        ("CVE-2026-84784", "curl", ["RHEL-4"]),
        ("CVE-2026-87863", "curl", ["RHEL-1", "RHEL-2"]),
        ("CVE-2026-87863", "openssl", ["RHEL-3"]),
    ]


@pytest.mark.parametrize(
    "issue",
    [
        _issue(cve="not-a-cve"),
        _issue(cve="CVE-2026-87863 CVE-2026-84784"),
        _issue(package=None),
        _issue(package="curl:stream/module"),
        _issue(fixVersions=[]),
        _issue(fixVersions=[{"name": "one"}, {"name": "two"}]),
        _issue(fixVersions=[{}]),
        _issue(key="invalid"),
    ],
)
def test_unsupported_trackers_are_logged_not_silently_reassigned(issue, caplog):
    assert multistream.group_trackers([issue]) == []
    assert "Skipping" in caplog.text


@pytest.fixture
def fetcher(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MULTISTREAM_TRIAGE_QUERY", 'project = RHEL AND component = "curl"')
    monkeypatch.setenv("DRY_RUN", "true")
    return multistream.MultiStreamTriageFetcher()


def test_group_query_is_required(monkeypatch):
    monkeypatch.delenv("MULTISTREAM_TRIAGE_QUERY", raising=False)
    with pytest.raises(ValueError, match="MULTISTREAM_TRIAGE_QUERY"):
        multistream.MultiStreamTriageFetcher()


@pytest.mark.parametrize("dry_run_env", [None, "false", "true"])
def test_fetcher_uses_standard_dry_run_setting(fetcher, monkeypatch, dry_run_env):
    if dry_run_env is None:
        monkeypatch.delenv("DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("DRY_RUN", dry_run_env)
    assert multistream.MultiStreamTriageFetcher().dry_run is (dry_run_env == "true")


@pytest.mark.asyncio
async def test_search_uses_group_query_and_paginates_before_grouping(fetcher):
    pages = []

    def request(url, json_data):
        pages.append(json_data)
        assert url == "https://jira.example.com/rest/api/3/search/jql"
        assert json_data["jql"] == fetcher.query
        assert "summary" in json_data["fields"]
        assert "customfield_10669" in json_data["fields"]
        if len(pages) == 1:
            return {"issues": [_issue()], "nextPageToken": "next"}
        assert json_data["nextPageToken"] == "next"
        return {"issues": [_issue("RHEL-2")]}

    async def no_rate_limit():
        pass

    flexmock(fetcher).should_receive("_rate_limit").replace_with(no_rate_limit)
    flexmock(fetcher).should_receive("_make_request_with_retries").replace_with(request)
    tasks = multistream.group_trackers(await fetcher.search_issues())
    assert len(pages) == 2
    assert tasks[0].issues == ["RHEL-1", "RHEL-2"]


@pytest.mark.asyncio
async def test_existing_fetcher_keeps_default_search_fields(fetcher):
    def request(url, json_data):
        assert "summary" not in json_data["fields"]
        return {"issues": []}

    async def no_rate_limit():
        pass

    flexmock(fetcher).should_receive("_rate_limit").replace_with(no_rate_limit)
    flexmock(fetcher).should_receive("_make_request_with_retries").replace_with(request)
    assert await JiraIssueFetcher.search_issues(fetcher) == []


@pytest.mark.asyncio
async def test_preview_does_not_connect_to_redis(fetcher, capsys):
    async def search():
        return [_issue()]

    flexmock(fetcher).should_receive("search_issues").replace_with(search)
    flexmock(multistream).should_receive("redis_client").never()
    await fetcher.run()
    tasks = json.loads(capsys.readouterr().out)
    assert tasks[0]["issues"] == ["RHEL-1"]


@pytest.mark.asyncio
async def test_empty_candidates_do_not_open_a_transaction():
    redis = flexmock()
    redis.should_receive("pipeline").never()
    assert await multistream.enqueue_groups(redis, []) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["empty", "queued", "processing", "completed", "failed", "conflict", "malformed"]
)
async def test_atomic_enqueue_deduplicates_active_groups_and_finished_members(state):
    task = multistream.group_trackers([_issue(), _issue("RHEL-2")])[0]
    previous = task.model_copy(update={"issues": ["RHEL-1"]})
    rows = {QUEUE: [], PROCESSING: [], RESULTS: []}
    if state in ("queued", "processing"):
        rows[QUEUE if state == "queued" else PROCESSING] = [previous.model_dump_json()]
    if state in ("completed", "failed"):
        rows[RESULTS] = [
            json.dumps(
                {"task": previous.model_dump(mode="json"), "error": "oops"}
                if state == "failed"
                else {"task": previous.model_dump(mode="json"), "outcomes": {}}
            )
        ]
    if state == "malformed":
        rows[QUEUE] = ["invalid json"]
    pushes = []
    attempts = []
    pipe = flexmock()

    async def watch(*keys):
        assert keys == (QUEUE, PROCESSING, RESULTS)

    async def lrange(key, start, end):
        assert (start, end) == (0, -1)
        return rows[key]

    async def execute():
        attempts.append(True)
        if state == "conflict" and len(attempts) == 1:
            rows[QUEUE] = [previous.model_dump_json()]
            raise WatchError()
        return [1] * len(pushes)

    @asynccontextmanager
    async def pipeline():
        yield pipe

    pipe.should_receive("watch").replace_with(watch)
    pipe.should_receive("lrange").replace_with(lrange)
    pipe.should_receive("multi")
    pipe.should_receive("lpush").replace_with(lambda key, payload: pushes.append((key, payload)))
    pipe.should_receive("execute").replace_with(execute)
    redis = flexmock()
    redis.should_receive("pipeline").replace_with(pipeline)
    if state == "malformed":
        with pytest.raises(ValueError):
            await multistream.enqueue_groups(redis, [task])
        assert pushes == []
        return
    count = await multistream.enqueue_groups(redis, [task])
    if state in ("queued", "processing", "conflict"):
        assert count == 0
    else:
        assert count == 1
        restored = MultiStreamTriageTask.model_validate_json(pushes[-1][1])
        assert restored.issues == (["RHEL-2"] if state in ("completed", "failed") else task.issues)
        assert pushes[-1][0] == QUEUE
