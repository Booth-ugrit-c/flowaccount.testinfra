import json
from pathlib import Path

import pytest

from trials.dispatch.cli import main
from trials.dispatch.gh import GhError
from trials.dispatch.replay import build_plan, discover_run_id, format_plan, run_name, run_replay
from trials.dispatch.specs import SpecError, load_calibration
from trials.dispatch.tests.fakes import FakeClock, FakeGitHub

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "demo-gate.json"
ARGS = dict(
    labels={"unit": "lane-a", "integration": "lane-b"},
    factors={"unit": 1.0, "integration": 1.0},
    threads={"unit": 1, "integration": 1},
    memory={"unit": 64, "integration": 64},
    repo="o/r",
    ref="main",
    trial_id="t-1",
)


def three_gate_calibration():
    def one(name, offset):
        return {"gate": name, "arrival_offset_s": offset, "split": "fit", "jobs": [{"id": "unit-001", "lane": "unit", "duration_s": 1.0, "services": []}]}

    return {"gates": [one("g-0001", 100.0), one("g-0002", 160.0), one("g-0003", 400.0)]}


def test_arrival_schedule_is_compressed_and_rebased_on_first_gate():
    plan = build_plan(three_gate_calibration(), split="all", limit=None, compress=4.0, **ARGS)
    assert [item.due_s for item in plan.items] == [0.0, 15.0, 75.0]


def test_replay_waits_until_each_due_time_without_real_sleep(tmp_path):
    clock = FakeClock()
    github = FakeGitHub(clock)
    plan = build_plan(three_gate_calibration(), split="all", limit=None, compress=4.0, **ARGS)
    t_start = clock.time()
    assert run_replay(plan, github.client(), clock, tmp_path)
    offsets = [round(t - t_start, 3) for t in github.dispatch_times]
    assert offsets == [0.0, 15.0, 75.0]
    assert sum(clock.sleeps) == 75.0


def test_dispatch_body_carries_ref_inputs_and_requests_run_details(tmp_path):
    clock = FakeClock()
    github = FakeGitHub(clock)
    plan = build_plan(load_calibration(FIXTURE), split="all", limit=None, compress=1.0, **ARGS)
    assert run_replay(plan, github.client(), clock, tmp_path)
    body = github.dispatches[0]
    assert body["ref"] == "main" and body["return_run_details"] is True
    assert body["inputs"]["trial_id"] == "t-1" and body["inputs"]["gate_id"] == "g-0001"
    assert [s["id"] for s in json.loads(body["inputs"]["jobs"])] == [
        "unit-001", "unit-002", "unit-003", "integration-001", "integration-002",
    ]
    state = json.loads((tmp_path / "dispatch.json").read_text(encoding="utf-8"))
    gate = state["gates"][0]
    assert gate["run_id"] == 9000 and gate["run_id_source"] == "dispatch-response"
    assert gate["spec_ids"] == [s["id"] for s in json.loads(body["inputs"]["jobs"])]


def test_run_id_discovery_by_run_name_when_response_has_no_details(tmp_path):
    clock = FakeClock()
    github = FakeGitHub(clock, return_run_details=False)
    github.show_run_after_polls = 2
    plan = build_plan(load_calibration(FIXTURE), split="all", limit=None, compress=1.0, **ARGS)
    assert run_replay(plan, github.client(), clock, tmp_path, discovery_interval_s=3.0)
    gate = json.loads((tmp_path / "dispatch.json").read_text(encoding="utf-8"))["gates"][0]
    assert gate["run_id"] == 9000 and gate["run_id_source"] == "run-name-poll"
    assert github.run_list_polls == 3
    assert clock.sleeps == [3.0, 3.0]


def test_discovery_matches_only_the_exact_run_name_and_times_out():
    clock = FakeClock()
    github = FakeGitHub(clock)
    github.runs[1] = {"id": 1, "name": "trial t-1 gate g-0009", "display_title": "trial t-1 gate g-0009"}
    with pytest.raises(GhError, match="no run named"):
        discover_run_id(github.client(), "o/r", run_name("t-1", "g-0001"), clock.time(), clock, timeout_s=10.0, interval_s=3.0)


def test_discovery_refuses_ambiguous_run_names():
    clock = FakeClock()
    github = FakeGitHub(clock)
    for run_id in (1, 2):
        github.runs[run_id] = {"id": run_id, "name": "trial t-1 gate g-0001", "display_title": "trial t-1 gate g-0001"}
    with pytest.raises(GhError, match="2 runs match"):
        discover_run_id(github.client(), "o/r", run_name("t-1", "g-0001"), clock.time(), clock, timeout_s=10.0, interval_s=3.0)


def test_rate_limit_sleeps_until_reset_then_retries_the_dispatch(tmp_path):
    clock = FakeClock()
    github = FakeGitHub(clock)
    github.rate_limit_failures = 1
    github.reset_in = 120
    plan = build_plan(load_calibration(FIXTURE), split="all", limit=None, compress=1.0, **ARGS)
    assert run_replay(plan, github.client(), clock, tmp_path)
    assert clock.sleeps[0] == pytest.approx(121.0)
    assert len(github.dispatches) == 1


def test_replay_refuses_to_reuse_an_out_dir(tmp_path):
    clock = FakeClock()
    github = FakeGitHub(clock)
    plan = build_plan(load_calibration(FIXTURE), split="all", limit=None, compress=1.0, **ARGS)
    run_replay(plan, github.client(), clock, tmp_path)
    with pytest.raises(SpecError, match="already exists"):
        run_replay(plan, github.client(), clock, tmp_path)


def test_gate_with_only_null_durations_is_recorded_not_dispatched(tmp_path):
    calibration = {"gates": [{"gate": "g-0001", "arrival_offset_s": 0, "split": "fit", "jobs": [{"id": "unit-001", "lane": "unit", "duration_s": None}]}]}
    clock = FakeClock()
    github = FakeGitHub(clock)
    plan = build_plan(calibration, split="all", limit=None, compress=1.0, **ARGS)
    assert run_replay(plan, github.client(), clock, tmp_path)
    state = json.loads((tmp_path / "dispatch.json").read_text(encoding="utf-8"))
    assert github.dispatches == [] and state["gates"] == [] and state["skipped_gates"][0]["gate_id"] == "g-0001"


def test_dry_run_prints_the_plan_and_calls_nothing(capsys, tmp_path):
    class Boom:
        def api(self, *a, **k):
            raise AssertionError("dry run must not call gh")

    code = main(
        [
            "replay", "--calibration", str(FIXTURE), "--split", "all",
            "--labels", "unit=ubuntu-24.04,integration=ubuntu-24.04",
            "--factor", "unit=0.25,integration=0.25", "--threads", "unit=2,integration=2",
            "--memory", "unit=256,integration=512", "--compress", "1",
            "--repo", "o/r", "--ref", "main", "--trial-id", "demo-001", "--out", str(tmp_path / "out"), "--dry-run",
        ],
        gh=Boom(),
        clock=FakeClock(),
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert printed.startswith("plan: 1 gate / 5 jobs (0 null-duration skipped)")
    assert '"cpu_seconds":2.5' in printed and '"cpu_seconds":5.0' in printed
    assert not (tmp_path / "out").exists()


def test_format_plan_counts_skipped_jobs():
    calibration = three_gate_calibration()
    calibration["gates"][0]["jobs"].append({"id": "unit-002", "lane": "unit", "duration_s": None})
    plan = build_plan(calibration, split="all", limit=1, compress=1.0, **ARGS)
    assert format_plan(plan).splitlines()[0] == "plan: 1 gate / 1 jobs (1 null-duration skipped)"
