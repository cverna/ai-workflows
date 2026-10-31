"""Drive the real client against a stub HTTP server.

The unit tests stub ``run_prompt``, which hid a response-envelope bug: the API
returns ``{"data": {"id": ...}}`` for session creation and session state but a
bare ``{"data": [...]}`` for context. These tests keep the client honest.

The stub also reproduces the resume hazard measured against the real server: a
finished session keeps reporting its old ``outcome`` *and* its old ``time.idle``
until the next run completes, so a waiter keyed on outcome alone returns the
previous answer.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ymir.opencode_worker.client import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeSettings,
    run_prompt,
    unwrap,
)


def _settings(base_url: str) -> OpenCodeSettings:
    return OpenCodeSettings(
        base_url=base_url,
        password="secret",
        directory="/work",
        request_timeout=5,
        poll_interval=0.01,
        task_timeout=5,
    )


class _Stub:
    """Models an OpenCode server that keeps a session's old outcome until idle."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.polls = 0
        self.idle = 0
        self.pending = 0
        self.sessions: set[str] = set()

    async def create(self, request: web.Request) -> web.Response:
        body = await request.json()
        assert body["location"] == {"directory": "/work"}
        sid = f"ses_{len(self.sessions) + 1}"
        self.sessions.add(sid)
        return web.json_response({"data": {"id": sid}})

    async def set_agent(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if sid not in self.sessions:
            return web.json_response({"error": "missing"}, status=404)
        # The real endpoint answers 204 No Content, not JSON.
        return web.Response(status=204)

    async def prompt(self, request: web.Request) -> web.Response:
        self.prompts.append((await request.json())["text"])
        self.pending += 1
        self.polls = 0
        return web.json_response({"data": {"id": "msg_1"}})

    async def session(self, request: web.Request) -> web.Response:
        self.polls += 1
        # A prompt finishes on the second poll after it lands, which keeps the
        # old outcome/idle visible on the first poll.
        if self.pending and self.polls > 1:
            self.pending -= 1
            self.idle += 1
        return web.json_response(
            {"data": {"id": request.match_info["sid"], "outcome": "succeeded", "time": {"idle": self.idle}}}
        )

    async def context(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"data": [{"type": "assistant", "content": [{"type": "text", "text": f"turn-{self.idle}"}]}]}
        )


def _app(stub: _Stub) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/session", stub.create)
    app.router.add_post("/api/session/{sid}/agent", stub.set_agent)
    app.router.add_post("/api/session/{sid}/prompt", stub.prompt)
    app.router.add_get("/api/session/{sid}", stub.session)
    app.router.add_get("/api/session/{sid}/context", stub.context)
    return app


@pytest.mark.asyncio
async def test_session_envelope_and_idle_wait():
    stub = _Stub()
    async with TestServer(_app(stub)) as server:
        client = OpenCodeClient(_settings(str(server.make_url(""))))

        sid = await client.create_session("triage", "title")
        assert sid == "ses_1"
        assert await client.idle_since(sid) == 0

        await client.prompt(sid, "analyse this")
        assert stub.prompts == ["analyse this"]
        # Outcome is already "succeeded" from the stub's perspective; only the
        # idle marker proves a new run finished.
        assert await client.wait_for_outcome(sid, after_idle=0) == "succeeded"
        assert await client.final_text(sid) == "turn-1"


@pytest.mark.asyncio
async def test_run_prompt_returns_session_and_text():
    stub = _Stub()
    async with TestServer(_app(stub)) as server:
        client = OpenCodeClient(_settings(str(server.make_url(""))))
        answer, sid = await run_prompt(client, "triage", "title", "hello")
        assert answer == "turn-1"
        assert sid == "ses_1"


@pytest.mark.asyncio
async def test_resume_continues_the_same_session():
    stub = _Stub()
    async with TestServer(_app(stub)) as server:
        client = OpenCodeClient(_settings(str(server.make_url(""))))

        _, sid = await run_prompt(client, "triage", "title", "first")
        answer, sid2 = await run_prompt(client, "backport", "title", "second", session_id=sid)

        assert sid2 == sid
        assert answer == "turn-2", "resumed run must not return the previous answer"
        assert len(stub.sessions) == 1
        assert stub.prompts == ["first", "second"]


@pytest.mark.asyncio
async def test_missing_session_falls_back_to_a_new_one():
    stub = _Stub()
    async with TestServer(_app(stub)) as server:
        client = OpenCodeClient(_settings(str(server.make_url(""))))
        answer, sid = await run_prompt(client, "triage", "title", "hello", session_id="ses_gone")
        assert sid == "ses_1"
        assert answer == "turn-1"


@pytest.mark.asyncio
async def test_http_error_is_wrapped():
    async def fail(_request: web.Request) -> web.Response:
        return web.json_response({"error": "nope"}, status=401)

    app = web.Application()
    app.router.add_post("/api/session", fail)
    async with TestServer(app) as server:
        client = OpenCodeClient(_settings(str(server.make_url(""))))
        with pytest.raises(OpenCodeError, match="401"):
            await client.create_session("triage", "title")


@pytest.mark.asyncio
async def test_missing_session_id_is_reported():
    async def empty(_request: web.Request) -> web.Response:
        return web.json_response({"data": {}})

    app = web.Application()
    app.router.add_post("/api/session", empty)
    async with TestServer(app) as server:
        client = OpenCodeClient(_settings(str(server.make_url(""))))
        with pytest.raises(OpenCodeError, match="no id"):
            await client.create_session("triage", "title")


def test_settings_require_a_password():
    with pytest.raises(ValueError, match="password"):
        OpenCodeSettings(password="")


@pytest.mark.asyncio
async def test_set_agent_accepts_no_content():
    """A 204 must not be treated as a resume failure.

    The real agent-switch endpoint returns 204; parsing it as JSON made every
    resumed stage silently start a fresh session.
    """
    stub = _Stub()
    async with TestServer(_app(stub)) as server:
        client = OpenCodeClient(_settings(str(server.make_url(""))))
        sid = await client.create_session("triage", "title")
        await client.set_agent(sid, "rebuild")  # must not raise


def test_unwrap_accepts_both_shapes():
    assert unwrap({"data": {"id": "a"}}) == {"id": "a"}
    assert unwrap({"id": "b"}) == {"id": "b"}
    assert unwrap({"data": "not-an-object"}) == {"data": "not-an-object"}
    assert unwrap("nonsense") == {}
    assert unwrap(None) == {}
