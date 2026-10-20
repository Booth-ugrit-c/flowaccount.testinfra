import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from trials.compare.cli import main
from trials.compare.stats import run_metrics, summarize, usage

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def stamp(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def window(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"gates": [
        {"gate": "g-0001", "kind": "pr", "jobs": [
            {"id": "unit-001", "lane": "unit", "duration_s": 1.0},
            {"id": "unit-002", "lane": "unit", "duration_s": 1.0},
        ]},
        {"gate": "g-0002", "kind": "schedule", "jobs": [{"id": "integration-001", "lane": "integration", "duration_s": 1.0}]},
    ]}))
    return path


def collect_dir(tmp_path, name, timings):
    directory = tmp_path / name
    (directory / "gates").mkdir(parents=True)
    jobs = [
        {"id": job_id, "conclusion": "success", "created_at": stamp(c), "started_at": stamp(s), "completed_at": stamp(e)}
        for job_id, (c, s, e) in timings.items()
    ]
    (directory / "gates" / "g-0001.json").write_text(json.dumps({"jobs": jobs}))
    return directory


JOBS = {
    "unit-001": {"lane": "unit", "kind": "pr"},
    "unit-002": {"lane": "unit", "kind": "pr"},
    "integration-001": {"lane": "integration", "kind": "schedule"},
}


def test_run_metrics_groups_by_lane_kind_and_all():
    replay = {
        "unit-001": {"wait": 10.0, "wall": 110.0},
        "unit-002": {"wait": 20.0, "wall": 120.0},
        "integration-001": {"wait": 90.0, "wall": 390.0},
    }
    m = run_metrics(JOBS, replay)
    assert m["unit/wait/median"] == 15.0
    assert m["all/wait/median"] == 20.0
    assert m["schedule/wait/median"] == 90.0
    assert m["pr/wall/median"] == 115.0
    assert m["integration/wall/p90"] == 390.0


def test_run_metrics_refuses_a_missing_job():
    with pytest.raises(ValueError, match="no replay record"):
        run_metrics(JOBS, {"unit-001": {"wait": 1.0, "wall": 2.0}})


def test_summarize_reports_delta_and_minimum_detectable_effect():
    rows = summarize({"asis": [{"k": 100.0}, {"k": 110.0}], "kube": [{"k": 50.0}, {"k": 60.0}]}, "asis")
    kube = next(r for r in rows if r["design"] == "kube")
    assert kube["delta"] == -50.0
    assert kube["delta_pct"] == pytest.approx(-50 / 105)
    assert kube["mde"] == pytest.approx(2.8 * math.sqrt(50.0 / 2 + 50.0 / 2))
    assert kube["detectable"] is True


def test_a_difference_inside_the_noise_is_not_detectable():
    rows = summarize({"asis": [{"k": 100.0}, {"k": 140.0}], "kube": [{"k": 110.0}, {"k": 150.0}]}, "asis")
    assert next(r for r in rows if r["design"] == "kube")["detectable"] is False


def test_usage_keeps_only_samples_inside_the_run(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text("epoch,cpu_busy,mem_used_mib,mem_total_mib\n" + "".join(f"{t},{t / 10},{100 * t},1000\n" for t in range(11)))
    assert usage([path], 2.0, 4.0) == {"cpu_busy_mean": pytest.approx(0.3), "mem_peak_mib": 400.0}


def test_cli_compares_designs_from_collect_dirs(tmp_path, capsys):
    w = window(tmp_path)
    asis = [collect_dir(tmp_path, f"a{n}", {"unit-001": (0, 10 + n, 20 + n), "unit-002": (0, 30, 40), "integration-001": (0, 50, 90)}) for n in range(2)]
    kube = [collect_dir(tmp_path, f"k{n}", {"unit-001": (0, 2 + n, 12 + n), "unit-002": (0, 3, 13), "integration-001": (0, 4, 44)}) for n in range(2)]
    argv = ["--window", str(w), "--compress", "4", "--baseline", "asis"]
    for d in asis:
        argv += ["--run", f"asis={d}"]
    for d in kube:
        argv += ["--run", f"kube={d}"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "| unit/wait/median | asis | 2 | 81.0 | 1.4 |" in out
    assert "| unit/wait/median | kube | 2 | 11.0 | 1.4 | 12.9% | -70.0 (-86.4%) | 4.0 | yes |" in out
