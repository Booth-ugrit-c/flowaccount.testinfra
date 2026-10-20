import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from trials.compare.cli import main, render
from trials.compare.stats import gate_metrics, idle_while_waiting, kinds_of, run_metrics, span, summarize, usage, waiting_set

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


def collect_dir(tmp_path, name, timings, gates=None):
    directory = tmp_path / name
    (directory / "gates").mkdir(parents=True)
    gates = gates or {"g-0001": [i for i in timings if i.startswith("unit")], "g-0002": [i for i in timings if i.startswith("integration")]}
    for gate, ids in gates.items():
        jobs = [
            {"id": i, "conclusion": "success", "created_at": stamp(timings[i][0]), "started_at": stamp(timings[i][1]), "completed_at": stamp(timings[i][2])}
            for i in ids
        ]
        if jobs:
            (directory / "gates" / f"{gate}.json").write_text(json.dumps({"gate_id": gate, "jobs": jobs}))
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
    assert m["pr/wall/p90"] == 119.0
    assert m["all/wall/p90"] == 336.0


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
    assert "| unit/wait/median | asis | 2 | 81.0 | 1.414 |" in out
    assert "| unit/wait/median | kube | 2 | 11.0 | 1.414 | 12.9% | -70.0 (-86.4%) | 3.960 | yes |" in out


def test_a_window_without_gate_kinds_is_refused(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"gates": [{"gate": "g-0001", "jobs": [{"id": "unit-001", "lane": "unit", "duration_s": 1.0}]}]}))
    with pytest.raises(ValueError, match="no kind"):
        kinds_of(path)


def samples_csv(path, inside):
    lines = ["epoch,cpu_busy,mem_used_mib,mem_total_mib"]
    for t in range(-10, 101):
        cpu, mem = (inside(t) if 0 <= t <= 90 else (1.0, 900))
        lines.append(f"{BASE.timestamp() + t},{cpu},{mem},1000")
    path.write_text("\n".join(lines) + "\n")


def test_usage_refuses_samples_that_miss_part_of_the_run(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text("epoch,cpu_busy,mem_used_mib,mem_total_mib\n" + "".join(f"{t},0.5,100,1000\n" for t in (2, 3)))
    with pytest.raises(ValueError, match="cover"):
        usage([path], 0.0, 10.0)


def test_usage_refuses_a_hole_inside_the_run(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text("epoch,cpu_busy,mem_used_mib,mem_total_mib\n" + "".join(f"{t},0.5,100,1000\n" for t in (0, 1, 2, 3, 8, 9, 10)))
    with pytest.raises(ValueError, match="cover"):
        usage([path], 0.0, 10.0)


HOST_TIMINGS = {"unit-001": (0, 5, 20), "unit-002": (10, 15, 40), "integration-001": (30, 35, 90)}


def host_run(tmp_path, name, hosts, cpu, timings=HOST_TIMINGS):
    directory = collect_dir(tmp_path, name, timings)
    for host in hosts:
        samples_csv(tmp_path / f"{name}-samples-{host}.csv", lambda t: (cpu, 300 if t == 10 else 100))
    return directory


def test_cli_reports_host_usage_cut_to_each_run(tmp_path, capsys):
    w = window(tmp_path)
    argv = ["--window", str(w), "--compress", "4", "--baseline", "asis", "--json", str(tmp_path / "c.json")]
    for n in range(2):
        argv += ["--run", f"asis={host_run(tmp_path, f'a{n}', ['vm1', 'vm2'], 0.5)}"]
        argv += ["--run", f"kube={host_run(tmp_path, f'k{n}', ['node'], 0.25)}"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "| host/mem_peak_mib | asis | 2 | 600.0 | 0.000 | 0.0% |" in out
    assert "| host/mem_peak_mib | kube | 2 | 300.0 | 0.000 | 0.0% | -300.0 (-50.0%) | 0.000 | yes |" in out
    runs = json.loads((tmp_path / "c.json").read_text())["runs"]
    assert [r["metrics"]["host/cpu_busy_mean"] for r in runs] == [0.5, 0.25, 0.5, 0.25]


def test_cli_refuses_runs_with_different_host_counts(tmp_path, capsys):
    w = window(tmp_path)
    argv = ["--window", str(w), "--compress", "4", "--baseline", "asis",
            "--run", f"asis={host_run(tmp_path, 'a0', ['vm1', 'vm2'], 0.5)}",
            "--run", f"asis={host_run(tmp_path, 'a1', ['vm1'], 0.5)}"]
    assert main(argv) == 2
    assert "sample files" in capsys.readouterr().err


def test_cli_refuses_a_design_without_samples_when_others_have_them(tmp_path, capsys):
    w = window(tmp_path)
    argv = ["--window", str(w), "--compress", "4", "--baseline", "asis",
            "--run", f"asis={host_run(tmp_path, 'a0', ['vm1'], 0.5)}",
            "--run", f"kube={collect_dir(tmp_path, 'k0', HOST_TIMINGS)}"]
    assert main(argv) == 2
    assert "sample files" in capsys.readouterr().err


def test_a_single_run_or_zero_baseline_prints_no_verdict():
    rows = summarize({"asis": [{"k": 0.0}, {"k": 0.0}], "kube": [{"k": 5.0}]}, "asis")
    assert next(r for r in rows if r["design"] == "kube")["detectable"] is None
    assert "| k | kube | 1 | 5.000 | n/a | n/a | 5.000 (n/a) | n/a | n/a |" in render(rows, "asis")


def test_span_runs_from_the_first_created_to_the_last_completed_job(tmp_path):
    assert span(collect_dir(tmp_path, "s", HOST_TIMINGS)) == (BASE.timestamp(), BASE.timestamp() + 90)


GATE_WINDOW = [
    ("g-0001", "pr", ["unit-001", "unit-002"]),
    ("g-0002", "pr", ["unit-003", "unit-004"]),
    ("g-0003", "pr", ["unit-005", "unit-006"]),
    ("g-0004", "schedule", ["integration-001", "integration-002"]),
    ("g-0005", "schedule", ["integration-003", "integration-004"]),
]

GATE_TIMINGS = {
    "unit-001": (0, 1, 10), "unit-002": (5, 6, 30),
    "unit-003": (100, 101, 110), "unit-004": (105, 106, 120),
    "unit-005": (200, 201, 230), "unit-006": (220, 221, 260),
    "integration-001": (300, 310, 360), "integration-002": (330, 335, 400),
    "integration-003": (500, 501, 520), "integration-004": (510, 512, 640),
}


def gate_window(tmp_path):
    path = tmp_path / "gw.json"
    gates = [
        {"gate": gate, "kind": kind, "jobs": [{"id": i, "lane": i.split("-")[0], "duration_s": 1.0} for i in ids]}
        for gate, kind, ids in GATE_WINDOW
    ]
    path.write_text(json.dumps({"gates": gates}))
    return path


def test_gate_wall_is_the_span_of_the_gate_not_of_any_job(tmp_path):
    jobs = kinds_of(gate_window(tmp_path))
    directory = collect_dir(tmp_path, "r", GATE_TIMINGS, {gate: ids for gate, _, ids in GATE_WINDOW})
    assert gate_metrics(directory, jobs, 2.0) == {
        "gate/all/wall/median": 120.0,
        "gate/all/wall/p90": 248.0,
        "gate/pr/wall/median": 60.0,
        "gate/pr/wall/p90": 108.0,
        "gate/schedule/wall/median": 240.0,
        "gate/schedule/wall/p90": 272.0,
    }


def test_a_collected_gate_with_no_window_gate_is_refused(tmp_path):
    jobs = kinds_of(window(tmp_path))
    timings = {"ghost-001": (0, 1, 5)}
    directory = collect_dir(tmp_path, "r", timings, {"g-0009": ["ghost-001"]})
    with pytest.raises(ValueError, match="0 window gates"):
        gate_metrics(directory, jobs, 1.0)


def test_a_collected_gate_spanning_two_window_gates_is_refused(tmp_path):
    jobs = kinds_of(window(tmp_path))
    directory = collect_dir(tmp_path, "r", HOST_TIMINGS, {"g-0001": list(HOST_TIMINGS)})
    with pytest.raises(ValueError, match="2 window gates"):
        gate_metrics(directory, jobs, 1.0)


WAIT_TIMINGS = {"unit-001": (0, 10, 40), "unit-002": (5, 15, 60), "integration-001": (20, 20, 50)}


def busy_before_15(early, late):
    return lambda t: (early if t < 15 else late, 100)


def test_idle_is_measured_only_while_work_waits(tmp_path):
    directory = collect_dir(tmp_path, "r", WAIT_TIMINGS)
    files = []
    for host, (early, late) in {"vm1": (1.0, 0.2), "vm2": (0.0, 0.6)}.items():
        path = tmp_path / f"r-samples-{host}.csv"
        samples_csv(path, busy_before_15(early, late))
        files.append(path)
    assert waiting_set(directory) == [(BASE.timestamp(), BASE.timestamp() + 15)]
    got = idle_while_waiting(files, waiting_set(directory), *span(directory), 4.0)
    assert got == {"idle_while_waiting": pytest.approx(0.5), "waiting_s": 60.0}
    assert usage(files, *span(directory))["cpu_busy_mean"] != pytest.approx(1 - 0.5)


def test_no_waiting_gives_zero_seconds_and_no_idle_value(tmp_path, capsys):
    now = {"unit-001": (0, 0, 20), "unit-002": (10, 10, 40), "integration-001": (30, 30, 90)}
    w = window(tmp_path)
    argv = ["--window", str(w), "--compress", "4", "--baseline", "asis"]
    for n in range(2):
        argv += ["--run", f"asis={host_run(tmp_path, f'a{n}', ['vm1'], 0.5, now)}"]
        argv += ["--run", f"kube={host_run(tmp_path, f'k{n}', ['node'], 0.25, now)}"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "| host/waiting_s | asis | 2 | 0.000 | 0.000 | n/a |" in out
    assert "| host/idle_while_waiting | asis | 2 | n/a | n/a | n/a |" in out
    assert "| host/idle_while_waiting | kube | 2 | n/a | n/a | n/a | n/a (n/a) | n/a | n/a |" in out
    assert "| host/waiting_s | kube | 2 | 0.000 | 0.000 | n/a | 0.000 (n/a) | 0.000 | no |" in out


def test_cli_prints_gate_wall_and_idle_while_waiting(tmp_path, capsys):
    w = window(tmp_path)
    argv = ["--window", str(w), "--compress", "4", "--baseline", "asis"]
    for design, hosts in {"asis": {"vm1": (1.0, 0.2), "vm2": (0.0, 0.6)}, "kube": {"node": (0.7, 0.7)}}.items():
        for n in range(2):
            name = f"{design[0]}{n}"
            directory = collect_dir(tmp_path, name, WAIT_TIMINGS)
            for host, (early, late) in hosts.items():
                samples_csv(tmp_path / f"{name}-samples-{host}.csv", busy_before_15(early, late))
            argv += ["--run", f"{design}={directory}"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "| gate/pr/wall/median | asis | 2 | 240.0 | 0.000 | 0.0% |" in out
    assert "| gate/schedule/wall/median | kube | 2 | 120.0 | 0.000 | 0.0% | 0.000 (+0.0%) | 0.000 | no |" in out
    assert "| gate/all/wall/median | asis | 2 | 180.0 | 0.000 | 0.0% |" in out
    assert "| gate/all/wall/p90 | asis | 2 | 228.0 | 0.000 | 0.0% |" in out
    assert "| host/waiting_s | asis | 2 | 60.0 | 0.000 | 0.0% |" in out
    assert "| host/idle_while_waiting | asis | 2 | 0.500 | 0.000 | 0.0% |" in out
    assert "| host/idle_while_waiting | kube | 2 | 0.300 | 0.000 | 0.0% | -0.200 (-40.0%) | 0.000 | yes |" in out


def test_fractions_print_with_three_decimals_and_json_keeps_full_precision(tmp_path, capsys):
    w = window(tmp_path)
    argv = ["--window", str(w), "--compress", "4", "--baseline", "asis", "--json", str(tmp_path / "c.json")]
    for n in range(2):
        argv += ["--run", f"asis={host_run(tmp_path, f'a{n}', ['vm1'], 0.12345)}"]
        argv += ["--run", f"kube={host_run(tmp_path, f'k{n}', ['node'], 0.12345)}"]
    assert main(argv) == 0
    assert "| host/cpu_busy_mean | asis | 2 | 0.123 | 0.000 |" in capsys.readouterr().out
    runs = json.loads((tmp_path / "c.json").read_text())["runs"]
    assert runs[0]["metrics"]["host/cpu_busy_mean"] == pytest.approx(0.12345)
