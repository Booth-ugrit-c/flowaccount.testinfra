from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.sim_gate import plan

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = "sim/dotnet/src/"
TESTS = "sim/dotnet/tests/"
CORE = SRC + "Core/Core.csproj"
DATA = SRC + "Data/Data.csproj"
UTILS = SRC + "Utils/Utils.csproj"
OTHER = SRC + "Other/Other.csproj"
SERVICE = SRC + "Service/Service.csproj"
API = SRC + "Api/Api.csproj"
SUPPORT = TESTS + "TestSupport/TestSupport.csproj"
CORE_TESTS = TESTS + "Core.Tests/Core.Tests.csproj"
OTHER_TESTS = TESTS + "Other.Tests/Other.Tests.csproj"
SERVICE_TESTS = TESTS + "Service.Tests/Service.Tests.csproj"
API_INTEGRATION = TESTS + "Api.IntegrationTests/Api.IntegrationTests.csproj"


def run(tmp_path, affected, lane, emit):
    path = tmp_path / "affected.json"
    path.write_text(json.dumps(affected), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "tools.sim_gate.plan", "--affected", str(path), "--lane", lane, "--emit", emit],
        capture_output=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


def lines(proc):
    assert proc.returncode == 0, proc.stderr
    assert b"\r" not in proc.stdout
    return [line for line in proc.stdout.decode().split("\n") if line]


def test_one_unit_test_project_brings_its_whole_dependency_closure_in_order(tmp_path):
    proc = run(tmp_path, {"build": [], "test": [CORE_TESTS]}, "unit", "build")
    assert lines(proc) == [CORE, SUPPORT, CORE_TESTS]


def test_unaffected_dependencies_are_in_the_plan(tmp_path):
    proc = run(tmp_path, {"build": [], "test": [SERVICE_TESTS]}, "unit", "build")
    plan_ = lines(proc)
    assert set(plan_) == {CORE, DATA, UTILS, SERVICE, SUPPORT, SERVICE_TESTS}


def test_every_project_follows_all_of_its_references(tmp_path):
    proc = run(tmp_path, {"build": [], "test": [SERVICE_TESTS, CORE_TESTS, OTHER_TESTS]}, "unit", "build")
    plan_ = lines(proc)
    position = {path: index for index, path in enumerate(plan_)}
    assert len(position) == len(plan_)
    for dependent, dependency in [
        (DATA, CORE), (SERVICE, CORE), (SERVICE, DATA), (SERVICE, UTILS), (SERVICE_TESTS, SERVICE),
        (SERVICE_TESTS, SUPPORT), (CORE_TESTS, CORE), (OTHER_TESTS, OTHER),
    ]:
        assert position[dependency] < position[dependent], (dependency, dependent)


def test_the_integration_lane_plans_only_the_integration_project_and_its_closure(tmp_path):
    affected = {"build": [], "test": [API_INTEGRATION, CORE_TESTS, SERVICE_TESTS]}
    proc = run(tmp_path, affected, "integration", "build")
    assert set(lines(proc)) == {CORE, DATA, UTILS, SERVICE, API, SUPPORT, API_INTEGRATION}


def test_the_unit_lane_never_plans_the_integration_project(tmp_path):
    affected = {"build": [], "test": [API_INTEGRATION, CORE_TESTS]}
    assert API_INTEGRATION not in lines(run(tmp_path, affected, "unit", "build"))
    assert lines(run(tmp_path, affected, "unit", "test")) == [CORE_TESTS]


def test_emit_test_lists_only_the_lanes_test_projects_in_the_given_order(tmp_path):
    affected = {"build": [], "test": [API_INTEGRATION, CORE_TESTS, OTHER_TESTS]}
    assert lines(run(tmp_path, affected, "integration", "test")) == [API_INTEGRATION]
    assert lines(run(tmp_path, affected, "unit", "test")) == [CORE_TESTS, OTHER_TESTS]


def test_an_empty_selection_gives_an_empty_plan(tmp_path):
    assert lines(run(tmp_path, {"build": [], "test": []}, "unit", "build")) == []
    assert lines(run(tmp_path, {"build": [], "test": []}, "unit", "test")) == []


def test_a_selected_project_that_is_not_in_the_graph_is_refused(tmp_path):
    proc = run(tmp_path, {"build": [], "test": ["sim/dotnet/tests/Ghost/Ghost.csproj"]}, "unit", "build")
    assert proc.returncode == 2
    assert b"Ghost" in proc.stderr


@pytest.mark.parametrize("lane", ["", "other", "UNIT"])
def test_an_unknown_lane_is_refused(tmp_path, lane):
    assert run(tmp_path, {"build": [], "test": [CORE_TESTS]}, lane, "build").returncode != 0


def test_a_missing_or_malformed_affected_file_is_refused(tmp_path):
    missing = subprocess.run(
        [sys.executable, "-m", "tools.sim_gate.plan", "--affected", str(tmp_path / "none.json"), "--lane", "unit", "--emit", "build"],
        capture_output=True, cwd=REPO_ROOT, timeout=60,
    )
    assert missing.returncode == 2
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    broken = subprocess.run(
        [sys.executable, "-m", "tools.sim_gate.plan", "--affected", str(bad), "--lane", "unit", "--emit", "build"],
        capture_output=True, cwd=REPO_ROOT, timeout=60,
    )
    assert broken.returncode == 2


def test_the_graph_comes_from_the_working_tree_not_from_a_stale_selection(tmp_path):
    assert plan.lane_tests([API_INTEGRATION, CORE_TESTS], "integration") == [API_INTEGRATION]
    assert plan.lane_tests([API_INTEGRATION, CORE_TESTS], "unit") == [CORE_TESTS]


def test_the_unit_lane_also_plans_an_affected_project_that_no_test_depends_on(tmp_path):
    proc = run(tmp_path, {"build": [OTHER, API], "test": []}, "unit", "build")
    assert set(lines(proc)) == {OTHER, API, SERVICE, CORE, DATA, UTILS}


def test_the_unit_lane_plans_affected_projects_with_their_dependencies_in_order(tmp_path):
    plan_ = lines(run(tmp_path, {"build": [API], "test": [CORE_TESTS]}, "unit", "build"))
    position = {path: index for index, path in enumerate(plan_)}
    assert {API, SERVICE, CORE, DATA, UTILS, SUPPORT, CORE_TESTS} == set(plan_)
    assert position[SERVICE] < position[API] and position[CORE] < position[SERVICE]


def test_the_unit_lane_never_plans_the_integration_project_from_the_build_list(tmp_path):
    plan_ = lines(run(tmp_path, {"build": [API_INTEGRATION, CORE], "test": []}, "unit", "build"))
    assert plan_ == [CORE]


def test_the_integration_lane_stays_seeded_by_its_tests_only(tmp_path):
    assert lines(run(tmp_path, {"build": [OTHER, API], "test": []}, "integration", "build")) == []
    plan_ = lines(run(tmp_path, {"build": [OTHER], "test": [API_INTEGRATION]}, "integration", "build"))
    assert OTHER not in plan_ and API_INTEGRATION in plan_


def test_an_unknown_project_in_the_build_list_is_refused(tmp_path):
    proc = run(tmp_path, {"build": ["sim/dotnet/src/Ghost/Ghost.csproj"], "test": []}, "unit", "build")
    assert proc.returncode == 2 and b"Ghost" in proc.stderr
