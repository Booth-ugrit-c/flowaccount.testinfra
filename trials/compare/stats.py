import csv
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

Z_TWO_SIDED = 2.8
METRICS = ("wall", "wait")


def kinds_of(window_file: Path) -> dict[str, dict]:
    out = {}
    for gate in json.loads(Path(window_file).read_text(encoding="utf-8"))["gates"]:
        if "kind" not in gate:
            raise ValueError(f"{Path(window_file).name}: gate {gate.get('gate')} has no kind, run annotate-events first")
        for job in gate["jobs"]:
            if job.get("duration_s") is not None:
                out[job["id"]] = {"lane": job["lane"], "kind": gate["kind"], "gate": gate["gate"]}
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


def gate_metrics(collect_dir: Path, jobs: dict[str, dict], compress: float) -> dict[str, float]:
    by_kind: dict[str, list[float]] = {}
    for path in sorted((Path(collect_dir) / "gates").glob("*.json")):
        members = [j for j in json.loads(path.read_text(encoding="utf-8"))["jobs"] if j["id"] in jobs]
        owners = {jobs[j["id"]]["gate"] for j in members}
        if len(owners) != 1:
            raise ValueError(f"{path.name}: jobs map to {len(owners)} window gates, expected 1")
        wall = (max(_ts(j["completed_at"]) for j in members) - min(_ts(j["created_at"]) for j in members)) * compress
        by_kind.setdefault(jobs[members[0]["id"]]["kind"], []).append(wall)
    by_kind["all"] = [w for walls in list(by_kind.values()) for w in walls]
    out = {}
    for kind, walls in sorted(by_kind.items()):
        out[f"gate/{kind}/wall/median"] = statistics.median(walls)
        out[f"gate/{kind}/wall/p90"] = p90(walls)
    return out


def waiting_set(collect_dir: Path) -> list[tuple[float, float]]:
    spans = sorted(
        (_ts(job["created_at"]), _ts(job["started_at"]))
        for path in (Path(collect_dir) / "gates").glob("*.json")
        for job in json.loads(path.read_text(encoding="utf-8"))["jobs"]
    )
    merged: list[list[float]] = []
    for start, end in spans:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def idle_while_waiting(sample_files: list[Path], waiting: list[tuple[float, float]], start: float, end: float, compress: float) -> dict[str, float]:
    waiting_s = sum(b - a for a, b in waiting) * compress
    if not waiting:
        return {"idle_while_waiting": math.nan, "waiting_s": 0.0}
    idle = []
    for path in sample_files:
        with open(path, newline="", encoding="utf-8") as handle:
            values = [
                1 - float(row["cpu_busy"])
                for row in csv.DictReader(handle)
                if start <= float(row["epoch"]) <= end and any(a <= float(row["epoch"]) < b for a, b in waiting)
            ]
        if not values:
            raise ValueError(f"{path.name}: no samples while work waits")
        idle.append(statistics.fmean(values))
    return {"idle_while_waiting": statistics.fmean(idle), "waiting_s": waiting_s}


def usage(sample_files: list[Path], start: float, end: float, interval: float = 1.0) -> dict[str, float]:
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
        edges = [start, *(float(r["epoch"]) for r in rows), end]
        if max(b - a for a, b in zip(edges, edges[1:])) > 3 * interval:
            raise ValueError(f"{path.name}: samples do not cover {start:.0f}-{end:.0f} within {3 * interval:g} s")
        busy.append(statistics.fmean(float(r["cpu_busy"]) for r in rows))
        peak += max(float(r["mem_used_mib"]) for r in rows)
    return {"cpu_busy_mean": statistics.fmean(busy), "mem_peak_mib": peak}


def _sd(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 and not any(math.isnan(v) for v in values) else math.nan


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
            sd = _sd(values)
            row = {"key": key, "design": design, "n": len(values), "mean": mean, "sd": sd, "cv": sd / mean if mean else math.nan}
            if design != baseline:
                base_mean = statistics.fmean(base)
                base_sd = _sd(base)
                mde = Z_TWO_SIDED * math.sqrt(sd ** 2 / len(values) + base_sd ** 2 / len(base))
                row.update({
                    "delta": mean - base_mean,
                    "delta_pct": (mean - base_mean) / base_mean if base_mean else math.nan,
                    "mde": mde,
                    "detectable": None if math.isnan(mde) else abs(mean - base_mean) > mde,
                })
            rows.append(row)
    return rows
