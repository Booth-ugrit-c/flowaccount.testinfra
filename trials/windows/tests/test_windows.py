import json

from trials.windows.cli import main
from trials.windows.select import Job, choose, idle_starts, jobs_of, window_calibration


def job(id_, created, duration, wait=0.0, lane="unit", split="fit"):
    return Job(id=id_, lane=lane, split=split, created=created, wait=wait, duration=duration)


def calibration(*gates):
    return {"scenario": "t", "gates": list(gates)}


def gate(name, offset, *jobs):
    return {"gate": name, "arrival_offset_s": offset, "split": "fit", "jobs": list(jobs)}


def cjob(id_, lane="unit", split="fit", duration=10.0, wait=0.0, created_offset=0.0):
    return {"id": id_, "lane": lane, "split": split, "duration_s": duration, "queue_wait_s": wait, "created_offset_s": created_offset}


def test_idle_starts_skip_arrivals_while_earlier_work_is_unfinished():
    jobs = [job("unit-001", 0, 10), job("unit-002", 5, 10), job("unit-003", 12, 1, wait=10), job("unit-004", 22, 1)]
    assert idle_starts(jobs) == [0]


def test_idle_starts_hand_case():
    jobs = [job("unit-001", 0, 10), job("unit-002", 5, 10), job("unit-003", 30, 1), job("unit-004", 31, 1)]
    assert idle_starts(jobs) == [0, 30, 31]


def test_queue_wait_extends_the_busy_horizon():
    jobs = [job("unit-001", 0, 10, wait=20), job("unit-002", 25, 1)]
    assert idle_starts(jobs) == [0]


def test_jobs_of_uses_gate_offset_plus_job_offset_and_drops_excluded_and_null():
    cal = calibration(
        gate("g-0001", 100.0, cjob("unit-001", created_offset=5.0), cjob("unit-002", split="excluded")),
        gate("g-0002", 50.0, {**cjob("integration-001", lane="integration"), "duration_s": None}),
    )
    jobs = jobs_of(cal)
    assert [(j.id, j.created) for j in jobs] == [("unit-001", 105.0)]


def test_choose_takes_busiest_non_overlapping_windows_until_targets_met():
    jobs = [job("unit-001", 0, 50), job("unit-002", 100, 5), job("unit-003", 1000, 60), job("unit-004", 1010, 60, wait=50)]
    chosen = choose(jobs, length=200, target=2, max_windows=5)
    assert [w.start for w in chosen] == [1000]
    assert [j.id for j in chosen[0].jobs] == ["unit-003", "unit-004"]


def test_choose_stops_once_every_bucket_reaches_target():
    jobs = [job("unit-001", 0, 50), job("unit-002", 1000, 60), job("unit-003", 2000, 5)]
    chosen = choose(jobs, length=100, target=1, max_windows=5)
    assert [w.start for w in chosen] == [1000]


def test_window_calibration_rebases_offsets_and_keeps_only_window_jobs():
    cal = calibration(gate("g-0001", 100.0, cjob("unit-001")), gate("g-0002", 900.0, cjob("unit-002")))
    jobs = jobs_of(cal)
    window = choose(jobs, length=300, target=1, max_windows=1)[0]
    out = window_calibration(cal, window, "w01")
    assert out["window"] == "w01"
    assert [(g["gate"], g["arrival_offset_s"]) for g in out["gates"]] == [("g-0001", 0.0)]


def test_cli_exits_2_when_nothing_is_usable(tmp_path):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(calibration(gate("g-0001", 0.0, cjob("unit-001", split="excluded")))))
    assert main(["--calibration", str(path), "--out", str(tmp_path / "out")]) == 2


def test_cli_exits_1_when_a_bucket_stays_below_target(tmp_path):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(calibration(gate("g-0001", 0.0, cjob("unit-001")))))
    assert main(["--calibration", str(path), "--target", "2", "--out", str(tmp_path / "out")]) == 1
    manifest = json.loads((tmp_path / "out" / "windows.json").read_text())
    assert manifest["totals"] == {"unit/fit": 1}
