"""Preview or enqueue CVE tracker groups selected by a dedicated JQL."""

import argparse
import asyncio
import json
import logging
import os
from collections import defaultdict
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import WatchError

from ymir.common.base_utils import redis_client
from ymir.common.cve import extract_cve_ids
from ymir.common.logging_setup import configure_logging
from ymir.common.multistream_triage import PROCESSING, QUEUE, RESULTS, MultiStreamTriageTask
from ymir.jira_issue_fetcher.jira_issue_fetcher import JiraIssueFetcher

logger = logging.getLogger(__name__)


def group_trackers(issues: list[dict[str, Any]]) -> list[MultiStreamTriageTask]:
    """Group only unambiguous, non-modular trackers; do not discover extra siblings."""
    groups = defaultdict(set)
    for issue in issues:
        fields = issue.get("fields") or {}
        versions = fields.get("fixVersions") or []
        try:
            task = MultiStreamTriageTask(
                cve=extract_cve_ids(fields.get("summary")) or "",
                package=fields.get("customfield_10669"),
                issues=[issue.get("key") or ""],
            )
            if len(versions) != 1 or not versions[0].get("name"):
                raise ValueError("Expected exactly one named fix version")
        except ValueError as error:
            logger.warning("Skipping %s: %s", issue.get("key"), error)
            continue
        groups[task.cve, task.package].add(task.issues[0])
    return [
        MultiStreamTriageTask(cve=cve, package=package, issues=sorted(members))
        for (cve, package), members in sorted(groups.items())
    ]


async def enqueue_groups(redis: Redis, tasks: list[MultiStreamTriageTask]) -> int:
    """Atomically deduplicate against pending groups and recorded tracker attempts."""
    if not tasks:
        return 0
    async with redis.pipeline() as pipe:
        while True:
            try:
                await pipe.watch(QUEUE, PROCESSING, RESULTS)
                pending = [
                    MultiStreamTriageTask.model_validate_json(payload)
                    for key in (QUEUE, PROCESSING)
                    for payload in await pipe.lrange(key, 0, -1)
                ]
                recorded = [
                    MultiStreamTriageTask.model_validate(json.loads(payload)["task"])
                    for payload in await pipe.lrange(RESULTS, 0, -1)
                ]
                busy = {(task.cve, task.package) for task in pending}
                seen = {issue for task in pending + recorded for issue in task.issues}
                ready = []
                for task in tasks:
                    if (task.cve, task.package) in busy:
                        continue
                    members = [issue for issue in task.issues if issue not in seen]
                    if members:
                        ready.append(task.model_copy(update={"issues": members}))
                        busy.add((task.cve, task.package))
                        seen.update(members)
                pipe.multi()
                for task in ready:
                    pipe.lpush(QUEUE, task.model_dump_json())
                await pipe.execute()
                return len(ready)
            except WatchError:
                # A worker or another fetcher changed the lists. Re-read all state.
                continue


class MultiStreamTriageFetcher(JiraIssueFetcher):
    """Reuse Jira pagination/authentication, not the existing enqueue or label logic."""

    def __init__(self):
        query = os.getenv("MULTISTREAM_TRIAGE_QUERY", "").strip()
        if not query:
            raise ValueError("MULTISTREAM_TRIAGE_QUERY must explicitly select the multi-stream trackers")
        super().__init__()
        self.query = query

    async def search_issues(self, fields: list[str] | None = None) -> list[dict[str, Any]]:
        return await super().search_issues(
            fields=fields if fields is not None else ["key", "summary", "customfield_10669", "fixVersions"]
        )

    async def run(self, enqueue: bool = False) -> None:
        tasks = group_trackers(await self.search_issues())
        if not enqueue:
            print(json.dumps([task.model_dump(mode="json") for task in tasks], indent=2))
            return
        async with redis_client(self.redis_url) as redis:
            count = await enqueue_groups(redis, tasks)
        logger.info("Enqueued %d of %d candidate CVE tracker groups", count, len(tasks))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enqueue", action="store_true", help="Write to the multi-stream triage queue; default is preview"
    )
    args = parser.parse_args()
    configure_logging(level=logging.INFO)
    asyncio.run(MultiStreamTriageFetcher().run(enqueue=args.enqueue))


if __name__ == "__main__":
    main()
