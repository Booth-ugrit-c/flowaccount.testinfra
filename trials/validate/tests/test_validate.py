import json

import pytest

from trials.validate.cli import main
from trials.validate.stats import fit_factors, paired, quartiles, real_jobs, replay_jobs, verdict


def write_window(path, jobs):
    path.write_text(json.dumps({"window": "w01", "gates": [{"gate": "g-0001", "arrival_offset_s": 0.0, "split": "fit", "jobs": jobs}]}))
    return path


def wjob(id_, duration, wait=0.0, lane="unit", split="holdout"):
    return {"id": id_, "lane": lane, "split": split, "duration_s": duration, "queue_wait_s": wait}


def write_collected(directory, rows):
    (directory / "gates").mkdir(parents=True)
    jobs = [
        {"id": id_, "conclusion": "success", "created_at": f"2026-01-01T00:00:{c:02d}Z", "started_at": f"2026-01-01T00:00:{s:02d}Z", "completed_at": f"2026-01-01T00:00:{e:02d}Z"}
        for id_, c, s, e in rows
    ]
    (directory / "gates" / "g-0001.json").write_text(json.dumps({"jobs": jobs}))
    return directory


def test_quartiles_inclusive_hand_values():
    assert quartiles([1, 2, 3, 4, 5]) == (2, 3, 4)


def test_replay_jobs_scale_by_compression(tmp_path):
    d = write_collected(tmp_path / "c", [("unit-001", 0, 2, 7)])
    assert replay_jobs([d], compress=4) == {"unit-001": {"wait": 8.0, "duration": 20.0, "wall": 28.0}}


def test_replay_jobs_reject_a_failed_job(tmp_path):
    d = write_collected(tmp_path / "c", [("unit-001", 0, 2, 7)])
    path = d / "gates" / "g-0001.json"
    data = json.loads(path.read_text())
    data["jobs"][0]["conclusion"] = "failure"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="did not succeed"):
        replay_jobs([d], compress=1)


def test_paired_requires_every_real_job(tmp_path):
    with pytest.raises(ValueError, match="no replay record"):
        paired({"unit-001": {}, "unit-002": {}}, {"unit-001": {}})


def test_fit_factor_scales_by_real_over_replay_median():
    pairs = [
        ({"lane": "unit", "split": "fit", "duration": 100}, {"duration": 50}),
        ({"lane": "unit", "split": "fit", "duration": 200}, {"duration": 100}),
        ({"lane": "unit", "split": "holdout", "duration": 999}, {"duration": 1}),
    ]
    assert fit_factors(pairs, {"unit": 0.5}) == {"unit": 0.5 * 150 / 75}


def test_fit_refuses_a_zero_replay_median():
    pairs = [({"lane": "unit", "split": "fit", "duration": 100}, {"duration": 0})] * 2
    with pytest.raises(ValueError, match="replay median duration is 0"):
        fit_factors(pairs, {"unit": 1.0})


def test_verdict_passes_inside_real_iqr_and_fails_outside():
    real = [10, 20, 30, 40, 50]
    inside = [({"lane": "unit", "split": "holdout", "wall": r, "wait": r}, {"wall": r, "wait": r}) for r in real]
    assert all(row["pass"] for row in verdict(inside))
    doubled = [({"lane": "unit", "split": "holdout", "wall": r, "wait": r}, {"wall": 2 * r, "wait": 2 * r}) for r in real]
    rows = verdict(doubled)
    assert [(r["metric"], r["real_q1"], r["real_q3"], r["replay_median"], r["pass"]) for r in rows] == [
        ("wall", 20, 40, 60, False),
        ("wait", 20, 40, 60, False),
    ]


def test_cli_verdict_exit_codes(tmp_path):
    window = write_window(tmp_path / "w01.json", [wjob("unit-001", 10), wjob("unit-002", 20), wjob("unit-003", 30)])
    good = write_collected(tmp_path / "good", [("unit-001", 0, 0, 10), ("unit-002", 0, 0, 20), ("unit-003", 0, 0, 30)])
    bad = write_collected(tmp_path / "bad", [("unit-001", 0, 0, 20), ("unit-002", 0, 0, 40), ("unit-003", 0, 0, 59)])
    args = ["verdict", "--windows", str(window), "--compress", "1", "--collected"]
    assert main(args + [str(good)]) == 0
    assert main(args + [str(bad)]) == 1
    assert main(args + [str(tmp_path / "missing")]) == 2


def test_cli_fit_prints_factors(tmp_path, capsys):
    window = write_window(tmp_path / "w01.json", [wjob("unit-001", 10, split="fit"), wjob("unit-002", 30, split="fit")])
    collected = write_collected(tmp_path / "c", [("unit-001", 0, 0, 5), ("unit-002", 0, 0, 15)])
    assert main(["fit", "--windows", str(window), "--collected", str(collected), "--compress", "1", "--factor", "unit=1"]) == 0
    assert capsys.readouterr().out.strip() == "unit=2.0000"


def test_real_jobs_skip_null_duration(tmp_path):
    window = write_window(tmp_path / "w01.json", [wjob("unit-001", 10, wait=2), {**wjob("unit-002", 1), "duration_s": None}])
    assert real_jobs([window]) == {"unit-001": {"lane": "unit", "split": "holdout", "wait": 2, "duration": 10, "wall": 12}}


def test_noise_floor_cv_of_per_run_medians():
    from trials.validate.stats import noise_floor

    def run(v):
        return [({"lane": "unit"}, {"wall": v, "wait": 1.0}), ({"lane": "unit"}, {"wall": v, "wait": 1.0})]

    rows = noise_floor([run(90.0), run(110.0)])
    wall = next(r for r in rows if r["metric"] == "wall")
    assert wall["mean_median"] == 100.0
    assert round(wall["cv"], 6) == round(14.142135623730951 / 100.0, 6)
    wait = next(r for r in rows if r["metric"] == "wait")
    assert wait["cv"] == 0.0


def test_noise_floor_needs_two_runs():
    from trials.validate.stats import noise_floor

    with pytest.raises(ValueError, match="two runs"):
        noise_floor([[]])
