import json
import statistics
from datetime import datetime
from pathlib import Path


def quartiles(values: list[float]) -> tuple[float, float, float]:
    if len(values) < 2:
        raise ValueError("need at least two values")
    q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, q2, q3


def _ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def real_jobs(window_files: list[Path]) -> dict[str, dict]:
    out = {}
    for path in window_files:
        for gate in json.loads(path.read_text(encoding="utf-8"))["gates"]:
            for job in gate["jobs"]:
                if job.get("duration_s") is None:
                    continue
                wait = job.get("queue_wait_s") or 0.0
                out[job["id"]] = {
                    "lane": job["lane"],
                    "split": job["split"],
                    "wait": wait,
                    "duration": job["duration_s"],
                    "wall": wait + job["duration_s"],
                }
    return out


def replay_jobs(collect_dirs: list[Path], compress: float) -> dict[str, dict]:
    out = {}
    for directory in collect_dirs:
        for path in sorted((directory / "gates").glob("*.json")):
            for job in json.loads(path.read_text(encoding="utf-8"))["jobs"]:
                if job.get("conclusion") != "success" or not job.get("started_at"):
                    raise ValueError(f"{path.name}: job {job.get('id')} did not succeed")
                created, started, completed = (_ts(job[k]) for k in ("created_at", "started_at", "completed_at"))
                out[job["id"]] = {
                    "wait": (started - created) * compress,
                    "duration": (completed - started) * compress,
                    "wall": (completed - created) * compress,
                }
    return out


def paired(real: dict[str, dict], replay: dict[str, dict]) -> list[tuple[dict, dict]]:
    missing = sorted(set(real) - set(replay))
    if missing:
        raise ValueError(f"{len(missing)} real jobs have no replay record, first: {missing[:3]}")
    return [(real[k], replay[k]) for k in sorted(real)]


def fit_factors(pairs: list[tuple[dict, dict]], factors: dict[str, float]) -> dict[str, float]:
    out = {}
    for lane, factor in factors.items():
        rows = [(r, p) for r, p in pairs if r["lane"] == lane and r["split"] == "fit"]
        if len(rows) < 2:
            raise ValueError(f"lane {lane}: fewer than two fit jobs")
        real = statistics.median(r["duration"] for r, _ in rows)
        replay = statistics.median(p["duration"] for _, p in rows)
        if not replay:
            raise ValueError(f"lane {lane}: replay median duration is 0")
        out[lane] = factor * real / replay
    return out


def verdict(pairs: list[tuple[dict, dict]], split: str = "holdout") -> list[dict]:
    rows = []
    lanes = sorted({r["lane"] for r, _ in pairs})
    for lane in lanes:
        sel = [(r, p) for r, p in pairs if r["lane"] == lane and r["split"] == split]
        if len(sel) < 2:
            raise ValueError(f"lane {lane}: fewer than two {split} jobs")
        for metric in ("wall", "wait"):
            q1, med, q3 = quartiles([r[metric] for r, _ in sel])
            replay_median = statistics.median(p[metric] for _, p in sel)
            rows.append(
                {
                    "lane": lane,
                    "metric": metric,
                    "n": len(sel),
                    "real_q1": q1,
                    "real_median": med,
                    "real_q3": q3,
                    "replay_median": replay_median,
                    "pass": q1 <= replay_median <= q3,
                }
            )
    return rows


def noise_floor(per_run: list[list[tuple[dict, dict]]]) -> list[dict]:
    if len(per_run) < 2:
        raise ValueError("need at least two runs")
    lanes = sorted({r["lane"] for pairs in per_run for r, _ in pairs})
    rows = []
    for lane in lanes:
        for metric in ("wall", "wait"):
            medians = []
            for pairs in per_run:
                values = [p[metric] for r, p in pairs if r["lane"] == lane]
                if not values:
                    raise ValueError(f"lane {lane}: a run has no jobs")
                medians.append(statistics.median(values))
            mean = statistics.fmean(medians)
            cv = statistics.stdev(medians) / mean if mean else 0.0
            rows.append({"lane": lane, "metric": metric, "runs": len(medians), "mean_median": mean, "cv": cv})
    return rows
