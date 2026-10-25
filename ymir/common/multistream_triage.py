"""Task contract shared by the multi-stream fetcher and worker."""

import re
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from ymir.common.cve import extract_cve_ids

QUEUE = "multistream_triage_queue"
PROCESSING = "multistream_triage_processing"
RESULTS = "multistream_triage_results"


class MultiStreamTriageTask(BaseModel):
    kind: Literal["cve_triage"] = "cve_triage"
    cve: str
    package: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
    issues: list[str] = Field(min_length=1)
    execution_id: UUID = Field(default_factory=uuid4)

    @field_validator("cve")
    @classmethod
    def validate_cve(cls, value: str) -> str:
        value = value.upper()
        if "," in value or extract_cve_ids(value) != value:
            raise ValueError("A group task must contain exactly one CVE identifier")
        return value

    @field_validator("issues")
    @classmethod
    def validate_issues(cls, values: list[str]) -> list[str]:
        issues = [value.upper() for value in values]
        if len(set(issues)) != len(issues) or any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", issue) for issue in issues
        ):
            raise ValueError("Issue keys must be valid and unique")
        return issues
