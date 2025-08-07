import os
import json
from enum import Enum
from typing import Annotated, Any
from urllib.parse import urljoin

import requests
from pydantic import Field


class Severity(Enum):
    NONE = "None"
    INFORMATIONAL = "Informational"
    LOW = "Low"
    MODERATE = "Moderate"
    IMPORTANT = "Important"
    CRITICAL = "Critical"


class PreliminaryTesting(Enum):
    NONE = "None"
    PASS = "Pass"
    FAIL = "Fail"
    REQUESTED = "Requested"


def _get_jira_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_jira_details(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> dict[str, Any] | str:
    """
    Gets details about the specified Jira issue, including all comments and remote links.
    Returns a dictionary with issue details, comments, and remote links or an error message on failure.
    """
    try:
        headers = _get_jira_headers(os.getenv("JIRA_TOKEN"))
        response = requests.get(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
            params={"expand": "comments"},
            headers=headers,
        )
        response.raise_for_status()
        issue_data = response.json()

        # Remote links in Jira have a specific endpoint, they are important as they often contains links to PRs or mailing lists.
        # https://community.atlassian.com/forums/Jira-questions/Issue-with-Design-Links-Not-Appearing-in-Remote-Links-API/qaq-p/2698765
        try:
            remote_response = requests.get(
                urljoin(
                    os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/remotelink"
                ),
                headers=headers,
            )
            remote_response.raise_for_status()
            remote_links = remote_response.json()

            # Add remote links to the issue data
            issue_data["remote_links"] = remote_links
        except requests.RequestException as e:
            # If remote links fail, continue without them
            issue_data["remote_links"] = []

    except requests.RequestException as e:
        return f"Failed to get details about the specified issue: {e}"
    return issue_data


def set_jira_fields(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    fix_versions: Annotated[
        list[str] | None,
        Field(
            description="List of Fix Version/s values",
            pattern=r"^(CentOS Stream \d+|eln|zstream|rhel-\d+\.\d+(\.(\d+|z)|\.\d+\.z|[._](alpha|beta))?)$",
        ),
    ] = None,
    severity: Annotated[Severity | None, Field(description="Severity value")] = None,
    preliminary_testing: Annotated[
        PreliminaryTesting | None, Field(description="Preliminary Testing value")
    ] = None,
) -> str | None:
    """
    Updates the specified Jira issue, setting the specified fields (if provided).
    Returns error message on failure.
    """
    fields = {}
    if fix_versions is not None:
        fields["fixVersions"] = [{"name": fv} for fv in fix_versions]
    if severity is not None:
        fields["customfield_12316142"] = {"value": severity.value}
    if preliminary_testing is not None:
        fields["customfield_12321540"] = {"value": preliminary_testing.value}
    if not fields:
        return
    try:
        response = requests.put(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}"),
            json={"fields": fields},
            headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
        )
        response.raise_for_status()
    except requests.RequestException as e:
        return f"Failed to set the specified fields: {e}"


def add_jira_comment(
    issue_key: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
    comment: Annotated[str, Field(description="Comment text to add")],
) -> str | None:
    """
    Adds a comment to the specified Jira issue. Returns error message on failure.
    """
    try:
        response = requests.post(
            urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue/{issue_key}/comment"),
            json={"body": comment},
            headers=_get_jira_headers(os.getenv("JIRA_TOKEN")),
        )
        response.raise_for_status()
    except requests.RequestException as e:
        return f"Failed to add the specified comment: {e}"
