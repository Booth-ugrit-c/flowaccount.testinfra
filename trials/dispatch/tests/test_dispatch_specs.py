import json

import pytest

from trials.dispatch.specs import SpecError, build_specs, load_calibration, parse_lane_map, select_gates

LABELS = {"unit": "lane-a", "integration": "lane-b"}
FACTORS = {"unit": 0.5, "integration": 2.0}
THREADS = {"unit": 2, "integration": 4}
MEMORY = {"unit": 256, "integration": 2048}


def gate(jobs, name="g-0001", offset=0.0, split="fit"):
    return {"gate": name, "arrival_offset_s": offset, "split": split, "jobs": jobs}


def job(job_id, lane, duration, services=()):
    return {"id": job_id, "lane": lane, "duration_s": duration, "services": list(services)}


def test_spec_conversion_applies_factor_and_per_lane_values():
    specs, skipped = build_specs(
        gate([job("unit-001", "unit", 100.0), job("integration-001", "integration", 50.0, ["sql", "cache"])]),
        LABELS, FACTORS, THREADS, MEMORY,
    )
    assert skipped == []
    assert specs == [
        {"id": "unit-001", "lane": "unit", "cpu_seconds": 50.0, "threads": 2, "memory_mib": 256, "services": [], "label": "lane-a"},
        {"id": "integration-001", "lane": "integration", "cpu_seconds": 100.0, "threads": 4, "memory_mib": 2048, "services": ["sql", "cache"], "label": "lane-b"},
    ]
    assert list(specs[0]) == ["id", "lane", "cpu_seconds", "threads", "memory_mib", "services", "label"]


def test_null_duration_is_skipped_and_reported():
    specs, skipped = build_specs(gate([job("unit-001", "unit", None), job("unit-002", "unit", 4.0)]), LABELS, FACTORS, THREADS, MEMORY)
    assert [s["id"] for s in specs] == ["unit-002"]
    assert skipped == ["unit-001"]


def test_lane_missing_from_a_table_fails_loudly():
    with pytest.raises(SpecError, match="--memory"):
        build_specs(gate([job("unit-001", "unit", 1.0)]), LABELS, FACTORS, THREADS, {"integration": 1})


def test_lane_and_kind_label_wins_over_the_lane_label():
    labels = {"unit": "pool-unit", "unit:schedule": "pool-unit-sched"}
    pr = {**gate([job("unit-001", "unit", 1.0)]), "kind": "pr"}
    nightly = {**gate([job("unit-002", "unit", 1.0)]), "kind": "schedule"}
    assert build_specs(pr, labels, FACTORS, THREADS, MEMORY)[0][0]["label"] == "pool-unit"
    assert build_specs(nightly, labels, FACTORS, THREADS, MEMORY)[0][0]["label"] == "pool-unit-sched"


def test_kind_routing_refuses_a_gate_without_a_kind():
    with pytest.raises(SpecError, match="annotate-events"):
        build_specs(gate([job("unit-001", "unit", 1.0)]), {"unit": "a", "unit:pr": "b"}, FACTORS, THREADS, MEMORY)


def test_a_kind_with_no_matching_label_fails_loudly():
    with pytest.raises(SpecError, match="--labels"):
        build_specs({**gate([job("unit-001", "unit", 1.0)]), "kind": "pr"}, {"unit:schedule": "a"}, FACTORS, THREADS, MEMORY)


def test_bad_or_duplicate_ids_are_refused():
    with pytest.raises(SpecError, match="must match"):
        build_specs(gate([job("Unit_1", "unit", 1.0)]), LABELS, FACTORS, THREADS, MEMORY)
    with pytest.raises(SpecError, match="duplicate"):
        build_specs(gate([job("unit-001", "unit", 1.0), job("unit-001", "unit", 2.0)]), LABELS, FACTORS, THREADS, MEMORY)


def test_more_than_256_specs_is_refused():
    jobs = [job(f"unit-{n:03d}", "unit", 1.0) for n in range(257)]
    with pytest.raises(SpecError, match="256"):
        build_specs(gate(jobs), LABELS, FACTORS, THREADS, MEMORY)


def test_parse_lane_map():
    assert parse_lane_map("unit=0.5,integration=2", float, "--factor") == {"unit": 0.5, "integration": 2.0}
    with pytest.raises(SpecError):
        parse_lane_map("unit", float, "--factor")
    with pytest.raises(SpecError):
        parse_lane_map("unit=abc", float, "--factor")


def test_select_gates_filters_split_orders_and_limits():
    calibration = {
        "gates": [
            gate([], "g-0003", 30.0, "holdout"),
            gate([], "g-0001", 10.0, "fit"),
            gate([], "g-0002", 20.0, "fit"),
        ]
    }
    assert [g["gate"] for g in select_gates(calibration, "all", None)] == ["g-0001", "g-0002", "g-0003"]
    assert [g["gate"] for g in select_gates(calibration, "fit", None)] == ["g-0001", "g-0002"]
    assert [g["gate"] for g in select_gates(calibration, "all", 2)] == ["g-0001", "g-0002"]
    with pytest.raises(SpecError):
        select_gates({"gates": [gate([], "g-0001", 0.0, "fit")]}, "holdout", None)


def test_load_calibration_rejects_empty_and_bad_split(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"gates": []}), encoding="utf-8")
    with pytest.raises(SpecError):
        load_calibration(path)
    path.write_text(json.dumps({"gates": [gate([], split="train")]}), encoding="utf-8")
    with pytest.raises(SpecError, match="split"):
        load_calibration(path)
