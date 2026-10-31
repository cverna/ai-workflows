"""Unit tests for the queue-driven OpenCode worker."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from flexmock import flexmock

from ymir.common.constants import RedisQueues
from ymir.common.models import Resolution, Task
from ymir.opencode_worker import worker as worker_module
from ymir.opencode_worker.client import OpenCodeError
from ymir.opencode_worker.result import MissingResultError, TriageResult, extract_result
from ymir.opencode_worker.worker import (
    WorkerSettings,
    build_prompt,
    process_payload,
    session_key,
)

VALID_BLOCK = """Report text.

```json
{"resolution": "backport", "package": "bash", "target_branch": "rhel-9.8.0",
 "confidence": "medium", "summary": "Backport needed",
 "justification_category": null, "evidence": "checked source",
 "proposed_comment": null, "proposed_labels": []}
```
"""


def _settings(**kwargs) -> WorkerSettings:
    return WorkerSettings(**{"max_attempts": 2, "result_dir": None, **kwargs})


def _client_returning(text: str):
    async def _inner(*_args, **_kwargs):
        return text, "ses_test"

    flexmock(worker_module).should_receive("run_prompt").replace_with(_inner)
    return lambda: None


class _FakeRedis:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []
        self.incremented = 0
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    async def get(self, key):  # pragma: no cover - trivial
        return self.store.get(key)

    async def set(self, key, value, ex=None):  # pragma: no cover - trivial
        self.store[key] = value

    async def delete(self, key):  # pragma: no cover - trivial
        self.deleted.append(key)
        self.store.pop(key, None)

    async def incr(self, key):  # pragma: no cover - trivial
        self.incremented += 1
        return self.incremented

    async def lpush(self, queue, value):  # pragma: no cover - trivial
        self.pushed.append((queue, value))

    async def rpush(self, queue, value):  # pragma: no cover - trivial
        self.pushed.append((queue, value))


@pytest.fixture
def lock_free():
    @asynccontextmanager
    async def _lock(*_args, **_kwargs):
        yield "token"

    flexmock(worker_module).should_receive("issue_lock").replace_with(_lock)


class TestResultContract:
    def test_extracts_last_fenced_block(self):
        result = extract_result(VALID_BLOCK)
        assert result.resolution is Resolution.BACKPORT
        assert result.confidence == "medium"

    def test_rejects_missing_block(self):
        with pytest.raises(MissingResultError):
            extract_result("no json here")

    def test_rejects_invalid_resolution(self):
        bad = VALID_BLOCK.replace('"backport"', '"nonsense"')
        with pytest.raises(MissingResultError):
            extract_result(bad)

    def test_prompt_requires_the_block(self):
        prompt = build_prompt(Task.from_issue("RHEL-1"), "RHEL-1", "triage")
        assert "```json" in prompt
        assert "RHEL-1" in prompt


class TestProcessPayload:
    @pytest.mark.asyncio
    async def test_records_result_without_touching_remote(self, lock_free, tmp_path):
        redis = _FakeRedis()
        settings = _settings(result_dir=tmp_path)

        await process_payload(
            redis,
            Task.from_issue("RHEL-1234").to_json().encode(),
            settings=settings,
            agent="triage",
            queue=RedisQueues.TRIAGE_QUEUE.value,
            client_factory=_client_returning(VALID_BLOCK),
        )

        saved = json.loads((tmp_path / "RHEL-1234-triage.json").read_text())
        assert saved["resolution"] == "backport"
        assert saved["issue"] == "RHEL-1234"
        assert redis.pushed == []

    @pytest.mark.asyncio
    async def test_malformed_payload_goes_to_error_list(self, lock_free):
        redis = _FakeRedis()
        await process_payload(
            redis, b"not json", settings=_settings(), agent="triage", queue=RedisQueues.TRIAGE_QUEUE.value, client_factory=_client_returning("")
        )
        assert redis.pushed and redis.pushed[0][0] == RedisQueues.ERROR_LIST.value

    @pytest.mark.asyncio
    async def test_missing_issue_key_goes_to_error_list(self, lock_free):
        redis = _FakeRedis()
        task = Task(metadata={"topic": "unrelated"})
        await process_payload(
            redis, task.to_json().encode(), settings=_settings(), agent="triage", queue=RedisQueues.TRIAGE_QUEUE.value, client_factory=_client_returning("")
        )
        assert redis.pushed[0][0] == RedisQueues.ERROR_LIST.value

    @pytest.mark.asyncio
    async def test_missing_result_block_goes_to_error_list(self, lock_free):
        redis = _FakeRedis()
        await process_payload(
            redis,
            Task.from_issue("RHEL-9").to_json().encode(),
            settings=_settings(),
            agent="triage",
            queue=RedisQueues.TRIAGE_QUEUE.value,
            client_factory=_client_returning("prose only"),
        )
        assert redis.pushed[0][0] == RedisQueues.ERROR_LIST.value

    @pytest.mark.asyncio
    async def test_requeues_on_transport_failure_then_gives_up(self, lock_free):
        redis = _FakeRedis()
        task = Task.from_issue("RHEL-77")

        def failing(*_args, **_kwargs):
            async def _inner():
                raise OpenCodeError("server unavailable")

            return _inner()

        flexmock(worker_module).should_receive("run_prompt").replace_with(failing)

        await process_payload(
            redis, task.to_json().encode(), settings=_settings(max_attempts=2), agent="triage", queue=RedisQueues.TRIAGE_QUEUE.value, client_factory=lambda: None
        )
        assert redis.pushed[0][0] == RedisQueues.TRIAGE_QUEUE.value
        requeued = Task.model_validate_json(redis.pushed[0][1])
        assert requeued.attempts == 1

        redis.pushed.clear()
        await process_payload(
            redis, requeued.to_json().encode(), settings=_settings(max_attempts=2), agent="triage", queue=RedisQueues.TRIAGE_QUEUE.value, client_factory=lambda: None
        )
        assert redis.pushed[0][0] == RedisQueues.ERROR_LIST.value

    @pytest.mark.asyncio
    async def test_lock_contention_drops_duplicate(self):
        @asynccontextmanager
        async def _busy(*_args, **_kwargs):
            yield None

        flexmock(worker_module).should_receive("issue_lock").replace_with(_busy)
        redis = _FakeRedis()

        await process_payload(
            redis,
            Task.from_issue("RHEL-55").to_json().encode(),
            settings=_settings(),
            agent="triage",
            queue=RedisQueues.TRIAGE_QUEUE.value,
            client_factory=lambda: None,
        )
        assert redis.pushed == []


class TestSessionScope:
    def test_sessions_are_scoped_per_stage(self):
        assert session_key("RHEL-1", "triage") != session_key("RHEL-1", "rebuild")
        assert session_key("RHEL-1", "triage") == "opencode:session:RHEL-1:triage"

    @pytest.mark.asyncio
    async def test_each_stage_stores_its_own_session(self, lock_free):
        redis = _FakeRedis()
        settings = _settings(resume_sessions=True)

        for agent in ("triage", "rebuild"):
            await process_payload(
                redis,
                Task.from_issue("RHEL-42").to_json().encode(),
                settings=settings,
                agent=agent,
                queue=RedisQueues.TRIAGE_QUEUE.value,
                client_factory=_client_returning(VALID_BLOCK),
            )

        assert set(redis.store) == {
            "opencode:session:RHEL-42:triage",
            "opencode:session:RHEL-42:rebuild",
        }

    @pytest.mark.asyncio
    async def test_unusable_result_forgets_the_session(self, lock_free):
        redis = _FakeRedis()
        redis.store["opencode:session:RHEL-7:triage"] = "ses_old"
        settings = _settings(resume_sessions=True)

        await process_payload(
            redis,
            Task.from_issue("RHEL-7").to_json().encode(),
            settings=settings,
            agent="triage",
            queue=RedisQueues.TRIAGE_QUEUE.value,
            client_factory=_client_returning("prose only"),
        )

        assert "opencode:session:RHEL-7:triage" in redis.deleted
        assert "opencode:session:RHEL-7:triage" not in redis.store
