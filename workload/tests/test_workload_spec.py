import json

import pytest

from workload import spec as spec_mod
from workload.tests.helpers import spec


def parse(**overrides):
    return spec_mod.parse_spec(spec(**overrides))


def test_valid_spec_parses_to_typed_values():
    job = parse(id="unit-017", cpu_seconds=120, threads=4, memory_mib=1024, services=["sql", "cache"])
    assert (job.id, job.cpu_seconds, job.threads, job.memory_mib, job.services) == (
        "unit-017", 120.0, 4, 1024, ("sql", "cache"))


def test_label_is_ignored_and_optional_keys_default():
    job = spec_mod.parse_spec({"id": "unit-001", "lane": "unit", "cpu_seconds": 1, "threads": 1, "memory_mib": 0})
    assert job.services == ()


@pytest.mark.parametrize("good", ["unit-017", "unit-000", "integration-1000", "e-123"])
def test_id_accepts(good):
    assert parse(id=good).id == good


@pytest.mark.parametrize("bad", ["unit-17", "Unit-017", "unit017", "unit-017x", "unit_017", "-017", "unit-", "unit-017\n", "", 17, None])
def test_id_rejects(bad):
    with pytest.raises(spec_mod.SpecError):
        parse(id=bad)


@pytest.mark.parametrize("good", [0.001, 1, 86400])
def test_cpu_seconds_accepts(good):
    assert parse(cpu_seconds=good).cpu_seconds == float(good)


@pytest.mark.parametrize("bad", [0, -1, 86400.1, float("nan"), float("inf"), "5", True, None])
def test_cpu_seconds_rejects(bad):
    with pytest.raises(spec_mod.SpecError):
        parse(cpu_seconds=bad)


@pytest.mark.parametrize("good", [1, 256])
def test_threads_accepts(good):
    assert parse(threads=good).threads == good


@pytest.mark.parametrize("bad", [0, -1, 257, 1.5, 2.0, "2", True, None])
def test_threads_rejects(bad):
    with pytest.raises(spec_mod.SpecError):
        parse(threads=bad)


@pytest.mark.parametrize("good", [0, 1, 1048576])
def test_memory_accepts(good):
    assert parse(memory_mib=good).memory_mib == good


@pytest.mark.parametrize("bad", [-1, 1048577, 1.5, "64", True, None])
def test_memory_rejects(bad):
    with pytest.raises(spec_mod.SpecError):
        parse(memory_mib=bad)


@pytest.mark.parametrize("good", [[], ["sql"], ["sql", "document", "cache", "search", "queue"]])
def test_services_accepts(good):
    assert list(parse(services=good).services) == good


@pytest.mark.parametrize("bad", [["mysql"], ["sql", "sql"], ["SQL"], "sql", [1], None])
def test_services_rejects(bad):
    with pytest.raises(spec_mod.SpecError):
        parse(services=bad)


@pytest.mark.parametrize("bad", ["Unit", "1unit", "", 5, None])
def test_lane_rejects(bad):
    with pytest.raises(spec_mod.SpecError):
        parse(lane=bad)


def test_unknown_key_is_an_error():
    with pytest.raises(spec_mod.SpecError, match="unknown keys: extra"):
        parse(extra=1)


@pytest.mark.parametrize("missing", ["id", "lane", "cpu_seconds", "threads", "memory_mib"])
def test_missing_required_key_is_an_error(missing):
    data = spec()
    del data[missing]
    with pytest.raises(spec_mod.SpecError, match="missing keys"):
        spec_mod.parse_spec(data)


@pytest.mark.parametrize("bad", [[], "x", 1, None])
def test_non_object_is_an_error(bad):
    with pytest.raises(spec_mod.SpecError):
        spec_mod.parse_spec(bad)


@pytest.mark.parametrize("text", ["{", "", '{"cpu_seconds": NaN}', '{"cpu_seconds": Infinity}'])
def test_json_text_failures_are_spec_errors(text):
    with pytest.raises(spec_mod.SpecError):
        spec_mod.parse_spec_json(text)


def test_check_specs_reports_each_bad_entry():
    text = json.dumps([spec(), spec(id="bad"), spec(threads=0)])
    problems = spec_mod.check_specs(text)
    assert len(problems) == 2
    assert problems[0].startswith("spec 1 (bad)")
    assert problems[1].startswith("spec 2 (unit-001)")


def test_check_specs_rejects_non_array_and_bad_json():
    assert spec_mod.check_specs("{}") == ["file must hold a JSON array of specs"]
    assert spec_mod.check_specs("[")[0].startswith("not valid JSON")
    assert spec_mod.check_specs("[]") == []
