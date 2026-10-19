from __future__ import annotations

import copy
import json

import pytest

from trials.calibration import cli
from trials.calibration.tests.helpers import FakeGh, client_for, config_dict, make_job, make_run, stamp

CUTOFF = stamp(10000)
SPLIT_FIELDS = ("split", "splits")


def straddling_world(count: int = 14):
    runs, jobs = [], {}
    for n in range(count):
        runs.append(make_run(100 + n, n * 1000))
        jobs[100 + n] = [make_job("unit / tests", "lab-runner-a", n * 1000, n * 1000 + 5, n * 1000 + 65)]
    return runs, jobs


def extract(tmp_path, monkeypatch, cfg, name="out"):
    cfg_path = tmp_path / f"{name}.config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    runs, jobs = straddling_world()
    client, _ = client_for(FakeGh(runs, jobs))
    monkeypatch.setattr(cli, "GhClient", lambda: client)
    out = tmp_path / name
    assert cli.main(["extract", "--config", str(cfg_path), "--out", str(out)]) == 0
    return json.loads((out / "today.json").read_text("utf-8")), out, cfg_path


def test_gates_at_or_after_the_cutoff_are_excluded_and_the_split_uses_earlier_gates_only(tmp_path, monkeypatch, capsys):
    today, _, _ = extract(tmp_path, monkeypatch, config_dict(split_until=CUTOFF))
    gates = today["gates"]
    assert [g["split"] for g in gates] == ["fit"] * 7 + ["holdout"] * 3 + ["excluded"] * 4
    assert [j["split"] for g in gates for j in g["jobs"]] == ["fit"] * 7 + ["holdout"] * 3 + ["excluded"] * 4
    assert [g["splits"]["unit"] for g in gates[9:11]] == ["holdout", "excluded"]
    out = capsys.readouterr().out
    assert "fit: gates 7, jobs 7" in out and "holdout: gates 3, jobs 3" in out and "excluded: gates 4, jobs 4" in out


def test_excluded_gates_keep_their_flags_and_offsets(tmp_path, monkeypatch):
    today, _, _ = extract(tmp_path, monkeypatch, config_dict(split_until=CUTOFF, flip=stamp(12000)))
    assert [g["arrival_offset_s"] for g in today["gates"][-4:]] == [10000.0, 11000.0, 12000.0, 13000.0]
    assert [g["after_flip"] for g in today["gates"][-4:]] == [False, False, False, True]
    assert today["split_rule"]["until"] == CUTOFF


def test_cutoff_after_every_gate_leaves_the_plain_seventy_thirty_split(tmp_path, monkeypatch):
    today, _, _ = extract(tmp_path, monkeypatch, config_dict(split_until=stamp(10**6)))
    counts = {s: sum(g["split"] == s for g in today["gates"]) for s in ("fit", "holdout", "excluded")}
    assert counts == {"fit": 9, "holdout": 5, "excluded": 0}


def strip_split(today: dict) -> dict:
    copy_ = copy.deepcopy(today)
    copy_.pop("split_rule")
    for gate in copy_["gates"]:
        for field in SPLIT_FIELDS:
            gate.pop(field)
        for job in gate["jobs"]:
            job.pop("split")
    return copy_


def resplit(tmp_path, monkeypatch, cfg_path, source, name):
    monkeypatch.setattr(cli, "GhClient", lambda: pytest.fail("resplit must not call the API"))
    out = tmp_path / name
    code = cli.main(["resplit", "--config", str(cfg_path), "--in", str(source), "--out", str(out)])
    return code, out


def test_resplit_reapplies_the_cutoff_without_touching_any_other_field(tmp_path, monkeypatch, capsys):
    wide, wide_out, _ = extract(tmp_path, monkeypatch, config_dict(split_until=stamp(10**6)), "wide")
    cut_path = tmp_path / "cut.config.json"
    cut_path.write_text(json.dumps(config_dict(split_until=CUTOFF)), encoding="utf-8")
    capsys.readouterr()
    code, out = resplit(tmp_path, monkeypatch, cut_path, wide_out / "today.json", "cut")
    resplit_today = json.loads((out / "today.json").read_text("utf-8"))
    direct, _, _ = extract(tmp_path, monkeypatch, config_dict(split_until=CUTOFF), "direct")
    assert code == 0
    assert resplit_today == direct
    assert strip_split(resplit_today) == strip_split(wide)
    assert (out / "today.json").read_text("utf-8") == (tmp_path / "direct" / "today.json").read_text("utf-8")
    assert not (out / "id-map.json").exists()


def test_resplit_prints_the_per_lane_counts(tmp_path, monkeypatch, capsys):
    _, wide_out, _ = extract(tmp_path, monkeypatch, config_dict(split_until=stamp(10**6)), "wide")
    cut_path = tmp_path / "cut.config.json"
    cut_path.write_text(json.dumps(config_dict(split_until=CUTOFF)), encoding="utf-8")
    capsys.readouterr()
    resplit(tmp_path, monkeypatch, cut_path, wide_out / "today.json", "cut")
    out = capsys.readouterr().out
    assert "fit: gates 7, jobs 7" in out and "holdout: gates 3, jobs 3" in out and "excluded: gates 4, jobs 4" in out


def test_resplit_with_the_same_cutoff_is_byte_identical(tmp_path, monkeypatch):
    cfg = config_dict(split_until=CUTOFF)
    _, out, cfg_path = extract(tmp_path, monkeypatch, cfg, "first")
    code, again = resplit(tmp_path, monkeypatch, cfg_path, out / "today.json", "second")
    assert code == 0
    assert (again / "today.json").read_bytes() == (out / "today.json").read_bytes()


def test_resplit_can_overwrite_its_own_input(tmp_path, monkeypatch):
    _, out, cfg_path = extract(tmp_path, monkeypatch, config_dict(split_until=CUTOFF), "inplace")
    before = (out / "today.json").read_bytes()
    code = cli.main(["resplit", "--config", str(cfg_path), "--in", str(out / "today.json"), "--out", str(out)])
    assert code == 0 and (out / "today.json").read_bytes() == before


def test_resplit_rejects_a_lane_the_config_does_not_know(tmp_path, monkeypatch, capsys):
    _, out, cfg_path = extract(tmp_path, monkeypatch, config_dict(split_until=CUTOFF), "known")
    data = json.loads((out / "today.json").read_text("utf-8"))
    data["gates"][0]["jobs"][0]["lane"] = "mystery"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    code, target = resplit(tmp_path, monkeypatch, cfg_path, bad, "rejected")
    assert code == 5 and not target.exists()
    assert "mystery" in capsys.readouterr().err


def test_resplit_of_an_unreadable_file_fails_without_output(tmp_path, monkeypatch):
    cfg_path = tmp_path / "c.json"
    cfg_path.write_text(json.dumps(config_dict(split_until=CUTOFF)), encoding="utf-8")
    code, target = resplit(tmp_path, monkeypatch, cfg_path, tmp_path / "missing.json", "nothing")
    assert code != 0 and not target.exists()


def test_lane_with_every_gate_after_the_cutoff_is_warned_and_all_excluded(tmp_path, monkeypatch, capsys):
    today, _, _ = extract(tmp_path, monkeypatch, config_dict(split_until=stamp(0)))
    assert {g["split"] for g in today["gates"]} == {"excluded"}
    assert "WARNING: lane unit has no gates" in capsys.readouterr().out
