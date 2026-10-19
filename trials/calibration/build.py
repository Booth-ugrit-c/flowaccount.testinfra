from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .collect import Observed
from .config import SERVICE_TYPES, Config
from .errors import DataError

FIT_NUMERATOR, FIT_DENOMINATOR = 7, 10
HOLDOUT_MIN_GATES = 20


@dataclass
class Calibration:
    today: dict
    id_map: dict
    manual_jobs: int
    off_runner_jobs: int
    warnings: list[str] = field(default_factory=list)


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds(later: datetime, earlier: datetime) -> float:
    return round((later - earlier).total_seconds(), 3)


def requested(job: dict, match_field: str) -> set[str]:
    if match_field == "labels":
        return set(job.get("labels") or [])
    return {job["runner_name"]} if job.get("runner_name") else set()


def lane_for(name: str, cfg: Config) -> str:
    for lane in cfg.lanes:
        if lane.pattern.search(name):
            return lane.name
    raise DataError(f"on-prem job name matches no lane pattern: {name!r}")


def services_for(name: str, lane: str, cfg: Config) -> tuple[list[str], str]:
    mapped = {s for pattern, services in cfg.service_patterns if pattern.search(name) for s in services}
    if mapped:
        return [s for s in SERVICE_TYPES if s in mapped], "mapped"
    default = next(item.default_services for item in cfg.lanes if item.name == lane)
    return [s for s in SERVICE_TYPES if s in default], "lane-default"


def timings(job: dict) -> tuple[float | None, float | None]:
    started, completed, created = parse(job.get("started_at")), parse(job.get("completed_at")), parse(job["created_at"])
    ran = job.get("runner_name") and started and completed and job.get("conclusion") != "skipped"
    if not ran:
        return None, None
    return seconds(completed, started), seconds(started, created)


def build(observed: list[Observed], cfg: Config) -> Calibration:
    manual = off_runner = 0
    lane_jobs = []
    for item in observed:
        hit = requested(item.job, cfg.match_field)
        if hit & cfg.manual_runners:
            manual += 1
            continue
        if not hit & cfg.runners:
            continue
        runner_name = item.job.get("runner_name")
        if runner_name and runner_name not in cfg.runners:
            off_runner += 1
        lane_jobs.append((item, lane_for(item.job["name"], cfg), sorted(hit & cfg.runners)[0]))

    ids: dict[int, str] = {}
    per_lane: dict[str, int] = {}
    for item, lane, _ in sorted(lane_jobs, key=lambda t: (t[0].job["created_at"], t[0].run_id, t[0].job["id"])):
        per_lane[lane] = per_lane.get(lane, 0) + 1
        ids[item.job["id"]] = f"{lane}-{per_lane[lane]:03d}"

    grouped: dict[tuple[int, int], list] = {}
    for entry in lane_jobs:
        grouped.setdefault((entry[0].run_id, entry[0].job["run_attempt"]), []).append(entry)
    ordered = sorted(
        grouped.items(),
        key=lambda kv: (min(e[0].job["created_at"] for e in kv[1]), kv[0]),
    )
    origin = parse(min(e[0].job["created_at"] for e in ordered[0][1])) if ordered else None

    gates, id_gates, id_jobs = [], {}, {}
    for number, ((run_id, attempt), entries) in enumerate(ordered, start=1):
        gate_id = f"g-{number:04d}"
        created = min(parse(e[0].job["created_at"]) for e in entries)
        jobs = []
        for item, lane, runner in sorted(entries, key=lambda e: (e[0].job["created_at"], ids[e[0].job["id"]])):
            duration, wait = timings(item.job)
            services, source = services_for(item.job["name"], lane, cfg)
            job_created = parse(item.job["created_at"])
            jobs.append({
                "id": ids[item.job["id"]], "lane": lane, "runner": runner,
                "duration_s": duration, "queue_wait_s": wait,
                "created_offset_s": seconds(job_created, created),
                "run_attempt": attempt, "conclusion": item.job.get("conclusion"),
                "services": services, "services_source": source,
                "after_flip": job_created > cfg.flip,
            })
            id_jobs[ids[item.job["id"]]] = {"name": item.job["name"], "url": item.job.get("html_url"), "run_id": run_id}
        gates.append({
            "gate": gate_id, "created_at": iso(created), "arrival_offset_s": seconds(created, origin),
            "after_flip": created > cfg.flip, "jobs": jobs,
        })
        id_gates[gate_id] = {"run_id": run_id, "run_attempt": attempt, "run_url": entries[0][0].run_url,
                             "workflow": entries[0][0].workflow}

    warnings = assign_splits(gates, [lane.name for lane in cfg.lanes])
    today = {
        "split_rule": {"fit": f"{FIT_NUMERATOR}/{FIT_DENOMINATOR}", "order": "gate created_at, per lane"},
        "window": {"start": iso(cfg.window_start), "end": iso(cfg.window_end)},
        "flip": iso(cfg.flip),
        "match_field": cfg.match_field,
        "manual_label_jobs": manual,
        "gates": gates,
    }
    return Calibration(today, {"gates": id_gates, "jobs": id_jobs}, manual, off_runner, warnings)


def assign_splits(gates: list[dict], lanes: list[str]) -> list[str]:
    warnings = []
    for lane in lanes:
        members = [g for g in gates if any(j["lane"] == lane for j in g["jobs"])]
        fit_count = len(members) * FIT_NUMERATOR // FIT_DENOMINATOR
        for index, gate in enumerate(members):
            gate.setdefault("splits", {})[lane] = "fit" if index < fit_count else "holdout"
        holdout = len(members) - fit_count
        if not members:
            warnings.append(f"WARNING: lane {lane} has no gates")
        elif holdout < HOLDOUT_MIN_GATES:
            warnings.append(f"WARNING: lane {lane} holdout has {holdout} gates (fewer than {HOLDOUT_MIN_GATES})")
    for gate in gates:
        values = set(gate["splits"].values())
        gate["split"] = values.pop() if len(values) == 1 else "mixed"
        for job in gate["jobs"]:
            job["split"] = gate["splits"][job["lane"]]
    return warnings
