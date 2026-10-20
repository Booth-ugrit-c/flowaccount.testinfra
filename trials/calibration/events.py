from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .collect import fetch_runs
from .config import Config
from .errors import DataError
from .ghapi import GhClient


def kind_of(run: dict, default_branch: str) -> str:
    return "schedule" if run["event"] == "schedule" or run.get("head_branch") == default_branch else "pr"


def run_events(client: GhClient, cfg: Config, run_ids: set[int]) -> dict[int, dict]:
    fields = lambda run: {"event": run["event"], "head_branch": run.get("head_branch")}
    runs = {
        run["id"]: fields(run)
        for workflow in cfg.workflows
        for run in fetch_runs(client, cfg, workflow)
        if run["id"] in run_ids
    }
    missing = sorted(run_ids - runs.keys())
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        for run_id, run in zip(missing, pool.map(lambda r: client.get(f"repos/{cfg.repo}/actions/runs/{r}"), missing)):
            runs[run_id] = fields(run)
    return runs


def gate_runs(today: dict, id_map: dict) -> dict[str, int]:
    runs = {}
    for gate in today["gates"]:
        ref = id_map.get("gates", {}).get(gate["gate"])
        if not ref or "run_id" not in ref:
            raise DataError(f"gate {gate['gate']} has no run in the id map")
        runs[gate["gate"]] = ref["run_id"]
    return runs


def annotate(today: dict, runs: dict[str, int], fields: dict[int, dict], default_branch: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for gate in today["gates"]:
        run = fields[runs[gate["gate"]]]
        gate["event"], gate["kind"] = run["event"], kind_of(run, default_branch)
        counts[run["event"]] = counts.get(run["event"], 0) + 1
    return counts
