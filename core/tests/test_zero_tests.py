from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.zero_tests import count_key

REPO = Path(__file__).resolve().parents[2]

CORE = "sim/dotnet/tests/Core.Tests/Core.Tests.csproj"
UTILS = "sim/dotnet/tests/Utils.Tests/Utils.Tests.csproj"


def run(tmp_path: Path, projects: list[str], counts: dict[str, str]):
    listing = tmp_path / "test.txt"
    listing.write_bytes("".join(f"{p}\n" for p in projects).encode())
    directory = tmp_path / "counts"
    directory.mkdir(exist_ok=True)
    for key, text in counts.items():
        (directory / f"{key}.count").write_bytes(text.encode())
    return subprocess.run(
        [sys.executable, "-m", "core.zero_tests", "--list", str(listing), "--counts", str(directory)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_every_project_with_tests_passes_and_prints_its_total(tmp_path):
    result = run(tmp_path, [CORE, UTILS], {count_key(CORE): "3\n", count_key(UTILS): "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"total=3 {CORE}" in result.stdout
    assert f"total=1 {UTILS}" in result.stdout


def test_a_project_that_ran_zero_tests_is_red(tmp_path):
    result = run(tmp_path, [CORE, UTILS], {count_key(CORE): "3", count_key(UTILS): "0"})
    assert result.returncode == 1
    assert f"{UTILS} ran 0 tests" in result.stdout


def test_a_project_with_no_count_is_red(tmp_path):
    result = run(tmp_path, [CORE, UTILS], {count_key(CORE): "3"})
    assert result.returncode == 1
    assert f"no test count for {UTILS}" in result.stdout


@pytest.mark.parametrize("text", ["", "abc", "-1", "3.0", "1 2", "٣"])
def test_a_count_that_is_not_a_whole_number_is_missing(tmp_path, text):
    result = run(tmp_path, [CORE], {count_key(CORE): text})
    assert result.returncode == 1
    assert f"no test count for {CORE}" in result.stdout


def test_an_empty_list_passes_and_says_so(tmp_path):
    result = run(tmp_path, [], {})
    assert result.returncode == 0
    assert result.stdout.strip() == "0 projects selected"


def test_blank_lines_alone_are_an_empty_list(tmp_path):
    result = run(tmp_path, ["", "  "], {})
    assert result.returncode == 0
    assert result.stdout.strip() == "0 projects selected"


def test_two_projects_sharing_a_count_key_are_red(tmp_path):
    other = "sim/dotnet/tests/Core.Tests/Core_Tests.csproj"
    assert count_key(other) != count_key(CORE)
    clash = "sim_dotnet/tests/Core.Tests/Core.Tests.csproj"
    assert count_key(clash) == count_key(CORE)
    result = run(tmp_path, [CORE, clash], {count_key(CORE): "3"})
    assert result.returncode == 1
    assert "share the count key" in result.stdout


def test_a_repeated_line_is_one_project(tmp_path):
    result = run(tmp_path, [CORE, CORE], {count_key(CORE): "2"})
    assert result.returncode == 0, result.stdout
    assert result.stdout.count(f"total=2 {CORE}") == 1


def test_a_missing_list_is_a_usage_error_and_never_green(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "core.zero_tests", "--list", str(tmp_path / "absent.txt"), "--counts", str(tmp_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2


def test_the_key_keeps_only_safe_bytes():
    assert count_key("sim/dotnet/tests/A.Tests/A.Tests.csproj") == "sim_dotnet_tests_A.Tests_A.Tests.csproj"
    assert count_key("a b/é") == "a_b___"
