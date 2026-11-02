#!/usr/bin/env python3
"""Score the OpenCode triage agent against the mock-Jira e2e fixtures.

The expectations are parsed out of ``ymir/agents/tests/e2e/test_triage.py`` so
there is one source of truth: whatever BeeAI asserts is what we score. The run
goes through the real queue and worker, so it exercises the same prompt and
result contract as production, against a deterministic Jira.

Fixtures come from the testing-jiras repository (``jiras/`` plus
``mock_data/triage/``); start the stack with ``make start-opencode-mock`` so the
gateway serves them.

    python3 files/opencode/eval/run_eval.py --list
    python3 files/opencode/eval/run_eval.py --repeat 2
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
E2E_TEST = REPO / "ymir/agents/tests/e2e/test_triage.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_mock_repos  # noqa: E402  (sibling script, not a package)

DEFAULT_FIXTURES = Path("/tmp/testing-jiras/mock_data/triage")

RESULT_FILENAME = "{issue}-triage.json"
NORMALISE = {
    "BACKPORT": "backport",
    "REBASE": "rebase",
    "NOT_AFFECTED": "not-affected",
    "POSTPONED_DEPENDENCY": "postponed_dependency",
    "REBUILD": "rebuild",
}

COST_SNIPPET = """
import base64, json, os, sys, urllib.parse, urllib.request
sid = sys.argv[1]
h = {"Authorization": "Basic " + base64.b64encode(("opencode:" + os.environ["OPENCODE_PASSWORD"]).encode()).decode()}
q = urllib.parse.urlencode({"location[directory]": "/home/beeai/work"})
msgs = json.load(urllib.request.urlopen(
    urllib.request.Request(f"http://127.0.0.1:4096/api/session/{sid}/message?{q}", headers=h), timeout=60))["data"]
print(sum(float(m.get("cost") or 0) for m in msgs if m.get("type") == "assistant"))
"""


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _const(node, default=None):
    return node.value if isinstance(node, ast.Constant) else default


def load_cases() -> list[dict]:
    """Extract TriageAgentTestCase(...) from the e2e suite."""
    tree = ast.parse(E2E_TEST.read_text())
    cases: list[dict] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "TriageAgentTestCase"
        ):
            continue
        issue = _const(_kwarg(node, "input"))
        expected = _kwarg(node, "expected_output")
        skip = _const(_kwarg(node, "skip_reason"))
        if not issue or not isinstance(expected, ast.Call):
            continue
        resolution = None
        for kw in expected.keywords:
            if kw.arg == "resolution" and isinstance(kw.value, ast.Attribute):
                resolution = kw.value.attr
        data = _kwarg(expected, "data")
        package = fix_version = justification = None
        if isinstance(data, ast.Call):
            package = _const(_kwarg(data, "package"))
            fix_version = _const(_kwarg(data, "fix_version"))
            justification = _const(_kwarg(data, "justification_category"))
        cases.append(
            {
                "issue": issue,
                "expected": NORMALISE.get(resolution or "", (resolution or "").lower()),
                "package": package,
                "fix_version": fix_version,
                "justification_category": justification,
                "skip_reason": skip,
            }
        )
    return cases


class Stack:
    """Thin podman/docker wrapper for the containers the eval needs."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        names = subprocess.run(
            [tool, "ps", "--format", "{{.Names}}"], capture_output=True, text=True, check=True
        ).stdout.split()
        self.gateway = self._pick(names, "mcp-gateway")
        self.agent = self._pick(names, "opencode-agent")
        self.worker = self._pick(names, "opencode-worker")
        self.valkey = self._pick(names, "valkey")

    @staticmethod
    def _pick(names: list[str], needle: str) -> str:
        matches = [n for n in names if needle in n]
        if not matches:
            raise SystemExit(f"no running container matches {needle!r}; start the stack first")
        return matches[0]

    def exec(self, container: str, *args: str, stdin: str | None = None) -> str:
        return subprocess.run(
            [self.tool, "exec", "-i", container, *args],
            capture_output=True,
            text=True,
            input=stdin,
            timeout=300,
        ).stdout

    def env(self, container: str, name: str) -> str | None:
        out = subprocess.run(
            [self.tool, "exec", container, "printenv", name], capture_output=True, text=True
        )
        return out.stdout.strip() or None

    def enqueue(self, payload: dict) -> None:
        self.exec(self.valkey, "redis-cli", "LPUSH", "triage_queue", json.dumps(payload))

    def result(self, path: str) -> dict | None:
        out = subprocess.run(
            [self.tool, "exec", self.worker, "cat", path], capture_output=True, text=True
        )
        if out.returncode != 0:
            return None
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError:
            return None

    def cost(self, session_id: str) -> float:
        try:
            out = self.exec(self.agent, "python3", "-", session_id, stdin=COST_SNIPPET)
            return float(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0.0

    def error_count(self) -> int:
        try:
            return int(self.exec(self.valkey, "redis-cli", "llen", "error_list").strip() or 0)
        except ValueError:
            return 0

    def latest_error(self) -> str:
        raw = self.exec(self.valkey, "redis-cli", "lindex", "error_list", "0")
        try:
            return json.loads(raw)["error"]["details"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return raw.strip()[:300]


def preflight(stack: Stack, fixture: str) -> list[str]:
    problems = []
    for name, container in (("gateway", stack.gateway), ("agent", stack.agent), ("worker", stack.worker)):
        if stack.env(container, "DRY_RUN") != "true":
            problems.append(f"{name}: DRY_RUN is not true")
    if stack.env(stack.gateway, "MOCK_JIRA") != "true":
        problems.append(
            "gateway: MOCK_JIRA is not true — restart with "
            "`make start-opencode-mock` (DRY_RUN=true MOCK_JIRA=true)"
        )
    probe = stack.exec(stack.gateway, "test", "-f", fixture)
    if probe.strip():
        problems.append(f"gateway: fixture {fixture} is not mounted where JIRA_MOCK_FILES points")
    return problems


def run_one(stack: Stack, case: dict, result_dir: str, timeout: int) -> dict:
    issue = case["issue"]
    path = f"{result_dir}/{RESULT_FILENAME.format(issue=issue)}"
    stack.exec(stack.worker, "rm", "-f", path)
    errors_before = stack.error_count()
    started = time.time()
    stack.enqueue({"metadata": {"issue": issue}})
    while time.time() - started < timeout:
        payload = stack.result(path)
        if payload:
            elapsed = time.time() - started
            actual = str(payload.get("resolution") or "")
            return {
                **case,
                "actual": actual,
                "match": actual == case["expected"],
                "target_branch": payload.get("target_branch"),
                "package_actual": payload.get("package"),
                "confidence": payload.get("confidence"),
                "summary": payload.get("summary"),
                "seconds": round(elapsed, 1),
                "cost": round(stack.cost(payload.get("session_id", "")), 4),
                "session_id": payload.get("session_id"),
            }
        # The worker records failures in error_list and moves on; without this the
        # runner would sit in its timeout for an issue that is already dead.
        if stack.error_count() > errors_before:
            return {
                **case,
                "actual": "WORKER_ERROR",
                "match": False,
                "error": stack.latest_error(),
                "seconds": round(time.time() - started, 1),
                "cost": 0.0,
            }
        time.sleep(5)
    return {
        **case,
        "actual": "TIMEOUT",
        "match": False,
        "seconds": round(time.time() - started, 1),
        "cost": 0.0,
    }


def print_scorecard(rows: list[dict]) -> None:
    print(f"\n{'issue':12s} {'expected':20s} {'actual':20s} {'ok':3s} {'target':12s} {'s':>5s} {'$':>6s}")
    for r in rows:
        print(
            f"{r['issue']:12s} {r['expected']:20s} {r['actual']:20s} "
            f"{'yes' if r['match'] else 'NO':3s} {str(r.get('target_branch') or '-'):12s} "
            f"{r['seconds']:5.0f} {r['cost']:6.3f}"
        )
    total = len(rows)
    matched = sum(1 for r in rows if r["match"])
    cost = sum(r["cost"] for r in rows)
    print(f"\nexact resolution: {matched}/{total} ({100 * matched / total:.0f}%)")
    print(f"cost: ${cost:.2f}   wall clock: {max((r['seconds'] for r in rows), default=0):.0f}s slowest")
    wrong = [r for r in rows if not r["match"]]
    if wrong:
        print("\nmismatches:")
        for r in wrong:
            print(f"  {r['issue']}: expected {r['expected']}, got {r['actual']} — {str(r.get('summary'))[:110]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the scored cases and exit")
    parser.add_argument("--include-skipped", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    parser.add_argument("--only", default="", help="comma-separated issue keys to run")
    parser.add_argument("--repeat", type=int, default=1, help="run the whole set N times for variance")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per issue")
    parser.add_argument("--container-tool", default="podman")
    parser.add_argument("--results-dir", default="/home/beeai/work/opencode-worker-results")
    parser.add_argument("--fixture-probe", default="/home/mcp/ymir/tools/privileged/tests/data/RHEL-112546")
    parser.add_argument(
        "--pre-fix",
        action="store_true",
        help="pin dist-git clones to each fixture's pre-fix commit (needs mock_data/triage)",
    )
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    parser.add_argument("--out", default=None, help="where to write the JSON report")
    args = parser.parse_args(argv)

    cases = load_cases()
    if not args.include_skipped:
        cases = [c for c in cases if not c["skip_reason"]]
    if args.only:
        wanted = {k.strip() for k in args.only.split(",") if k.strip()}
        cases = [c for c in cases if c["issue"] in wanted]
    if args.limit:
        cases = cases[: args.limit]

    if args.list:
        for c in cases:
            print(f"{c['issue']:12s} {c['expected']:20s} {str(c['package']):18s} {c.get('fix_version') or '-'}")
        print(f"\n{len(cases)} scored cases")
        return 0

    stack = Stack(args.container_tool)
    problems = preflight(stack, args.fixture_probe)
    if problems:
        print("preflight failed:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    manifest: dict[str, list[dict]] = {}
    if args.pre_fix:
        print(f"preparing pre-fix repositories from {args.fixtures}", flush=True)
        manifest = prepare_mock_repos.setup(
            [c["issue"] for c in cases], fixtures_dir=Path(args.fixtures), tool=args.container_tool
        )
        print(f"  {len(manifest)} issues pinned", flush=True)
    else:
        print(
            "WARNING: --pre-fix is off, so clones use today's branches. "
            "Backport cases will mostly come back not-affected because the fix is already merged.",
            file=sys.stderr,
        )

    rows: list[dict] = []
    for run in range(1, args.repeat + 1):
        if args.repeat > 1:
            print(f"\n=== run {run}/{args.repeat} ===")
        for case in cases:
            if manifest:
                prepare_mock_repos.activate(case["issue"], manifest, tool=args.container_tool)
                prepare_mock_repos.write_provenance(case["issue"], manifest, tool=args.container_tool)
            row = run_one(stack, case, args.results_dir, args.timeout)
            row["run"] = run
            rows.append(row)
            print(
                f"[{run}] {row['issue']} {'ok' if row['match'] else 'MISMATCH'} "
                f"expected={row['expected']} actual={row['actual']} ${row['cost']:.3f}",
                flush=True,
            )

    print_scorecard(rows)

    if args.repeat > 1:
        print("\nper-issue stability:")
        for case in cases:
            mine = [r["actual"] for r in rows if r["issue"] == case["issue"]]
            stable = len(set(mine)) == 1
            print(f"  {case['issue']:12s} {'stable' if stable else 'FLIPPED':7s} {mine}")

    out = Path(args.out) if args.out else Path(
        f"eval-results/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cases": rows, "repeat": args.repeat}, indent=2))
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
