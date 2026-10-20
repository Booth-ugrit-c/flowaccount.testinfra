from __future__ import annotations

import json

import pytest

from trials.calibration import cli
from trials.calibration.tests.helpers import FakeGh, client_for, config_dict, make_run


def write(tmp_path, gates, id_map):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config_dict()))
    today = tmp_path / "today.json"
    today.write_text(json.dumps({"gates": gates}))
    ids = tmp_path / "id-map.json"
    ids.write_text(json.dumps({"gates": id_map, "jobs": {}}))
    return cfg, today, ids


def gate(name):
    return {"gate": name, "arrival_offset_s": 0.0, "split": "fit", "jobs": [{"id": "unit-001", "lane": "unit"}]}


def test_default_branch_and_scheduled_runs_are_schedule_and_feature_branches_are_pr(tmp_path):
    runs = [
        make_run(101, 0, event="workflow_dispatch"),
        make_run(102, 60, event="workflow_dispatch", head_branch="main"),
        make_run(103, 120, workflow="nightly.yml", event="schedule", head_branch="main"),
        make_run(104, 180, event="push"),
    ]
    cfg, today, ids = write(
        tmp_path,
        [gate("g-0001"), gate("g-0002"), gate("g-0003"), gate("g-0004")],
        {f"g-000{n}": {"run_id": 100 + n} for n in range(1, 5)},
    )
    client, _ = client_for(FakeGh(runs, {}))
    report = cli.annotate_events(cfg, today, ids, tmp_path / "out", client=client)
    out = json.loads((tmp_path / "out" / "today.json").read_text())
    assert [(g["event"], g["kind"]) for g in out["gates"]] == [
        ("workflow_dispatch", "pr"), ("workflow_dispatch", "schedule"), ("schedule", "schedule"), ("push", "pr"),
    ]
    assert 'kinds: {"pr": 2, "schedule": 2}' in report


def test_a_run_missing_from_the_listing_is_fetched_by_id(tmp_path):
    outside_window = -10 * 86400
    fake = FakeGh([make_run(101, 0), make_run(102, outside_window, event="workflow_dispatch")], {})
    cfg, today, ids = write(tmp_path, [gate("g-0001"), gate("g-0002")], {"g-0001": {"run_id": 101}, "g-0002": {"run_id": 102}})
    client, _ = client_for(fake)
    cli.annotate_events(cfg, today, ids, tmp_path / "out", client=client)
    out = json.loads((tmp_path / "out" / "today.json").read_text())
    assert [g["kind"] for g in out["gates"]] == ["pr", "pr"]
    assert out["gates"][1]["event"] == "workflow_dispatch"
    assert ["api", "repos/acme/widgets/actions/runs/102"] in fake.log


def test_a_gate_absent_from_the_id_map_fails_with_a_data_error(tmp_path):
    cfg, today, ids = write(tmp_path, [gate("g-0001")], {})
    client, _ = client_for(FakeGh([], {}))
    with pytest.raises(cli.DataError, match="g-0001"):
        cli.annotate_events(cfg, today, ids, tmp_path / "out", client=client)
