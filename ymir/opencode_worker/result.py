"""Machine-readable result contract for queue-driven OpenCode runs.

The BeeAI agents end with a Pydantic schema that the caller validates. An
OpenCode session ends with prose, which a queue cannot act on, so the worker
requires one fenced JSON block and rejects the run without it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ymir.common.models import Resolution

logger = logging.getLogger(__name__)

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class MissingResultError(ValueError):
    """The agent's answer did not contain a usable result block."""


class TriageResult(BaseModel):
    """The subset of a triage verdict the queue needs to route work."""

    resolution: Resolution = Field(description="Chosen resolution path")
    package: str = Field(min_length=1)
    target_branch: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]
    summary: str = Field(min_length=1)
    justification_category: str | None = None
    evidence: str | None = None
    proposed_comment: str | None = None
    proposed_labels: list[str] = Field(default_factory=list)


def extract_result(text: str) -> TriageResult:
    """Parse the last fenced JSON block in *text* into a ``TriageResult``."""
    blocks = _FENCED_JSON.findall(text or "")
    if not blocks:
        raise MissingResultError("answer contains no fenced JSON result block")
    try:
        return TriageResult.model_validate(json.loads(blocks[-1]))
    except (json.JSONDecodeError, ValidationError) as error:
        raise MissingResultError(f"result block is invalid: {error}") from error


def result_prompt_suffix() -> str:
    """Instructions appended to every queued prompt so the contract is explicit."""
    return (
        "\n\nFinish with exactly one fenced JSON block and nothing after it:\n"
        "```json\n"
        '{"resolution": "rebase|backport|rebuild|not-affected|clarification-needed|'
        'open-ended-analysis|postponed_dependency|postponed_y_stream|postponed_no_patch|'
        'postponed_pr_pending", "package": "<name>", "target_branch": "<branch>", '
        '"confidence": "high|medium|low", "summary": "<one sentence>", '
        '"justification_category": "<category or null>", "evidence": "<what you inspected>", '
        '"proposed_comment": "<comment text or null>", "proposed_labels": ["<label>"]}\n'
        "```\n"
        "The block is consumed by an automated queue, so it must be valid JSON and "
        "must not be wrapped in anything else."
    )
