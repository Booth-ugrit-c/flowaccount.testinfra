import csv
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from trials.validate.stats import replay_jobs

Z_TWO_SIDED = 2.8
METRICS = ("wall", "wait")


def kinds_of(window_file: Path) -> dict[str, dict]:
    out = {}
    for gate in json.loads(Path(window_file).read_text(encoding="utf-8"))["gates"]:
        for job in gate["jobs"]:
            if job.get("duration_s") is not None:
                out[job["id"]] = {"lane": job["lane"], "kind": gate.get("kind", "pr")}
    return out


def p90(values: list[float]) -> float:
    return statistics.quantiles(values, n=10, method="inclusive")[-1] if len(values) > 1 else values[0]


def run_metrics(jobs: dict[str, dict], replay: dict[str, dict]) -> dict[str, float]:
    missing = sorted(set(jobs) - set(replay))
    if missing:
        raise ValueError(f"{len(missing)} window jobs have no replay record, first: {missing[:3]}")
    out = {}
    groups = {"all": list(jobs)}
    for job_id, meta in jobs.items():
        groups.setdefault(meta["lane"], []).append(job_id)
        groups.setdefault(f"{meta['kind']}", []).append(job_id)
    for group, ids in sorted(groups.items()):
        for metric in METRICS:
            values = [replay[i][metric] for i in ids]
            out[f"{group}/{metric}/median"] = statistics.median(values)
            out[f"{group}/{metric}/p90"] = p90(values)
    return out


def _ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def span(collect_dir: Path) -> tuple[float, float]:
    stamps = [
        (_ts(job["created_at"]), _ts(job["completed_at"]))
        for path in sorted((Path(collect_dir) / "gates").glob("*.json"))
        for job in json.loads(path.read_text(encoding="utf-8"))["jobs"]
        if job.get("completed_at")
    ]
    if not stamps:
        raise ValueError(f"{collect_dir}: no completed jobs")
    return min(s for s, _ in stamps), max(e for _, e in stamps)


def usage(sample_files: list[Path], start: float, end: float) -> dict[str, float]:
    busy, peak = [], 0.0
    for path in sample_files:
        rows = []
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                epoch = float(row["epoch"])
                if start <= epoch <= end:
                    rows.append(row)
        if not rows:
            raise ValueError(f"{path.name}: no samples between {start:.0f} and {end:.0f}")
        busy.append(statistics.fmean(float(r["cpu_busy"]) for r in rows))
        peak += max(float(r["mem_used_mib"]) for r in rows)
    return {"cpu_busy_mean": statistics.fmean(busy), "mem_peak_mib": peak}


def summarize(per_design: dict[str, list[dict[str, float]]], baseline: str) -> list[dict]:
    if baseline not in per_design:
        raise ValueError(f"baseline {baseline!r} has no runs")
    rows = []
    keys = sorted(set.intersection(*(set(r) for runs in per_design.values() for r in runs)))
    for key in keys:
        base = [r[key] for r in per_design[baseline]]
        for design, runs in per_design.items():
            values = [r[key] for r in runs]
            mean = statistics.fmean(values)
            sd = statistics.stdev(values) if len(values) > 1 else math.nan
            row = {"key": key, "design": design, "n": len(values), "mean": mean, "sd": sd, "cv": sd / mean if mean else math.nan}
            if design != baseline:
                base_mean = statistics.fmean(base)
                base_sd = statistics.stdev(base) if len(base) > 1 else math.nan
                mde = Z_TWO_SIDED * math.sqrt(sd ** 2 / len(values) + base_sd ** 2 / len(base))
                row.update({
                    "delta": mean - base_mean,
                    "delta_pct": (mean - base_mean) / base_mean if base_mean else math.nan,
                    "mde": mde,
                    "detectable": abs(mean - base_mean) > mde,
                })
            rows.append(row)
    return rows
