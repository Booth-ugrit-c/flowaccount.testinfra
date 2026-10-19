import json

import pytest

from trials.sim.cli import main
from trials.sim.core import E2EWL, Gate, UnitWL, load_e2e, load_unit, run_ids, simulate, timeline


def approx(x):
    return pytest.approx(x, abs=1e-6)


def unit_only(unit, **kw):
    return simulate(kw.pop("arm"), [Gate(0.0, unit, None)], **kw)


def test_longest_first_makespan_two_slots():
    # tasks [3,3,2] on 2 slots longest-first: 3|3, then 2 on the first freed lane => 3 + 2 = 5
    r = unit_only(UnitWL(affected=[3, 3, 2]), arm="B", unit_cpu=2.0, pool=2.0)
    assert r["wall"] == approx(5.0)


def test_longest_first_beats_list_order():
    # list order [2,2,3] on 2 lanes: 2|2, then 3 => 5. longest-first [3,2,2]: 3|2, then 2 on lane two => 4.
    wl = UnitWL(affected=[2, 2, 3])
    a = unit_only(wl, arm="A", unit_cpu=2.0, pool=2.0, a_conc=(2, 2, 2))
    b = unit_only(wl, arm="B", unit_cpu=2.0, pool=2.0)
    assert a["wall"] == approx(5.0)
    assert b["wall"] == approx(4.0)


def barrier_workload():
    return UnitWL(build=[4, 4], affected=[1, 1], ui=[4])


def test_barrier_case_arm_a():
    # pool 4, barriers 2/2/2: build 0-4 (2 busy), affected 4-5 (2 busy), ui 5-9 (1 busy).
    r = unit_only(barrier_workload(), arm="A", unit_cpu=4.0, pool=4.0, a_conc=(2, 2, 2))
    assert r["wall"] == approx(4 + 1 + 4)
    # vacuum window = [0,5) while ui is unstarted: idle 2 CPU for 4 s + 2 CPU for 1 s over 4 CPU x 5 s
    assert r["vacuum_window_s"] == approx(5.0)
    assert r["vacuum_share"] == approx((2 * 4 + 2 * 1) / (4 * 5))
    assert r["vacuum_cause_cpu_s"]["step_barrier"] == approx(2 * 4 + 2 * 1)
    # whole window adds the ui phase: idle 3 CPU x 4 s; 4 CPU x 9 s available
    assert r["idle_share"] == approx((2 * 4 + 2 * 1 + 3 * 4) / (4 * 9))


def test_barrier_case_arm_b_has_no_barrier():
    # 4 lanes, [4,4,4,1,1]: 4,4,4,1 start at 0, the second 1 starts at t=1 => all started at 1, last ends at 4
    r = unit_only(barrier_workload(), arm="B", unit_cpu=4.0, pool=4.0)
    assert r["wall"] == approx(4.0)
    assert r["vacuum_window_s"] == approx(1.0)
    assert r["vacuum_share"] == approx(0.0)
    # three lanes busy for t in [2,4): idle 1 CPU x 2 s of 4 CPU x 4 s, all of it tail
    assert r["idle_share"] == approx((1 * 2) / (4 * 4))
    assert r["idle_cause_cpu_s"]["tail"] == approx(1 * 2)


def e2e_only(tests, arm, **kw):
    return simulate(arm, [Gate(0.0, UnitWL(), E2EWL(0, 0, tests))], **kw)


@pytest.mark.parametrize("arm", ["B", "C"])
def test_account_cap_cpu_free_but_tests_wait(arm):
    # pool 4, w=1, accounts 2: only 2 workers although 4 fit. Tests [2,2,2,2] => 2 waves => wall 4.
    r = e2e_only([2, 2, 2, 2], arm, w=1.0, accounts=2, unit_cpu=0.0, pool=4.0)
    assert r["wall"] == approx(4.0)
    # tests 3 and 4 are unstarted during [0,2): idle 2 CPU of 4; all of it account_cap = 2 CPU x 2 s
    assert r["vacuum_window_s"] == approx(2.0)
    assert r["vacuum_share"] == approx(2 / 4)
    assert r["vacuum_cause_cpu_s"]["account_cap"] == approx(2 * 2)
    assert r["held_max"] == 2


def test_pool_worker_starts_when_unit_task_ends():
    # pool 2, unit tasks [3,5] on 2 unit pods; 2 e2e pods (w=1) Pending. Unit pod one exits at 3:
    # e2e pod one binds at exactly 3, runs test one 3-5 and then takes test two 5-7 (the second e2e pod
    # binds at 5 when the second unit pod exits, finds no test left and exits).
    wl = Gate(0.0, UnitWL(affected=[3, 5]), E2EWL(0, 0, [2, 2]))
    r = simulate("C", [wl], w=1.0, accounts=2, pool=2.0)
    g = r["gates"][0]
    assert g["e2e_first_test_start"] == approx(3.0)
    assert g["unit_end"] == approx(5.0)
    assert g["e2e_end"] == approx(7.0)
    assert r["wall"] == approx(7.0)


def test_pool_both_unit_pods_exit_together():
    # unit [3,3] on 2 pods, e2e tests [2,2] w=1: both e2e pods bind at 3 and finish at 5.
    wl = Gate(0.0, UnitWL(affected=[3, 3]), E2EWL(0, 0, [2, 2]))
    r = simulate("C", [wl], w=1.0, accounts=2, pool=2.0)
    assert r["gates"][0]["e2e_first_test_start"] == approx(3.0)
    assert r["wall"] == approx(5.0)
    assert r["held_max"] == 2


def test_pod_overhead_and_sched_latency_and_startup():
    # pool 1, one e2e test of 2 s, w=1, startup 3 s: create at 0, bound at latency 1, started at +2 overhead = 3,
    # startup 3 => 6, test 6-8.
    g = Gate(0.0, UnitWL(), E2EWL(0, 3, [2]))
    r = simulate("C", [g], w=1.0, accounts=1, pool=1.0, pod_overhead=2.0, sched_latency=1.0)
    assert r["gates"][0]["e2e_first_test_start"] == approx(6.0)
    assert r["wall"] == approx(8.0)
    # unit: bound 1, start 3, 2 s task => 5
    r2 = simulate("C", [Gate(0.0, UnitWL(affected=[2]), None)], pool=1.0, pod_overhead=2.0, sched_latency=1.0)
    assert r2["wall"] == approx(5.0)


def test_build_prerequisite_and_startup_arms_b_and_c():
    # pool 3, w=1, build-e2e 4 s, startup 1 s, tests [2,2], one unit task [1]. Tests run 5-7 in both arms.
    # Vacuum window [0,5), pool 3: idle 1 CPU x 1 s + 2 CPU x 3 s + 3 CPU x 1 s.
    mk = lambda: [Gate(0.0, UnitWL(affected=[1]), E2EWL(4, 1, [2, 2]))]
    vacuum = (1 * 1 + 2 * 3 + 3 * 1) / (3 * 5)
    b = simulate("B", mk(), w=1.0, accounts=9, unit_cpu=1.0, pool=3.0)
    assert b["wall"] == approx(7.0)
    assert b["vacuum_share"] == approx(vacuum)
    # unit partition idle during [1,5) = 1 CPU x 4 s (partition boundary); e2e partition (2 CPU, 1 busy)
    # idle during build = 1 CPU x 4 s, during startup = 2 CPU x 1 s
    assert b["vacuum_cause_cpu_s"]["partition_boundary"] == approx(1 * 4)
    assert b["vacuum_cause_cpu_s"]["e2e_prereq"] == approx(1 * 4)
    assert b["vacuum_cause_cpu_s"]["startup"] == approx(2 * 1)
    c = simulate("C", mk(), w=1.0, accounts=9, pool=3.0)
    assert c["wall"] == approx(7.0)
    assert c["vacuum_share"] == approx(vacuum)
    # [0,4): 1 CPU (t<1) + 2 CPU (1<=t<4) unusable until the build ends = 1 + 6; [4,5): 2 startup pods + 1 free spoken for = 3
    assert c["vacuum_cause_cpu_s"]["e2e_prereq"] == approx(1 * 1 + 2 * 3)
    assert c["vacuum_cause_cpu_s"]["startup"] == approx(3 * 1)


def test_two_gates_fixed_partition_unit_waits_for_runner():
    # pool 1: gate one unit [4]; gate two arrives at 1 with unit [1]; the unit partition is one exclusive runner.
    mk = lambda: [Gate(0.0, UnitWL(affected=[4]), None), Gate(1.0, UnitWL(affected=[1]), None)]
    for arm in ("A", "B"):
        r = simulate(arm, mk(), unit_cpu=1.0, pool=1.0, a_conc=(1, 1, 1))
        assert r["gates"][1]["unit_start_delay"] == approx(3.0)
        assert r["gates"][1]["wall"] == approx(4.0)


def two_pr_pool():
    return [Gate(0.0, UnitWL(), E2EWL(0, 0, [4, 4])), Gate(1.0, UnitWL(affected=[1]), None)]


def test_two_gates_pool_unit_waits_behind_running_e2e_worker():
    # pool 1 (one pod at a time), accounts 1. Gate one's worker holds the pool 0-8; gate two's unit pod waits => starts 8.
    r = simulate("C", two_pr_pool(), w=1.0, accounts=1, pool=1.0)
    assert r["gates"][1]["unit_start_delay"] == approx(7.0)
    assert r["gates"][0]["wall"] == approx(8.0)


def test_two_gates_pool_time_slice_lets_unit_in():
    # T=4: worker exits after test one at 4; unit pod (higher priority) takes the pool 4-5 (delay 3),
    # the replacement worker runs test two 5-9.
    r = simulate("C", two_pr_pool(), w=1.0, accounts=1, pool=1.0, slice_T=4)
    assert r["gates"][1]["unit_start_delay"] == approx(3.0)
    assert r["gates"][1]["wall"] == approx(4.0)
    assert r["gates"][0]["wall"] == approx(9.0)


@pytest.mark.parametrize("arm", ["A", "B", "C"])
def test_work_conservation_on_synthetic_inputs(arm):
    # integral of busy CPU over time equals the work in the inputs (independent arithmetic), whatever the schedule
    u = UnitWL(build=[4, 4], affected=[3, 5, 2, 4], ui=[6])
    e = E2EWL(3, 2, [5, 7, 3, 4, 6])
    w, pool = 1.5, 6.0
    work = (4 + 4 + 3 + 5 + 2 + 4 + 6) + 3 + (5 + 7 + 3 + 4 + 6) * w
    r = simulate(arm, [Gate(0.0, u, e)], w=w, accounts=3, unit_cpu=3.0, pool=pool, a_conc=(2, 2, 2), keep_rows=True)
    rows = r["rows"]
    busy = sum((rows[i][1] + rows[i][2]) * (rows[i + 1][0] - rows[i][0]) for i in range(len(rows) - 1)) / 1000
    assert busy == pytest.approx(work, rel=1e-9)
    assert r["wall"] >= work / pool - 1e-6
    assert r["idle_cause_cpu_s"]["other"] == approx(0.0)


def test_arm_a_step_concurrency_limits_lanes():
    # 4 lanes but barriers 1/2/2: build [2,2] one at a time = 4; affected [3,3,3] two at a time = 3 + 3 = 6;
    # ui [4,4,4] two at a time = 4 + 4 = 8
    wl = UnitWL(build=[2, 2], affected=[3, 3, 3], ui=[4, 4, 4])
    r = unit_only(wl, arm="A", unit_cpu=4.0, pool=4.0, a_conc=(1, 2, 2))
    assert r["wall"] == approx(4 + 6 + 8)


@pytest.mark.parametrize("arm", ["A", "B"])
def test_idle_unit_partition_with_nothing_unstarted_is_tail(arm):
    # pool 3 = unit partition 1 CPU + E2E partition 2 CPU. Unit [1], one E2E test of 4 s on one worker (w=1).
    # Unit partition idle 1 CPU x 3 s, E2E partition idle 1 CPU x 4 s; every task has started, so all idle is tail.
    gate = Gate(0.0, UnitWL(affected=[1]), E2EWL(0, 0, [4]))
    r = simulate(arm, [gate], w=1.0, accounts=1, unit_cpu=1.0, pool=3.0, a_conc=(1, 1, 1))
    assert r["wall"] == approx(4.0)
    assert r["idle_cause_cpu_s"]["tail"] == approx(1 * 3 + 1 * 4)
    assert r["idle_cause_cpu_s"]["partition_boundary"] == approx(0.0)


def test_pool_accounts_are_shared_across_gates():
    # pool 4, w=1, one account, two gates at 0 with one 3 s test each. Gate two's worker pod binds and waits
    # for the account while holding 1 CPU: gate one runs 0-3, gate two 3-6.
    mk = lambda: Gate(0.0, UnitWL(), E2EWL(0, 0, [3]))
    r = simulate("C", [mk(), mk()], w=1.0, accounts=1, pool=4.0)
    assert r["gates"][0]["wall"] == approx(3.0)
    assert r["gates"][1]["wall"] == approx(6.0)
    # [0,3): 1 CPU busy of 4; the other 3 CPU are held or free for a worker that has no account
    assert r["vacuum_cause_cpu_s"]["account_cap"] == approx(3 * 3)


UNIT_MODEL = {"runs": [
    {"run_id": 701, "build_s": 6, "projects": [
        {"step": "affected", "test_s": 3, "cache_hit": False},
        {"step": "affected", "test_s": 8, "cache_hit": True},
        {"step": "affected", "test_s": 9, "cache_hit": False, "no_tests": True},
        {"step": "affected", "test_s": None, "cache_hit": False},
        {"step": "affected", "test_s": 5, "cache_hit": False},
        {"step": "ui", "test_s": 2, "cache_hit": False},
        {"step": "ui", "test_s": 7, "cache_hit": True},
    ]},
    {"run_id": 702, "build_s": None, "projects": [{"step": "ui", "test_s": 4, "cache_hit": False}]},
]}

E2E_MODEL = {"runs": [
    {"run_id": 801, "build_s": 8, "startup_s": 3, "tests": [
        {"id": "t1", "start_offset_s": 5, "dur_s": 7},
        {"id": "t2", "start_offset_s": 2, "dur_s": 4},
        {"id": "t0", "start_offset_s": 5, "dur_s": 3},
    ]},
]}


@pytest.fixture
def models(tmp_path):
    unit, e2e = tmp_path / "unit.json", tmp_path / "e2e.json"
    unit.write_text(json.dumps(UNIT_MODEL))
    e2e.write_text(json.dumps(E2E_MODEL))
    return unit, e2e


def test_loader_matches_hand_counts_from_json(models):
    unit, e2e = models
    u = load_unit(unit, "701", build_lanes=2)
    assert u.build == [6, 6]
    assert u.affected == [3, 5]
    assert u.ui == [2]
    u2 = load_unit(unit, 702)
    assert u2.build == [] and u2.affected == [] and u2.ui == [4]
    e = load_e2e(e2e, "801")
    assert e.build_s == 8 and e.startup_s == 3
    assert e.tests == [4, 3, 7]
    assert run_ids(unit) == ["701", "702"]


def test_cli_runs_lists_ids(models, capsys):
    assert main(["runs", str(models[0])]) == 0
    assert capsys.readouterr().out.split() == ["701", "702"]


def test_cli_run_prints_summary(models, capsys):
    unit, _ = models
    # run 701 with one build lane: build [6], affected [3,5], ui [2]; arm B, 2 lanes, longest-first:
    # 6|5, then 3 on the lane freed at 5 (ends 8), then 2 on the lane freed at 6 (ends 8) => wall 8
    args = ["run", "--arm", "B", "--unit-model", str(unit), "--unit-run", "701", "--unit-cpu", "2", "--pool", "2"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["wall"] == approx(8.0)


def test_cli_rejects_model_without_run(models):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--arm", "B", "--unit-model", str(models[0])])
    assert exc.value.code == 2


def test_timeline_averages_per_step():
    # one 4 s task on one of two CPUs: every 2 s step has 1 CPU busy, 1 idle, no e2e CPU
    r = unit_only(UnitWL(affected=[4]), arm="B", unit_cpu=2.0, pool=2.0, keep_rows=True)
    tl = timeline(r, 2.0)
    assert [x["t"] for x in tl] == [0.0, 2.0]
    assert [x["unit_cpu"] for x in tl] == [approx(1.0), approx(1.0)]
    assert [x["idle"] for x in tl] == [approx(1.0), approx(1.0)]
    assert [x["e2e_cpu"] for x in tl] == [approx(0.0), approx(0.0)]
