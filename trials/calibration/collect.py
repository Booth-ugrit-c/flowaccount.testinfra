from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config
from .errors import ApiError
from .ghapi import GhClient

RUN_SEARCH_CAP = 1000
PER_PAGE = 100


@dataclass(frozen=True)
class Observed:
    job: dict
    run_id: int
    run_url: str
    workflow: str


def fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_runs(client: GhClient, cfg: Config, workflow: str) -> list[dict]:
    base = f"repos/{cfg.repo}/actions/workflows/{workflow}/runs"
    runs: dict[int, dict] = {}

    def window(start: datetime, end: datetime) -> str:
        return f"created={fmt(start)}..{fmt(end)}"

    def walk(start: datetime, end: datetime) -> None:
        total = client.get(f"{base}?{window(start, end)}&per_page=1")["total_count"]
        if total == 0:
            return
        if total > RUN_SEARCH_CAP:
            if end - start < timedelta(seconds=2):
                raise ApiError("more runs in one second than the search cap allows")
            middle = start + (end - start) // 2
            walk(start, middle)
            walk(middle + timedelta(seconds=1), end)
            return
        got = [run for page in client.pages(f"{base}?{window(start, end)}&per_page={PER_PAGE}")
               for run in page["workflow_runs"]]
        if len(got) < total:
            raise ApiError(f"run listing incomplete: {len(got)} of {total}")
        runs.update({run["id"]: run for run in got})

    walk(cfg.window_start, cfg.window_end)
    return list(runs.values())


def fetch_jobs(client: GhClient, cfg: Config, run_id: int) -> list[dict]:
    path = f"repos/{cfg.repo}/actions/runs/{run_id}/jobs?per_page={PER_PAGE}&filter=all"
    pages = client.pages(path)
    jobs = [job for page in pages for job in page["jobs"]]
    if pages and len(jobs) < pages[0]["total_count"]:
        raise ApiError(f"job listing incomplete for run {run_id}")
    return jobs


def collect(client: GhClient, cfg: Config) -> tuple[list[Observed], dict]:
    observed: list[Observed] = []
    seen = {"runs": 0, "jobs": 0}
    pool = ThreadPoolExecutor(max_workers=cfg.workers)
    try:
        for workflow in cfg.workflows:
            runs = fetch_runs(client, cfg, workflow)
            for run, jobs in zip(runs, pool.map(lambda r: fetch_jobs(client, cfg, r["id"]), runs)):
                seen["runs"] += 1
                seen["jobs"] += len(jobs)
                observed += [Observed(job, run["id"], run["html_url"], workflow) for job in jobs]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return observed, seen
