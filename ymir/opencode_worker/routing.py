"""Map queues to agent profiles for the single-worker dispatcher.

One container can serve every stage: each queue is owned by exactly one agent
profile, and each profile gets its own ``run_task_loop`` so per-agent priority
twins, concurrency and shutdown stay intact. Keeping the stages on separate
queues preserves per-stage retry and permissions even though the deployment is
now a single Deployment.
"""

from __future__ import annotations

from dataclasses import dataclass

from ymir.common.constants import RedisQueues

# Stage name -> (c9s queue, c10s queue). Streaming stages differ because the
# containers carry different toolchains; triage is stream-independent.
STREAM_QUEUES: dict[str, tuple[str, str]] = {
    "rebase": (RedisQueues.REBASE_QUEUE_C9S.value, RedisQueues.REBASE_QUEUE_C10S.value),
    "backport": (RedisQueues.BACKPORT_QUEUE_C9S.value, RedisQueues.BACKPORT_QUEUE_C10S.value),
    "rebuild": (RedisQueues.REBUILD_QUEUE_C9S.value, RedisQueues.REBUILD_QUEUE_C10S.value),
}

STREAM_INDEPENDENT_QUEUES: dict[str, str] = {
    "triage": RedisQueues.TRIAGE_QUEUE.value,
}

DEFAULT_AGENTS = ("triage", "rebuild")


@dataclass(frozen=True)
class StageRoute:
    """One agent profile and the queues it consumes, priority twin first."""

    agent: str
    queues: tuple[str, ...]


def _stream_index(container_version: str) -> int:
    version = (container_version or "c10s").strip().lower()
    if version == "c9s":
        return 0
    if version == "c10s":
        return 1
    raise ValueError(f"CONTAINER_VERSION must be c9s or c10s, not {container_version!r}")


def routes_for(agents: tuple[str, ...], container_version: str) -> list[StageRoute]:
    """Build one route per enabled agent, filtered to the container's stream."""
    stream = _stream_index(container_version)
    routes: list[StageRoute] = []
    for agent in agents:
        name = agent.strip().lower()
        if not name:
            continue
        if name in STREAM_INDEPENDENT_QUEUES:
            base = STREAM_INDEPENDENT_QUEUES[name]
        elif name in STREAM_QUEUES:
            base = STREAM_QUEUES[name][stream]
        else:
            raise ValueError(
                f"unknown agent {name!r}; expected one of "
                f"{sorted({*STREAM_INDEPENDENT_QUEUES, *STREAM_QUEUES})}"
            )
        routes.append(
            StageRoute(agent=name, queues=(RedisQueues.priority_twin(base), base))
        )
    if not routes:
        raise ValueError("no agents enabled; set OPENCODE_WORKER_AGENTS")
    return routes
