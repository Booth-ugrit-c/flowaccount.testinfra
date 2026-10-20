from dataclasses import dataclass, field


@dataclass(frozen=True)
class Job:
    id: str
    lane: str
    split: str
    created: float
    wait: float
    duration: float

    @property
    def completed(self) -> float:
        return self.created + self.wait + self.duration


@dataclass
class Window:
    start: float
    length: float
    jobs: list[Job] = field(default_factory=list)

    @property
    def busy(self) -> float:
        return sum(j.duration for j in self.jobs)

    def counts(self) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for j in self.jobs:
            out[(j.lane, j.split)] = out.get((j.lane, j.split), 0) + 1
        return out


def jobs_of(calibration: dict) -> list[Job]:
    jobs = []
    for gate in calibration["gates"]:
        for job in gate["jobs"]:
            if job.get("duration_s") is None or job.get("split") in (None, "excluded"):
                continue
            jobs.append(
                Job(
                    id=job["id"],
                    lane=job["lane"],
                    split=job["split"],
                    created=gate["arrival_offset_s"] + job.get("created_offset_s", 0.0),
                    wait=job.get("queue_wait_s") or 0.0,
                    duration=job["duration_s"],
                )
            )
    return sorted(jobs, key=lambda j: (j.created, j.id))


def idle_starts(jobs: list[Job]) -> list[float]:
    starts, horizon = [], float("-inf")
    for job in jobs:
        if job.created >= horizon:
            starts.append(job.created)
        horizon = max(horizon, job.completed)
    return starts


def candidates(jobs: list[Job], length: float) -> list[Window]:
    out = []
    for start in idle_starts(jobs):
        inside = [j for j in jobs if start <= j.created < start + length]
        if inside:
            out.append(Window(start, length, inside))
    return out


def choose(jobs: list[Job], length: float, target: int, max_windows: int) -> list[Window]:
    buckets = {(j.lane, j.split) for j in jobs}
    have = {b: 0 for b in buckets}
    chosen: list[Window] = []
    for window in sorted(candidates(jobs, length), key=lambda w: (-w.busy, w.start)):
        if len(chosen) >= max_windows or all(n >= target for n in have.values()):
            break
        if any(window.start < c.start + c.length and c.start < window.start + window.length for c in chosen):
            continue
        gain = window.counts()
        if not any(have[b] < target and gain.get(b, 0) for b in buckets):
            continue
        chosen.append(window)
        for b, n in gain.items():
            have[b] += n
    return sorted(chosen, key=lambda w: w.start)


def window_calibration(calibration: dict, window: Window, name: str) -> dict:
    keep = {j.id for j in window.jobs}
    gates = []
    for gate in calibration["gates"]:
        jobs = [j for j in gate["jobs"] if j["id"] in keep]
        if not jobs:
            continue
        gates.append(
            {
                "gate": gate["gate"],
                "arrival_offset_s": round(gate["arrival_offset_s"] - window.start, 3),
                "split": gate["split"],
                "jobs": jobs,
            }
        )
    return {"scenario": calibration.get("scenario", "today"), "window": name, "length_s": window.length, "gates": gates}
