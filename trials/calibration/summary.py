from __future__ import annotations

import statistics

def quartiles(values: list[float]) -> tuple[float, float, float] | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, median, q3


def describe(values: list[float]) -> str:
    q = quartiles(values)
    if q is None:
        return "n=0"
    return f"median {q[1]:.1f}s IQR {q[0]:.1f}-{q[2]:.1f}s n={len(values)}"


def render(
    today: dict, lanes: list[str], warnings: list[str], off_runner_jobs: int | None = None, api: dict | None = None
) -> str:
    gates = today["gates"]
    lines = []
    for lane in lanes:
        jobs = [(g, j) for g in gates for j in g["jobs"] if j["lane"] == lane]
        member_gates = [g for g in gates if lane in g["splits"]]
        if not jobs:
            lines.append(f"lane {lane}: no jobs")
            continue
        lines.append(
            f"lane {lane}: jobs {len(jobs)}, gates {len(member_gates)}, "
            f"range {member_gates[0]['created_at']} .. {member_gates[-1]['created_at']}"
        )
        for split in ("fit", "holdout", "excluded"):
            part = [(g, j) for g, j in jobs if j["split"] == split]
            lines.append(
                f"  {split}: gates {sum(1 for g in member_gates if g['splits'][lane] == split)}, jobs {len(part)}"
            )
            lines.append(f"    duration_s   {describe([j['duration_s'] for _, j in part if j['duration_s'] is not None])}")
            lines.append(f"    queue_wait_s {describe([j['queue_wait_s'] for _, j in part if j['queue_wait_s'] is not None])}")
        lines.append(f"  skipped or unfinished (null duration): {sum(1 for _, j in jobs if j['duration_s'] is None)}")
        lines.append(f"  jobs after flip: {sum(1 for _, j in jobs if j['after_flip'])}")
        sources = sorted({j["services_source"] for _, j in jobs})
        lines.append(f"  services source: {', '.join(sources)}")
    lines.append(f"manual-label jobs (not a lane): {today['manual_label_jobs']}")
    if off_runner_jobs is not None:
        lines.append(f"matched by label but ran on another runner: {off_runner_jobs}")
    lines.extend(warnings)
    if api:
        lines.append(
            f"scanned runs {api['runs']}, jobs {api['jobs']}; gh api calls {api['calls']}, "
            f"rate-limit probes {api['rate_probes']}, rate-limit waits {api['rate_waits']}"
        )
    return "\n".join(lines)
