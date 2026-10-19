import json
import re

from trials.dispatch.clock import Clock
from trials.dispatch.gh import GhClient, GhResult

T0 = "2026-01-01T00:00:00Z"


class FakeClock(Clock):
    def __init__(self):
        self.now = 1_800_000_000.0
        self.mono = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.mono

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.mono += seconds
        self.now += seconds


def ok(payload) -> GhResult:
    return GhResult(0, json.dumps(payload) if payload is not None else "", "")


def fail(message: str) -> GhResult:
    return GhResult(1, "", message)


def iso(seconds: int) -> str:
    return f"2026-01-01T{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}Z"


def job(name: str, job_id: int, *, created=0, started=5, completed=65, conclusion="success", status="completed", runner="runner-1") -> dict:
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "created_at": iso(created),
        "started_at": iso(started) if started is not None else None,
        "completed_at": iso(completed) if completed is not None else None,
        "runner_name": runner,
    }


class FakeGitHub:
    def __init__(self, clock: FakeClock, return_run_details: bool = True):
        self.clock = clock
        self.return_run_details = return_run_details
        self.next_run_id = 9000
        self.dispatches: list[dict] = []
        self.dispatch_times: list[float] = []
        self.runs: dict[int, dict] = {}
        self.jobs: dict[int, list[dict]] = {}
        self.logs: dict[int, str] = {}
        self.calls: list[list[str]] = []
        self.run_list_polls = 0
        self.show_run_after_polls = 0
        self.rate_limit_failures = 0
        self.reset_in = 120

    def runner(self, args, stdin):
        self.calls.append(args)
        if args[:2] == ["api", "rate_limit"]:
            return ok({"resources": {"core": {"remaining": 0, "reset": self.clock.time() + self.reset_in}}})
        method, path = args[2], args[3]
        route, _, query = path.partition("?")
        if self.rate_limit_failures:
            self.rate_limit_failures -= 1
            return fail("gh: API rate limit exceeded (HTTP 403)")
        if method == "POST" and route.endswith("/dispatches"):
            return self._dispatch(json.loads(stdin))
        if route.endswith("/workflows/trial-gate.yml/runs"):
            self.run_list_polls += 1
            visible = self.run_list_polls > self.show_run_after_polls
            return ok({"workflow_runs": list(self.runs.values()) if visible else []})
        match = re.search(r"/actions/runs/(\d+)/jobs$", route)
        if match:
            batch = self.jobs[int(match.group(1))]
            page = int(re.search(r"(?:^|&)page=(\d+)", query).group(1))
            per = int(re.search(r"per_page=(\d+)", query).group(1))
            return ok({"total_count": len(batch), "jobs": batch[(page - 1) * per : page * per]})
        match = re.search(r"/actions/runs/(\d+)$", route)
        if match:
            return ok(self.runs[int(match.group(1))])
        return fail(f"unrouted {method} {path}")

    def _dispatch(self, body) -> GhResult:
        self.dispatches.append(body)
        self.dispatch_times.append(self.clock.time())
        run_id = self.next_run_id
        self.next_run_id += 1
        inputs = body["inputs"]
        self.runs[run_id] = {
            "id": run_id,
            "name": f"trial {inputs['trial_id']} gate {inputs['gate_id']}",
            "display_title": f"trial {inputs['trial_id']} gate {inputs['gate_id']}",
            "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
            "status": "completed",
            "conclusion": "success",
        }
        specs = json.loads(inputs["jobs"])
        self.jobs[run_id] = [job("validate", run_id * 100)] + [job(s["id"], run_id * 100 + i + 1) for i, s in enumerate(specs)]
        if self.return_run_details and body.get("return_run_details"):
            return ok({"workflow_run_id": run_id, "run_url": "u", "html_url": self.runs[run_id]["html_url"]})
        return ok(None)

    def client(self) -> GhClient:
        return GhClient(runner=self.runner, clock=self.clock, log_fetcher=self.log)

    def log(self, repo, job_id):
        return self.logs.get(job_id, f"2026-01-01T00:00:09Z workload-result {json.dumps({'ok': True, 'job': job_id})}\n")
