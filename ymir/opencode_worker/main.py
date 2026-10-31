"""Entry point: one worker process, one loop per enabled agent profile."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ymir.common.base_utils import install_shutdown_handler, redis_client, run_task_loop
from ymir.common.logging_setup import configure_logging
from ymir.opencode_worker.client import OpenCodeClient, OpenCodeSettings
from ymir.opencode_worker.routing import DEFAULT_AGENTS, StageRoute, routes_for
from ymir.opencode_worker.worker import WorkerSettings, process_payload

logger = logging.getLogger(__name__)


def _agents() -> tuple[str, ...]:
    raw = os.getenv("OPENCODE_WORKER_AGENTS", "")
    if not raw.strip():
        return DEFAULT_AGENTS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _settings() -> WorkerSettings:
    result_dir = os.getenv("OPENCODE_RESULT_DIR", "")
    return WorkerSettings(
        max_attempts=int(os.getenv("OPENCODE_WORKER_MAX_ATTEMPTS", "2")),
        result_dir=Path(result_dir) if result_dir else None,
        resume_sessions=os.getenv("OPENCODE_RESUME_SESSIONS", "false").lower() == "true",
        session_ttl=int(os.getenv("OPENCODE_SESSION_TTL", "86400")),
    )


def _client() -> OpenCodeClient:
    return OpenCodeClient(
        OpenCodeSettings(
            base_url=os.getenv("OPENCODE_URL", "http://127.0.0.1:4096"),
            password=os.environ["OPENCODE_PASSWORD"],
            directory=os.getenv("OPENCODE_DIRECTORY", "/home/beeai/work"),
            request_timeout=float(os.getenv("OPENCODE_REQUEST_TIMEOUT", "60")),
            poll_interval=float(os.getenv("OPENCODE_POLL_INTERVAL", "2")),
            task_timeout=float(os.getenv("OPENCODE_TASK_TIMEOUT", "1800")),
        )
    )


def _require_dry_run() -> None:
    """Refuse to consume a queue when the fleet is not in dry-run mode.

    Compose gives ``mcp-gateway``, ``opencode-agent`` and this worker the same
    ``DRY_RUN`` value, so asserting it here is the local half of the guard. A
    queue consumer runs unattended: in live mode it would post real comments and
    open real merge requests with nobody watching. Set
    ``OPENCODE_REQUIRE_DRY_RUN=false`` only to run a live queue deliberately.
    """
    if os.getenv("OPENCODE_REQUIRE_DRY_RUN", "true").lower() != "true":
        return
    if os.getenv("DRY_RUN", "false").lower() != "true":
        raise SystemExit(
            "Refusing to start: DRY_RUN is not 'true'. Queue work is unattended, so "
            "live mode must be opted into explicitly with OPENCODE_REQUIRE_DRY_RUN=false."
        )


async def _serve_route(
    route: StageRoute,
    *,
    redis_url: str,
    settings: WorkerSettings,
    shutdown_event: asyncio.Event,
) -> None:
    concurrency = int(os.getenv("MAX_CONCURRENT_TASKS", "1"))
    logger.info("Agent %s listening on %s (max_concurrent=%d)", route.agent, route.queues, concurrency)
    async with redis_client(redis_url) as redis:
        await run_task_loop(
            redis,
            list(route.queues),
            lambda payload: process_payload(
                redis,
                payload,
                agent=route.agent,
                queue=route.queues[-1],
                settings=settings,
                client_factory=_client,
            ),
            max_concurrent=concurrency,
            shutdown_event=shutdown_event,
        )


async def main() -> None:
    configure_logging(level=logging.INFO, buffer_size=int(os.getenv("LOG_BUFFER_SIZE", 0)))
    _require_dry_run()
    settings = _settings()
    routes = routes_for(_agents(), os.getenv("CONTAINER_VERSION", "c10s"))
    shutdown_event = asyncio.Event()
    install_shutdown_handler(asyncio.get_running_loop(), shutdown_event)

    logger.info("OpenCode worker serving %d agent profile(s): %s", len(routes), [r.agent for r in routes])
    await asyncio.gather(
        *(
            _serve_route(
                route,
                redis_url=os.environ["REDIS_URL"],
                settings=settings,
                shutdown_event=shutdown_event,
            )
            for route in routes
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
