from __future__ import annotations

import json

import pytest

from datetime import datetime, timezone

from trials.calibration import cli
from trials.calibration.build import HOLDOUT_MIN_GATES, assign_splits
from trials.calibration.tests.helpers import FakeGh, client_for, config_dict, make_job, make_run


FAR = datetime(2099, 1, 1, tzinfo=timezone.utc)


def synthetic_gates(count: int, lanes=("unit",)) -> list[dict]:
    return [
        {"gate": f"g-{n:04d}", "created_at": "2026-01-10T00:00:00Z", "jobs": [{"lane": lane} for lane in lanes]}
        for n in range(count)
    ]


def test_ten_gate_lane_splits_exactly_seven_fit_three_holdout(tmp_path, monkeypatch, capsys):
    runs, jobs = [], {}
    for n in range(10):
        runs.append(make_run(100 + n, n * 1000))
        jobs[100 + n] = [make_job("unit / tests", "lab-runner-a", n * 1000, n * 1000 + 5, n * 1000 + 65)]
    cfg_path = tmp_path / "c.json"
    cfg_path.write_text(json.dumps(config_dict(workflows=["pipeline.yml"])), encoding="utf-8")
    client, _ = client_for(FakeGh(runs, jobs))
    monkeypatch.setattr(cli, "GhClient", lambda: client)
    assert cli.main(["extract", "--config", str(cfg_path), "--out", str(tmp_path / "out")]) == 0
    gates = json.loads((tmp_path / "out" / "today.json").read_text("utf-8"))["gates"]
    assert [g["split"] for g in gates] == ["fit"] * 7 + ["holdout"] * 3
    assert [j["split"] for g in gates for j in g["jobs"]] == ["fit"] * 7 + ["holdout"] * 3
    assert "WARNING: lane unit holdout has 3 gates (fewer than 20)" in capsys.readouterr().out


def test_split_is_chronological_even_when_input_is_shuffled(tmp_path, monkeypatch):
    order = [3, 0, 4, 1, 2]
    runs, jobs = [], {}
    for n in order:
        runs.append(make_run(100 + n, n * 1000))
        jobs[100 + n] = [make_job("unit / tests", "lab-runner-a", n * 1000, n * 1000 + 5, n * 1000 + 65)]
    cfg_path = tmp_path / "c.json"
    cfg_path.write_text(json.dumps(config_dict(workflows=["pipeline.yml"])), encoding="utf-8")
    client, _ = client_for(FakeGh(runs, jobs))
    monkeypatch.setattr(cli, "GhClient", lambda: client)
    cli.main(["extract", "--config", str(cfg_path), "--out", str(tmp_path / "out")])
    gates = json.loads((tmp_path / "out" / "today.json").read_text("utf-8"))["gates"]
    assert [g["arrival_offset_s"] for g in gates] == [0.0, 1000.0, 2000.0, 3000.0, 4000.0]
    assert [g["split"] for g in gates] == ["fit", "fit", "fit", "holdout", "holdout"]


@pytest.mark.parametrize("count,holdout,warns", [(63, 19, True), (64, 20, False), (100, 30, False), (1, 1, True)])
def test_holdout_warning_threshold(count, holdout, warns):
    gates = synthetic_gates(count)
    warnings = assign_splits(gates, ["unit"], FAR)
    assert sum(g["split"] == "holdout" for g in gates) == holdout
    assert bool(warnings) is warns
    if warns:
        assert warnings[0] == f"WARNING: lane unit holdout has {holdout} gates (fewer than {HOLDOUT_MIN_GATES})"


def test_lanes_split_independently_and_a_straddling_gate_is_mixed():
    gates = synthetic_gates(10, lanes=("integration",))
    for gate in gates[5:]:
        gate["jobs"].append({"lane": "unit"})
    assign_splits(gates, ["unit", "integration"], FAR)
    assert [g["splits"]["integration"] for g in gates] == ["fit"] * 7 + ["holdout"] * 3
    assert [g["splits"].get("unit") for g in gates[5:]] == ["fit", "fit", "fit", "holdout", "holdout"]
    assert [g["split"] for g in gates] == ["fit"] * 5 + ["fit", "fit", "mixed", "holdout", "holdout"]


def test_lane_without_gates_is_warned():
    assert assign_splits([], ["unit"], FAR) == ["WARNING: lane unit has no gates"]
