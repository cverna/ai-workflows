"""Turn a queued ``Task`` into one OpenCode session and record its verdict."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as aioredis

from ymir.common.base_utils import fix_await
from ymir.common.constants import RedisQueues
from ymir.common.issue_lock import issue_lock
from ymir.common.logging_setup import current_jira_issue
from ymir.common.models import ErrorData, ErrorListEntry, Task
from ymir.opencode_worker.client import OpenCodeClient, OpenCodeError, run_prompt
from ymir.opencode_worker.result import MissingResultError, extract_result, result_prompt_suffix

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], OpenCodeClient]

# Scoped per stage on purpose: a resumed session carries the previous answer
# with it, and a later stage endorsed that answer instead of doing its own work.
# Retries of the same stage may resume; different stages must start fresh.
SESSION_KEY = "opencode:session:{issue}:{agent}"


@dataclass(frozen=True)
class WorkerSettings:
    max_attempts: int = 2
    result_dir: Path | None = None
    resume_sessions: bool = False
    session_ttl: int = 86_400

    def lock_prefix(self, agent: str) -> str:
        return f"lock:opencode-{agent}:"


def issue_key_from(task: Task) -> str | None:
    """Triage tasks carry ``issue``; other stages use ``jira_issue``."""
    for key in ("issue", "jira_issue"):
        value = task.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def build_prompt(task: Task, issue_key: str, agent: str) -> str:
    topic = task.metadata.get("topic") or "the referenced Jira issue"
    prompt = (
        f"Continue the Ymir workflow for Jira issue {issue_key} ({topic}) "
        f"at the {agent} stage.\n\n"
        "Use the ymir-privileged_* tools for Jira, GitLab and maintainer/shared "
        "rule lookups. Decide only from evidence you actually collected; if the "
        "evidence is blocked or inconclusive, say so and route conservatively "
        "rather than assuming the package is safe."
    )
    # The fetcher already ran the eligibility check, so pass its routing facts
    # instead of making the agent re-derive them (and possibly disagree). Only
    # truthy facts are forwarded: the schema defaults every field to False/None,
    # so forwarding defaults would claim knowledge the queue did not have.
    routing = [
        (label, task.metadata[key])
        for key, label in (
            ("needs_internal_fix", "needs_internal_fix"),
            ("is_older_zstream", "is_older_zstream"),
            ("internal_target_branch", "internal_target_branch"),
        )
        if task.metadata.get(key)
    ]
    if routing:
        facts = ", ".join(
            f"{label}={str(value).lower() if isinstance(value, bool) else value}"
            for label, value in routing
        )
        prompt += (
            "\n\nThe queue passed these routing facts with the task: "
            f"{facts}. Use them rather than re-deriving them, and pass "
            "`needs_internal_fix` to the version-mapping helper. A mapped branch that "
            "does not exist yet is normal: RHEL-first fixes create it, stream-first "
            "fixes land on the CentOS Stream branch and are inherited."
        )
    return prompt + result_prompt_suffix()


async def _record_error(redis: aioredis.Redis, details: str, issue_key: str | None, task: Task | None) -> None:
    error_id = await fix_await(redis.incr(RedisQueues.ERROR_ID_COUNTER.value))
    entry = ErrorListEntry(
        error_id=error_id,
        task=task,
        error=ErrorData(details=details, jira_issue=issue_key or "unknown"),
    )
    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, entry.model_dump_json()))
    logger.error("Recorded worker error %s: %s", error_id, details)


def _persist(settings: WorkerSettings, agent: str, issue_key: str, payload: dict) -> Path | None:
    if settings.result_dir is None:
        return None
    settings.result_dir.mkdir(parents=True, exist_ok=True)
    target = settings.result_dir / f"{issue_key}-{agent}.json"
    target.write_text(json.dumps(payload, indent=2))
    return target


def session_key(issue_key: str, agent: str) -> str:
    return SESSION_KEY.format(issue=issue_key, agent=agent)


async def _resume_session(
    redis: aioredis.Redis, issue_key: str, agent: str, settings: WorkerSettings
) -> str | None:
    if not settings.resume_sessions:
        return None
    value = await fix_await(redis.get(session_key(issue_key, agent)))
    if isinstance(value, bytes):
        value = value.decode()
    return value or None


async def _remember_session(
    redis: aioredis.Redis, issue_key: str, agent: str, session_id: str, settings: WorkerSettings
) -> None:
    if not settings.resume_sessions:
        return
    await fix_await(redis.set(session_key(issue_key, agent), session_id, ex=settings.session_ttl))


async def _forget_session(redis: aioredis.Redis, issue_key: str, agent: str, settings: WorkerSettings) -> None:
    """Drop a session that produced no usable result, so a retry starts clean."""
    if not settings.resume_sessions:
        return
    await fix_await(redis.delete(session_key(issue_key, agent)))


async def process_payload(
    redis: aioredis.Redis,
    payload: bytes,
    *,
    agent: str,
    queue: str,
    settings: WorkerSettings,
    client_factory: ClientFactory,
) -> None:
    """Handle one queue payload. Never raises: failures are recorded or requeued."""
    try:
        task = Task.model_validate_json(payload)
    except Exception as error:
        await _record_error(redis, f"Malformed task payload: {error}", None, None)
        return

    issue_key = issue_key_from(task)
    if issue_key is None:
        await _record_error(redis, "Task metadata has no issue key", None, task)
        return
    current_jira_issue.set(issue_key)

    async with issue_lock(redis, issue_key, prefix=settings.lock_prefix(agent)) as token:
        if token is None:
            logger.info("%s is already being processed; dropping duplicate task", issue_key)
            return

        try:
            answer, session_id = await run_prompt(
                client_factory(),
                agent,
                f"{issue_key} {agent}",
                build_prompt(task, issue_key, agent),
                session_id=await _resume_session(redis, issue_key, agent, settings),
            )
            result = extract_result(answer)
        except MissingResultError as error:
            await _forget_session(redis, issue_key, agent, settings)
            await _record_error(redis, f"No usable result for {issue_key}: {error}", issue_key, task)
            return
        except OpenCodeError as error:
            await _requeue_or_error(redis, task, issue_key, agent, str(error), settings, queue)
            return
        except Exception as error:  # pragma: no cover - defensive
            # Never record an empty reason: a bare TimeoutError used to surface
            # as "Unexpected failure for ISSUE: " with nothing to diagnose.
            await _record_error(
                redis,
                f"Unexpected failure for {issue_key}: {type(error).__name__}: {error!r}",
                issue_key,
                task,
            )
            return

        await _remember_session(redis, issue_key, agent, session_id, settings)
        record = {
            "issue": issue_key,
            "agent": agent,
            "session_id": session_id,
            "user_triggered": task.user_triggered,
            "execution_id": str(task.execution_id),
            **result.model_dump(mode="json"),
        }
        path = _persist(settings, agent, issue_key, record)
        logger.info(
            "Processed %s at %s: resolution=%s confidence=%s%s",
            issue_key,
            agent,
            result.resolution.value,
            result.confidence,
            f" (saved to {path})" if path else "",
        )


async def _requeue_or_error(
    redis: aioredis.Redis,
    task: Task,
    issue_key: str,
    agent: str,
    reason: str,
    settings: WorkerSettings,
    queue: str,
) -> None:
    if task.attempts + 1 >= settings.max_attempts:
        await _forget_session(redis, issue_key, agent, settings)
        await _record_error(
            redis, f"{issue_key} failed after {task.attempts + 1} attempts: {reason}", issue_key, task
        )
        return
    retry = task.model_copy(update={"attempts": task.attempts + 1})
    await fix_await(redis.lpush(queue, retry.to_json()))
    logger.warning("Requeued %s after attempt %d (%s)", issue_key, task.attempts + 1, reason)
