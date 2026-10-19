import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from trials.dispatch.clock import Clock
from trials.dispatch.gh import GhClient, GhError
from trials.dispatch.replay import STATE_FILE, write_state

CONTROL_JOBS = ("validate",)
RESULT_RE = re.compile(r"workload-result (\{.*\})")
LOG_ATTEMPTS = 3
LOG_RETRY_S = 5.0


@dataclass
class GateProblems:
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    non_terminal: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.missing or self.extra or self.non_terminal or self.duplicates)


def is_terminal(job: dict) -> bool:
    return job.get("status") == "completed" and job.get("conclusion") is not None


def check_gate(expected_ids: list[str], jobs: list[dict]) -> GateProblems:
    names = [j["id"] for j in jobs]
    expected = set(expected_ids)
    return GateProblems(
        missing=sorted(expected - set(names)),
        extra=sorted(set(names) - expected),
        non_terminal=sorted(j["id"] for j in jobs if not is_terminal(j)),
        duplicates=sorted({n for n in names if names.count(n) > 1}),
    )


def parse_workload_result(log_text: str) -> dict | None:
    found = None
    for line in log_text.splitlines():
        match = RESULT_RE.search(line)
        if match:
            try:
                found = json.loads(match.group(1))
            except ValueError:
                continue
    return found


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def fetch_jobs(gh: GhClient, repo: str, run_id: int) -> list[dict]:
    jobs, page = [], 1
    while True:
        payload = gh.api(f"repos/{repo}/actions/runs/{run_id}/jobs", params={"per_page": 100, "page": page})
        batch = (payload or {}).get("jobs", [])
        jobs.extend(batch)
        if len(batch) < 100 or len(jobs) >= (payload or {}).get("total_count", 0):
            return jobs
        page += 1


def build_job_record(gh: GhClient, repo: str, raw: dict, clock: Clock) -> dict:
    record = {
        "id": raw["name"],
        "job_id": raw["id"],
        "status": raw.get("status"),
        "conclusion": raw.get("conclusion"),
        "created_at": raw.get("created_at"),
        "started_at": raw.get("started_at"),
        "completed_at": raw.get("completed_at"),
        "runner_name": raw.get("runner_name"),
        "workload_result": None,
        "log_error": None,
    }
    if raw["name"] in CONTROL_JOBS or not raw.get("started_at"):
        return record
    for attempt in range(LOG_ATTEMPTS):
        try:
            record["workload_result"] = parse_workload_result(gh.job_log(repo, raw["id"]))
            record["log_error"] = None
            break
        except GhError as exc:
            record["log_error"] = str(exc)
            if attempt + 1 < LOG_ATTEMPTS:
                clock.sleep(LOG_RETRY_S)
    return record


def collect(out_dir: Path, gh: GhClient, clock: Clock, poll_s: float = 15.0, timeout_s: float = 21600.0) -> tuple[bool, list[str]]:
    out_dir = Path(out_dir)
    state = json.loads((out_dir / STATE_FILE).read_text(encoding="utf-8"))
    repo = state["repo"]
    (out_dir / "gates").mkdir(exist_ok=True)
    summary, failures = [], []
    pending = []
    for gate in state["gates"]:
        if gate.get("run_id") is None:
            failures.append(f"gate {gate['gate_id']}: never dispatched or run id unknown: {gate.get('error')}")
        else:
            pending.append(gate)
    deadline = clock.monotonic() + timeout_s
    while pending:
        still = []
        for gate in pending:
            run = gh.api(f"repos/{repo}/actions/runs/{gate['run_id']}")
            if run.get("status") != "completed":
                still.append(gate)
                continue
            failures.extend(_finish_gate(gh, clock, state, gate, run, out_dir, summary))
        pending = still
        if pending and clock.monotonic() >= deadline:
            failures.extend(f"gate {g['gate_id']}: run {g['run_id']} not completed after {timeout_s:g}s" for g in pending)
            break
        if pending:
            clock.sleep(poll_s)
    state["collected"] = True
    write_state(out_dir, state)
    return not failures, summary + failures


def _finish_gate(gh, clock, state, gate, run, out_dir, summary) -> list[str]:
    raw_jobs = fetch_jobs(gh, state["repo"], gate["run_id"])
    records = [build_job_record(gh, state["repo"], raw, clock) for raw in raw_jobs]
    spec_jobs = [r for r in records if r["id"] not in CONTROL_JOBS]
    control = [r for r in records if r["id"] in CONTROL_JOBS]
    problems = check_gate(gate["spec_ids"], spec_jobs)
    stamps = [r["created_at"] for r in spec_jobs if r["created_at"]]
    ends = [r["completed_at"] for r in spec_jobs if r["completed_at"]]
    wall = _seconds(min(stamps), max(ends)) if stamps and ends else None
    document = {
        "trial_id": state["trial_id"],
        "gate_id": gate["gate_id"],
        "run_id": gate["run_id"],
        "run_url": gate.get("run_url"),
        "run_status": run.get("status"),
        "run_conclusion": run.get("conclusion"),
        "expected_ids": gate["spec_ids"],
        "jobs": spec_jobs,
        "control_jobs": control,
        "complete": not problems,
    }
    (out_dir / "gates" / f"{gate['gate_id']}.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    summary.append(f"gate {gate['gate_id']} run {gate['run_id']} jobs={len(spec_jobs)}/{len(gate['spec_ids'])} gate_wall_s={wall} conclusion={run.get('conclusion')}")
    if not problems:
        return []
    return [
        f"INCOMPLETE gate {gate['gate_id']}: missing={problems.missing} extra={problems.extra} "
        f"non_terminal={problems.non_terminal} duplicates={problems.duplicates}"
    ]
