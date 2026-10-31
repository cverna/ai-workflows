"""Unit tests for queue → agent routing."""

from __future__ import annotations

import pytest

from ymir.common.constants import RedisQueues
from ymir.opencode_worker.routing import DEFAULT_AGENTS, routes_for


class TestRoutes:
    def test_triage_is_stream_independent(self):
        (route,) = routes_for(("triage",), "c10s")
        assert route.agent == "triage"
        assert route.queues == (
            RedisQueues.TRIAGE_QUEUE_TODO.value,
            RedisQueues.TRIAGE_QUEUE.value,
        )

    def test_stream_selects_the_matching_queue(self):
        (c9s,) = routes_for(("rebuild",), "c9s")
        (c10s,) = routes_for(("rebuild",), "c10s")
        assert c9s.queues[-1] == RedisQueues.REBUILD_QUEUE_C9S.value
        assert c10s.queues[-1] == RedisQueues.REBUILD_QUEUE_C10S.value

    def test_priority_twin_always_comes_first(self):
        for agent in ("triage", "rebase", "backport", "rebuild"):
            (route,) = routes_for((agent,), "c10s")
            assert route.queues[0].endswith("_todo")

    def test_one_route_per_agent(self):
        routes = routes_for(("triage", "rebuild"), "c10s")
        assert [r.agent for r in routes] == ["triage", "rebuild"]
        assert len({q for r in routes for q in r.queues}) == 4

    def test_unknown_agent_is_rejected(self):
        with pytest.raises(ValueError, match="unknown agent"):
            routes_for(("deploy",), "c10s")

    def test_unknown_stream_is_rejected(self):
        with pytest.raises(ValueError, match="c9s or c10s"):
            routes_for(("rebuild",), "c11s")

    def test_empty_selection_is_rejected(self):
        with pytest.raises(ValueError, match="no agents enabled"):
            routes_for((), "c10s")

    def test_default_covers_triage_and_rebuild(self):
        assert set(DEFAULT_AGENTS) == {"triage", "rebuild"}
