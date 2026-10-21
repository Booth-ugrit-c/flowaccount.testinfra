from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.sim_gate import gate

REPO_ROOT = Path(__file__).resolve().parents[3]

PASS = "pass"
TEST = "red [test]"
INFRA_TWICE = "red [infra failed 2x]"
INFRA_ONCE = "red [infra failed 1x]"


@pytest.mark.parametrize(
    "lanes, expected",
    [
        ([("unit", "pass", "")], PASS),
        ([("unit", "pass", ""), ("integration", "pass", "")], PASS),
        ([("unit", "infra", "pass")], PASS),
        ([("unit", "test", "")], TEST),
        ([("unit", "infra", "test")], TEST),
        ([("unit", "infra", "infra")], INFRA_TWICE),
        ([("unit", "infra", "")], INFRA_ONCE),
        ([("unit", "infra", "skipped")], INFRA_ONCE),
        ([("unit", "pass", ""), ("integration", "test", "")], TEST),
        ([("unit", "infra", "infra"), ("integration", "test", "")], TEST),
        ([("unit", "infra", "infra"), ("integration", "pass", "")], INFRA_TWICE),
        ([("unit", "infra", ""), ("integration", "infra", "infra")], INFRA_TWICE),
        ([("unit", "infra", "pass"), ("integration", "infra", "pass")], PASS),
        ([("unit", "pass", "test")], PASS),
    ],
)
def test_overall_verdict(lanes, expected):
    assert gate.overall(lanes) == expected


@pytest.mark.parametrize("attempt", ["", "skipped", "failure", "PASS", "unknown"])
def test_an_attempt_without_a_known_verdict_is_never_green(attempt):
    assert gate.overall([("unit", attempt, "")]) == TEST
    assert gate.overall([("unit", attempt, "pass")]) == TEST


@pytest.mark.parametrize("retry", ["failure", "cancelled", "garbage"])
def test_a_retry_without_a_known_verdict_after_infra_is_never_green(retry):
    assert gate.overall([("unit", "infra", retry)]) == TEST


def test_no_lane_at_all_is_never_green():
    assert gate.overall([]) == TEST


def test_a_second_lane_that_never_reported_blocks_the_gate():
    assert gate.overall([("unit", "pass", ""), ("integration", "", "")]) == TEST


def test_parse_lane_accepts_both_shapes():
    assert gate.parse_lane("unit=infra,pass") == ("unit", "infra", "pass")
    assert gate.parse_lane("unit=pass") == ("unit", "pass", "")
    assert gate.parse_lane("unit=") == ("unit", "", "")
    assert gate.parse_lane("unit=infra,") == ("unit", "infra", "")


@pytest.mark.parametrize("raw", ["unit", "=pass", "unit=a,b,c", "bad name=pass", "unit=pass\n| injected"])
def test_parse_lane_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        gate.parse_lane(raw)


def test_summary_is_a_markdown_table_with_one_row_per_lane_and_the_overall():
    text = gate.summary([("unit", "infra", "pass"), ("integration", "test", "")], TEST)
    lines = text.splitlines()
    assert lines[0].startswith("## ")
    assert "| unit | infra | pass | pass |" in lines
    assert "| integration | test | none | red [test] |" in lines
    assert TEST in text


def test_summary_cannot_be_forged_through_a_lane_name_or_verdict():
    text = gate.summary([("unit", "pass|x", "")], PASS)
    assert "pass|x" not in text


def run_cli(args, summary_path=None):
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    if summary_path is not None:
        env["GITHUB_STEP_SUMMARY"] = str(summary_path)
    return subprocess.run(
        [sys.executable, "-m", "tools.sim_gate.gate", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=30,
    )


@pytest.mark.parametrize(
    "args, code, line",
    [
        (["--lane", "unit=pass", "--lane", "integration=pass"], 0, "gate: pass"),
        (["--lane", "unit=infra,pass"], 0, "gate: pass"),
        (["--lane", "unit=test"], 1, "gate: red [test]"),
        (["--lane", "unit=infra,infra"], 1, "gate: red [infra failed 2x]"),
        (["--lane", "unit=infra,test"], 1, "gate: red [test]"),
    ],
)
def test_cli_exit_code_follows_the_verdict(tmp_path, args, code, line):
    summary = tmp_path / "summary.md"
    out = run_cli(args, summary)
    assert (out.returncode, out.stdout.strip()) == (code, line)
    assert line.removeprefix("gate: ") in summary.read_text()


def test_cli_without_a_summary_file_still_prints_the_verdict():
    out = run_cli(["--lane", "unit=pass"])
    assert (out.returncode, out.stdout.strip()) == (0, "gate: pass")


def test_cli_usage_error_is_nonzero_and_never_green():
    for args in ([], ["--lane", "garbage"], ["--lane"]):
        out = run_cli(args)
        assert out.returncode != 0
        assert "gate: pass" not in out.stdout
