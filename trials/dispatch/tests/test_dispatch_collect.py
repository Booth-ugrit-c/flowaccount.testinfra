import json
from pathlib import Path

from trials.dispatch.cli import main
from trials.dispatch.collect import check_gate, collect, parse_workload_result
from trials.dispatch.replay import build_plan, run_replay
from trials.dispatch.specs import load_calibration
from trials.dispatch.tests.fakes import FakeClock, FakeGitHub, job

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "demo-gate.json"
IDS = ["unit-001", "unit-002", "unit-003", "integration-001", "integration-002"]
ARGS = dict(
    labels={"unit": "lane-a", "integration": "lane-b"},
    factors={"unit": 1.0, "integration": 1.0},
    threads={"unit": 1, "integration": 1},
    memory={"unit": 64, "integration": 64},
    repo="o/r",
    ref="main",
    trial_id="t-1",
)


def dispatched(tmp_path):
    clock = FakeClock()
    github = FakeGitHub(clock)
    plan = build_plan(load_calibration(FIXTURE), split="all", limit=None, compress=1.0, **ARGS)
    assert run_replay(plan, github.client(), clock, tmp_path)
    return clock, github


def gate_file(tmp_path):
    return json.loads((tmp_path / "gates" / "g-0001.json").read_text(encoding="utf-8"))


def test_collect_writes_one_raw_file_per_gate_with_timeline_and_result(tmp_path):
    clock, github = dispatched(tmp_path)
    ok, lines = collect(tmp_path, github.client(), clock)
    assert ok, lines
    document = gate_file(tmp_path)
    assert document["complete"] is True and document["run_id"] == 9000
    assert [j["id"] for j in document["jobs"]] == IDS
    first = document["jobs"][0]
    assert first["created_at"] == "2026-01-01T00:00:00Z"
    assert first["started_at"] == "2026-01-01T00:00:05Z"
    assert first["completed_at"] == "2026-01-01T00:01:05Z"
    assert first["conclusion"] == "success" and first["runner_name"] == "runner-1"
    assert first["workload_result"] == {"ok": True, "job": first["job_id"]}
    assert [j["id"] for j in document["control_jobs"]] == ["validate"]
    assert "gate_wall_s=65.0" in lines[0]


def test_collect_polls_until_the_run_completes(tmp_path):
    clock, github = dispatched(tmp_path)
    run = github.runs[9000]
    run["status"] = "in_progress"
    polls = []
    original = github.runner

    def runner(args, stdin):
        if args[3] == "repos/o/r/actions/runs/9000":
            polls.append(1)
            if len(polls) == 3:
                run["status"] = "completed"
        return original(args, stdin)

    github.runner = runner
    ok, _ = collect(tmp_path, github.client(), clock, poll_s=10.0)
    assert ok and len(polls) == 3 and clock.sleeps == [10.0, 10.0]


def test_collect_reports_a_run_that_never_completes(tmp_path):
    clock, github = dispatched(tmp_path)
    github.runs[9000]["status"] = "in_progress"
    ok, lines = collect(tmp_path, github.client(), clock, poll_s=10.0, timeout_s=30.0)
    assert not ok and "not completed after 30s" in lines[-1]


def test_missing_job_id_makes_collect_fail_even_though_every_collected_job_is_terminal(tmp_path):
    clock, github = dispatched(tmp_path)
    github.jobs[9000] = [j for j in github.jobs[9000] if j["name"] != "integration-002"]
    ok, lines = collect(tmp_path, github.client(), clock)
    assert not ok
    assert any("INCOMPLETE gate g-0001" in line and "missing=['integration-002']" in line for line in lines)
    assert gate_file(tmp_path)["complete"] is False


def test_equal_count_with_a_wrong_id_still_fails(tmp_path):
    clock, github = dispatched(tmp_path)
    for raw in github.jobs[9000]:
        if raw["name"] == "unit-003":
            raw["name"] = "unit-099"
    ok, lines = collect(tmp_path, github.client(), clock)
    assert not ok
    assert "missing=['unit-003']" in lines[-1] and "extra=['unit-099']" in lines[-1]


def test_duplicate_job_name_fails(tmp_path):
    clock, github = dispatched(tmp_path)
    github.jobs[9000].append(job("unit-001", 5))
    ok, lines = collect(tmp_path, github.client(), clock)
    assert not ok and "duplicates=['unit-001']" in lines[-1]


def test_non_terminal_job_in_a_completed_run_fails(tmp_path):
    clock, github = dispatched(tmp_path)
    for raw in github.jobs[9000]:
        if raw["name"] == "unit-002":
            raw["status"], raw["conclusion"], raw["completed_at"] = "in_progress", None, None
    ok, lines = collect(tmp_path, github.client(), clock)
    assert not ok and "non_terminal=['unit-002']" in lines[-1]


def test_cli_collect_exits_nonzero_on_a_missing_id_and_zero_when_complete(tmp_path, capsys):
    clock, github = dispatched(tmp_path)
    assert main(["collect", "--out", str(tmp_path)], gh=github.client(), clock=clock) == 0
    github.jobs[9000] = github.jobs[9000][:-1]
    assert main(["collect", "--out", str(tmp_path)], gh=github.client(), clock=clock) == 1
    assert "collect: INCOMPLETE" in capsys.readouterr().out


def test_gate_without_a_run_id_is_incomplete(tmp_path):
    clock, github = dispatched(tmp_path)
    state_path = tmp_path / "dispatch.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["gates"][0]["run_id"] = None
    state["gates"][0]["error"] = "no run named"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    ok, lines = collect(tmp_path, github.client(), clock)
    assert not ok and "never dispatched or run id unknown" in lines[0]


def test_log_failure_leaves_a_null_result_with_the_error_recorded(tmp_path):
    from trials.dispatch.gh import GhError

    clock, github = dispatched(tmp_path)

    def log(repo, job_id):
        raise GhError("404")

    client = github.client()
    client.log_fetcher = log
    ok, _ = collect(tmp_path, client, clock)
    first = gate_file(tmp_path)["jobs"][0]
    assert ok and first["workload_result"] is None and first["log_error"] == "404"


def test_check_gate_unit():
    def record(name):
        return {"id": name, "status": "completed", "conclusion": "success"}

    records = [record(name) for name in IDS]
    assert not check_gate(IDS, records)
    assert check_gate(IDS, records[:-1]).missing == ["integration-002"]
    assert check_gate(IDS, records + [record("unit-050")]).extra == ["unit-050"]


def test_parse_workload_result_takes_the_last_valid_line():
    log = "x\n2026-01-01T00:00:01Z workload-result {bad\n2026-01-01T00:00:02Z workload-result {\"ok\": true, \"n\": 1}\nend\n"
    assert parse_workload_result(log) == {"ok": True, "n": 1}
    assert parse_workload_result("nothing here") is None


def test_logs_are_fetched_only_for_started_non_control_jobs(tmp_path):
    clock, github = dispatched(tmp_path)
    for raw in github.jobs[9000]:
        if raw["name"] == "unit-003":
            raw.update(started_at=None, completed_at=None, conclusion="skipped")
    fetched = []

    def log(repo, job_id):
        fetched.append(job_id)
        return ""

    client = github.client()
    client.log_fetcher = log
    ok, _ = collect(tmp_path, client, clock)
    names = {raw["id"]: raw["name"] for raw in github.jobs[9000]}
    assert ok and sorted(names[j] for j in fetched) == ["integration-001", "integration-002", "unit-001", "unit-002"]


def test_failed_control_job_fails_collect_even_when_the_job_set_is_complete(tmp_path, capsys):
    clock, github = dispatched(tmp_path)
    for raw in github.jobs[9000]:
        if raw["name"] == "validate":
            raw["conclusion"] = "failure"
    ok, lines = collect(tmp_path, github.client(), clock)
    assert not ok
    assert "FAILED gate g-0001: control job validate conclusion=failure" in lines[-1]
    assert gate_file(tmp_path)["failed_reasons"] == ["control job validate conclusion=failure"]
    assert main(["collect", "--out", str(tmp_path)], gh=github.client(), clock=clock) == 1
    assert "collect: INCOMPLETE" in capsys.readouterr().out


def test_failed_cancelled_or_timed_out_run_fails_collect_even_when_the_job_set_is_complete(tmp_path):
    for conclusion in ("failure", "cancelled", "timed_out"):
        out = tmp_path / conclusion
        out.mkdir()
        clock, github = dispatched(out)
        github.runs[9000]["conclusion"] = conclusion
        ok, lines = collect(out, github.client(), clock)
        assert not ok
        assert f"FAILED gate g-0001: run conclusion={conclusion}" in lines[-1]


def test_successful_run_with_successful_control_job_still_passes(tmp_path):
    clock, github = dispatched(tmp_path)
    ok, _ = collect(tmp_path, github.client(), clock)
    assert ok and gate_file(tmp_path)["failed_reasons"] == []
