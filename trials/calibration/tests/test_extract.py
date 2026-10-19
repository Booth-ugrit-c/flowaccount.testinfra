from __future__ import annotations

import json

import pytest

from trials.calibration import cli, collect
from trials.calibration.tests.helpers import (
    FakeGh, client_for, config_dict, make_job, make_run, stamp, standard_world,
)


@pytest.fixture
def run_cli(tmp_path, monkeypatch):
    def run(cfg: dict, fake: FakeGh, capsys, **client_kwargs):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        client, slept = client_for(fake, **client_kwargs)
        monkeypatch.setattr(cli, "GhClient", lambda: client)
        out = tmp_path / "out"
        code = cli.main(["extract", "--config", str(cfg_path), "--out", str(out)])
        captured = capsys.readouterr()
        return code, out, captured, slept
    return run


def load(out):
    return json.loads((out / "today.json").read_text("utf-8")), json.loads((out / "id-map.json").read_text("utf-8"))


def test_groups_jobs_into_gates_with_opaque_ids_and_offsets(run_cli, capsys):
    runs, jobs = standard_world(3)
    code, out, _, _ = run_cli(config_dict(), FakeGh(runs, jobs), capsys)
    assert code == 0
    today, id_map = load(out)
    assert [g["gate"] for g in today["gates"]] == ["g-0001", "g-0002", "g-0003"]
    assert [g["arrival_offset_s"] for g in today["gates"]] == [0.0, 600.0, 1200.0]
    assert today["gates"][0]["created_at"] == stamp(60)
    first = today["gates"][0]["jobs"]
    assert [(j["id"], j["lane"]) for j in first] == [("integration-001", "integration"), ("unit-001", "unit")]
    unit = next(j for j in first if j["lane"] == "unit")
    assert (unit["duration_s"], unit["queue_wait_s"], unit["run_attempt"], unit["conclusion"]) == (100.0, 10.0, 1, "success")
    assert id_map["jobs"]["unit-001"]["name"] == "unit / tests"
    assert id_map["gates"]["g-0002"]["run_url"].endswith("/runs/101")
    assert all("acme" not in json.dumps(g) for g in today["gates"])


def test_each_run_attempt_is_its_own_gate(run_cli, capsys):
    runs = [make_run(7, 0)]
    jobs = {7: [
        make_job("unit / tests", "lab-runner-a", 10, 20, 80, conclusion="failure", attempt=1),
        make_job("unit / tests", "lab-runner-a", 500, 510, 560, attempt=2),
    ]}
    code, out, _, _ = run_cli(config_dict(), FakeGh(runs, jobs), capsys)
    today, _ = load(out)
    assert code == 0
    assert [(g["gate"], g["arrival_offset_s"], g["jobs"][0]["run_attempt"]) for g in today["gates"]] == [
        ("g-0001", 0.0, 1), ("g-0002", 490.0, 2)]


def test_services_mapped_or_lane_default(run_cli, capsys):
    runs, jobs = standard_world(1)
    jobs[100].append(make_job("integration / plain", "lab-runner-b", 70, 80, 90))
    _, out, _, _ = run_cli(config_dict(), FakeGh(runs, jobs), capsys)
    today, _ = load(out)
    sources = {(j["services_source"], tuple(j["services"])) for j in today["gates"][0]["jobs"]}
    assert sources == {("mapped", ("document",)), ("lane-default", ("sql", "cache")), ("lane-default", ())}


def test_null_duration_jobs_are_kept_and_counted(run_cli, capsys):
    runs = [make_run(5, 0)]
    jobs = {5: [
        make_job("unit / tests", "lab-runner-a", 0, 5, 65),
        make_job("integration / skipped", "lab-runner-b", 0, 0, 0, runner=None, conclusion="skipped"),
        make_job("integration / cancelled in queue", "lab-runner-b", 0, None, 30, runner=None, conclusion="cancelled"),
    ]}
    code, out, captured, _ = run_cli(config_dict(), FakeGh(runs, jobs), capsys)
    today, _ = load(out)
    flat = {j["lane"]: j for g in today["gates"] for j in g["jobs"] if j["conclusion"] != "cancelled"}
    cancelled = next(j for g in today["gates"] for j in g["jobs"] if j["conclusion"] == "cancelled")
    assert code == 0
    assert (cancelled["duration_s"], cancelled["queue_wait_s"]) == (None, None)
    assert flat["integration"]["duration_s"] is None and flat["unit"]["duration_s"] == 60.0
    assert "skipped or unfinished (null duration): 2" in captured.out


def test_manual_label_jobs_are_counted_and_not_a_lane(run_cli, capsys):
    runs, jobs = standard_world(1)
    jobs[100].append(make_job("integration / manual", "lab-runner-m", 70, 80, 90))
    code, out, captured, _ = run_cli(config_dict(), FakeGh(runs, jobs), capsys)
    today, _ = load(out)
    assert code == 0
    assert today["manual_label_jobs"] == 1
    assert sum(len(g["jobs"]) for g in today["gates"]) == 2
    assert "manual-label jobs (not a lane): 1" in captured.out


def test_after_flip_flag_and_count(run_cli, capsys):
    runs = [make_run(1, 0), make_run(2, 15 * 86400)]
    jobs = {
        1: [make_job("unit / tests", "lab-runner-a", 0, 5, 65)],
        2: [make_job("unit / tests", "lab-runner-a", 15 * 86400, 15 * 86400 + 5, 15 * 86400 + 65)],
    }
    _, out, captured, _ = run_cli(config_dict(), FakeGh(runs, jobs), capsys)
    today, _ = load(out)
    assert [g["after_flip"] for g in today["gates"]] == [False, True]
    assert "jobs after flip: 1" in captured.out


def test_match_on_runner_name_ignores_labels(run_cli, capsys):
    runs = [make_run(1, 0)]
    jobs = {1: [
        make_job("unit / by name", "some-label", 0, 5, 65, runner="lab-runner-a"),
        make_job("unit / by label only", "lab-runner-a", 0, 5, 65, runner="cloud-1"),
    ]}
    code, out, captured, _ = run_cli(config_dict(match_field="runner_name"), FakeGh(runs, jobs), capsys)
    today, id_map = load(out)
    assert code == 0
    assert [j["id"] for j in today["gates"][0]["jobs"]] == ["unit-001"]
    assert id_map["jobs"]["unit-001"]["name"] == "unit / by name"


def test_label_match_that_ran_elsewhere_is_reported(run_cli, capsys):
    runs = [make_run(1, 0)]
    jobs = {1: [make_job("unit / tests", "lab-runner-a", 0, 5, 65, runner="cloud-1")]}
    _, _, captured, _ = run_cli(config_dict(), FakeGh(runs, jobs), capsys)
    assert "matched by label but ran on another runner: 1" in captured.out


def test_unmapped_job_name_fails_loudly(run_cli, capsys):
    runs = [make_run(1, 0)]
    jobs = {1: [make_job("lint / style", "lab-runner-a", 0, 5, 65)]}
    code, out, captured, _ = run_cli(config_dict(), FakeGh(runs, jobs), capsys)
    assert code == 5 and not out.exists()
    assert "matches no lane pattern" in captured.err


def test_run_listing_is_split_when_search_cap_is_hit(run_cli, capsys, monkeypatch):
    monkeypatch.setattr(collect, "RUN_SEARCH_CAP", 3)
    runs = [make_run(100 + n, n * 3600) for n in range(8)]
    jobs = {r["id"]: [make_job("unit / tests", "lab-runner-a", r["id"], r["id"] + 1, r["id"] + 9)] for r in runs}
    fake = FakeGh(runs, jobs, cap=3)
    code, out, captured, _ = run_cli(config_dict(workflows=["pipeline.yml"]), fake, capsys)
    today, _ = load(out)
    assert code == 0
    assert len(today["gates"]) == 8
    assert "scanned runs 8" in captured.out


def test_calls_are_read_only_gets_through_gh_api(run_cli, capsys):
    runs, jobs = standard_world(2)
    fake = FakeGh(runs, jobs)
    run_cli(config_dict(), fake, capsys)
    assert fake.log and all(call[0] == "api" for call in fake.log)
    assert all("--paginate" in c for c in fake.log if "per_page=100" in c[-1])


def test_summary_reports_median_and_iqr_per_split(run_cli, capsys):
    runs, jobs = [], {}
    for n, duration in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 100]):
        runs.append(make_run(100 + n, n * 100))
        jobs[100 + n] = [make_job("unit / tests", "lab-runner-a", n * 100, n * 100 + 4, n * 100 + 4 + duration)]
    code, _, captured, _ = run_cli(config_dict(workflows=["pipeline.yml"]), FakeGh(runs, jobs), capsys)
    assert code == 0
    assert "fit: gates 7, jobs 7" in captured.out and "holdout: gates 3, jobs 3" in captured.out
    assert "duration_s   median 40.0s IQR 25.0-55.0s n=7" in captured.out
    assert "queue_wait_s median 4.0s IQR 4.0-4.0s n=7" in captured.out
    assert "duration_s   median 90.0s IQR 85.0-95.0s n=3" in captured.out
