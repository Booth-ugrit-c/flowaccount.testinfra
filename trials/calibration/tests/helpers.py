from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from trials.calibration.ghapi import GhClient, GhError

REPO = "acme/widgets"
BASE = datetime(2026, 1, 10, tzinfo=timezone.utc)


def stamp(offset_s: float) -> str:
    return (BASE + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def config_dict(**over) -> dict:
    cfg = {
        "repo": REPO,
        "workflows": ["pipeline.yml", "nightly.yml"],
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
        "flip": "2026-01-20T00:00:00Z",
        "match_field": "labels",
        "runners": ["lab-runner-a", "lab-runner-b"],
        "manual_runners": ["lab-runner-m"],
        "lanes": [
            {"name": "unit", "job_pattern": "^unit", "default_services": []},
            {"name": "integration", "job_pattern": "^integration", "default_services": ["sql", "cache"]},
        ],
        "service_patterns": [{"pattern": "document-store", "services": ["document"]}],
    }
    cfg.update(over)
    return cfg


_job_ids = iter(range(1, 10**6))


def make_job(name, label, created, start=None, end=None, runner="same", conclusion="success", attempt=1):
    if runner == "same":
        runner = label
    return {
        "id": next(_job_ids), "name": name, "labels": [label], "runner_name": runner,
        "created_at": stamp(created), "started_at": None if start is None else stamp(start),
        "completed_at": None if end is None else stamp(end), "conclusion": conclusion,
        "run_attempt": attempt, "html_url": f"https://github.com/{REPO}/actions/runs/0/job/0",
    }


def make_run(run_id, created, workflow="pipeline.yml"):
    return {
        "id": run_id, "created_at": stamp(created), "run_attempt": 1,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}", "workflow": workflow,
    }


class FakeGh:
    def __init__(self, runs: list[dict], jobs: dict[int, list[dict]], cap: int = 1000):
        self.runs = copy.deepcopy(runs)
        self.jobs = copy.deepcopy(jobs)
        self.cap = cap
        self.log: list[list[str]] = []
        self.failures: list[GhError] = []
        self.reset_epoch = 0
        self.always_fail: GhError | None = None

    def __call__(self, args: list[str]) -> str:
        self.log.append(list(args))
        if self.always_fail:
            raise self.always_fail
        if self.failures and args != ["api", "rate_limit"]:
            raise self.failures.pop(0)
        if args == ["api", "rate_limit"]:
            return json.dumps({"resources": {"core": {"remaining": 0, "reset": self.reset_epoch}}})
        assert args[0] == "api" and not {"-X", "--method", "-f", "-F", "--input"} & set(args)
        paginate = "--paginate" in args
        pages = self.respond(args[-1])
        return json.dumps(pages if paginate else pages[0])

    def respond(self, path: str) -> list[dict]:
        parts = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        per_page = int(query.get("per_page", 30))
        segments = parts.path.split("/")
        if segments[-1] == "runs" and "workflows" in segments:
            workflow = segments[segments.index("workflows") + 1]
            lo, hi = query["created"].split("..")
            matched = sorted(
                (r for r in self.runs if r["workflow"] == workflow and lo <= r["created_at"] <= hi),
                key=lambda r: r["created_at"], reverse=True,
            )
            return paged(matched[: self.cap], per_page, "workflow_runs", len(matched))
        if segments[-1] == "jobs":
            run_id = int(segments[-2])
            assert query["filter"] == "all"
            items = self.jobs.get(run_id, [])
            return paged(items, per_page, "jobs", len(items))
        raise AssertionError(f"unexpected path {path}")


def paged(items: list[dict], per_page: int, key: str, total: int) -> list[dict]:
    chunks = [items[i:i + per_page] for i in range(0, len(items), per_page)] or [[]]
    return [{"total_count": total, key: chunk} for chunk in chunks]


def client_for(fake: FakeGh, retries: int = 2) -> tuple[GhClient, list[float]]:
    slept: list[float] = []
    client = GhClient(runner=fake, sleep=slept.append, now=lambda: 0.0, retries=retries, backoff_s=1.0)
    return client, slept


def standard_world(gate_count: int = 3) -> tuple[list[dict], dict[int, list[dict]]]:
    runs, jobs = [], {}
    for n in range(gate_count):
        at = n * 600
        run_id = 100 + n
        runs.append(make_run(run_id, at))
        jobs[run_id] = [
            make_job("build / compile", "cloud-x", at, at + 5, at + 60, runner="cloud-x-1"),
            make_job("unit / tests", "lab-runner-a", at + 60, at + 70, at + 170),
            make_job("integration / tests document-store", "lab-runner-b", at + 60, at + 90, at + 400),
        ]
    return runs, jobs
