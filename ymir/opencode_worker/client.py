"""Minimal client for the OpenCode HTTP API used by the queue worker."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Terminal session outcomes. ``None`` while the session is still running.
TERMINAL_OUTCOMES = frozenset({"succeeded", "failed", "interrupted"})


def unwrap(payload: Any) -> dict[str, Any]:
    """Return the object body of an OpenCode response.

    Some endpoints answer with the object, others wrap it as ``{"data": {...}}``.
    Both shapes appear across the API, so accept either rather than guessing per
    endpoint.
    """
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


class OpenCodeError(RuntimeError):
    """The OpenCode server rejected a request or never finished a session."""


@dataclass(frozen=True)
class OpenCodeSettings:
    """Connection and timing for one worker."""

    base_url: str = "http://127.0.0.1:4096"
    directory: str = "/home/beeai/work"
    password: str = ""
    request_timeout: float = 60.0
    poll_interval: float = 2.0
    task_timeout: float = 1800.0

    def __post_init__(self) -> None:
        if not self.password:
            raise ValueError("an OpenCode password is required to drive the server")


class OpenCodeClient:
    """Create or resume a session, submit one prompt, wait, read the answer."""

    def __init__(self, settings: OpenCodeSettings) -> None:
        self._settings = settings
        self._base = settings.base_url.rstrip("/")
        token = base64.b64encode(f"opencode:{settings.password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self._directory = settings.directory
        self._timeout = aiohttp.ClientTimeout(total=settings.request_timeout)

    @property
    def _location(self) -> dict[str, str]:
        return {"directory": self._directory}

    @property
    def _location_query(self) -> dict[str, str]:
        return {"location[directory]": self._directory}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base}{path}"
        try:
            async with aiohttp.ClientSession(headers=self._headers, timeout=self._timeout) as session:
                async with session.request(method, url, **kwargs) as response:
                    if response.status >= 400:
                        detail = (await response.text())[:500]
                        raise OpenCodeError(f"{method} {path} returned {response.status}: {detail}")
                    # Several endpoints answer 204 No Content (agent switch,
                    # prompt admission) and must not be parsed as JSON.
                    raw = await response.text()
                    if not raw.strip():
                        return {}
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as error:
                        raise OpenCodeError(
                            f"{method} {path} returned non-JSON body: {raw[:200]!r}"
                        ) from error
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            # asyncio.TimeoutError is not an aiohttp.ClientError and its str() is
            # empty, so it used to escape as "Unexpected failure: " with no clue
            # what timed out.
            raise OpenCodeError(f"{method} {path} failed: {type(error).__name__}: {error!r}") from error

    async def create_session(self, agent: str, title: str) -> str:
        payload = {"agent": agent, "title": title, "location": self._location}
        data = unwrap(await self._request("POST", "/api/session", json=payload))
        session_id = data.get("id")
        if not session_id:
            raise OpenCodeError(f"session creation returned no id: {str(data)[:300]}")
        logger.info("Created OpenCode session %s for agent %s", session_id, agent)
        return str(session_id)

    async def set_agent(self, session_id: str, agent: str) -> None:
        await self._request("POST", f"/api/session/{session_id}/agent", json={"agent": agent})

    async def prompt(self, session_id: str, text: str) -> None:
        await self._request("POST", f"/api/session/{session_id}/prompt", json={"text": text})

    async def session(self, session_id: str) -> dict[str, Any]:
        data = await self._request(
            "GET", f"/api/session/{session_id}", params=self._location_query
        )
        return unwrap(data)

    async def idle_since(self, session_id: str) -> int:
        """Return the previous ``time.idle`` marker, or 0 when never idle."""
        info = await self.session(session_id)
        return int(info.get("time", {}).get("idle") or 0)

    async def wait_for_outcome(self, session_id: str, *, after_idle: int = 0) -> str:
        """Wait for a completion newer than ``after_idle``.

        A finished session keeps reporting its old ``outcome`` — and its old
        ``time.idle`` — until the next run completes. Waiting on the outcome alone
        would return the previous answer the instant a resumed session is
        prompted again, so completion is defined as "terminal outcome *and* an
        idle marker newer than the one observed before prompting".
        """
        deadline = asyncio.get_running_loop().time() + self._settings.task_timeout
        while True:
            info = await self.session(session_id)
            outcome = info.get("outcome")
            idle = int(info.get("time", {}).get("idle") or 0)
            if outcome in TERMINAL_OUTCOMES and idle > after_idle:
                logger.info("Session %s finished with outcome %s", session_id, outcome)
                return str(outcome)
            if asyncio.get_running_loop().time() >= deadline:
                raise OpenCodeError(
                    f"session {session_id} did not finish within {self._settings.task_timeout:.0f}s"
                )
            await asyncio.sleep(self._settings.poll_interval)

    async def final_text(self, session_id: str) -> str:
        """Return the last assistant text message of a finished session."""
        data = await self._request(
            "GET", f"/api/session/{session_id}/context", params=self._location_query
        )
        messages = data.get("data") if isinstance(data, dict) else []
        for message in reversed(messages or []):
            if message.get("type") != "assistant":
                continue
            text = "".join(
                part.get("text", "") for part in message.get("content", []) if part.get("type") == "text"
            ).strip()
            if text:
                return text
        raise OpenCodeError(f"session {session_id} produced no assistant text")


async def run_prompt(
    client: OpenCodeClient,
    agent: str,
    title: str,
    prompt: str,
    *,
    session_id: str | None = None,
) -> tuple[str, str]:
    """Run one stage, resuming ``session_id`` when it is still usable.

    Returns ``(answer, session_id)`` so the caller can persist the session for a
    later stage. A session that no longer exists falls back to a fresh one; the
    API reports that as an error rather than silently starting over.
    """
    resumed = False
    if session_id:
        try:
            await client.set_agent(session_id, agent)
            resumed = True
        except OpenCodeError as error:
            logger.warning("Cannot resume session %s (%s); starting fresh", session_id, error)
            session_id = None

    if not session_id:
        session_id = await client.create_session(agent, title)

    idle_before = await client.idle_since(session_id)
    await client.prompt(session_id, prompt)
    outcome = await client.wait_for_outcome(session_id, after_idle=idle_before)
    if outcome != "succeeded":
        raise OpenCodeError(f"session {session_id} ended with outcome {outcome}")

    answer = await client.final_text(session_id)
    if resumed:
        logger.info("Resumed session %s for agent %s", session_id, agent)
    return answer, session_id
