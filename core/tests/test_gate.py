from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import gate

REPO_ROOT = Path(__file__).resolve().parents[2]

PASS = "pass"
TEST = "red [test]"
INFRA_TWICE = "red [infra failed 2x]"
INFRA_ONCE = "red [infra failed 1x]"
TRIPWIRE = "red [infra: cache tripwire]"
SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.mark.parametrize(
    "lanes, expected",
    [
        ([("unit", "pass")], PASS),
        ([("unit", "pass"), ("integration", "pass")], PASS),
        ([("unit", "test")], TEST),
        ([("unit", "infra")], INFRA_ONCE),
        ([("unit", "pass"), ("integration", "test")], TEST),
        ([("unit", "infra"), ("integration", "test")], TEST),
        ([("unit", "infra"), ("integration", "pass")], INFRA_ONCE),
        ([("build", "pass"), ("unit", "pass"), ("integration", "pass")], PASS),
        ([("build", "pass"), ("unit", "infra"), ("integration", "pass")], INFRA_ONCE),
        ([("build", "pass"), ("unit", "test"), ("integration", "infra")], TEST),
        ([("build", "test"), ("unit", ""), ("integration", "")], TEST),
        ([("build", "infra"), ("unit", ""), ("integration", "skipped")], INFRA_ONCE),
    ],
)
def test_overall_verdict(lanes, expected):
    assert gate.overall(lanes) == expected


@pytest.mark.parametrize("verdict", ["", "skipped", "failure", "PASS", "unknown"])
def test_a_lane_without_a_known_verdict_is_never_green(verdict):
    assert gate.overall([("unit", verdict)]) == TEST
    assert gate.overall([("build", "pass"), ("unit", verdict)]) == TEST


@pytest.mark.parametrize("verdict", ["", "skipped", "failure", "unknown"])
def test_a_build_without_a_known_verdict_is_never_green(verdict):
    assert gate.overall([("build", verdict), ("unit", "")]) == TEST


def test_no_lane_at_all_is_never_green():
    assert gate.overall([]) == TEST


def test_a_second_lane_that_never_reported_blocks_the_gate():
    assert gate.overall([("unit", "pass"), ("integration", "")]) == TEST


def test_lanes_after_a_failed_build_are_not_run():
    lanes = [("build", "infra"), ("unit", ""), ("integration", "skipped")]
    assert gate.verdicts(lanes) == [("build", "infra"), ("unit", "none"), ("integration", "none")]
    assert gate.verdicts([("build", "test"), ("unit", "")]) == [("build", "test"), ("unit", "none")]


def test_an_empty_lane_after_a_passing_build_is_red_test():
    lanes = [("build", "pass"), ("unit", ""), ("integration", "pass")]
    assert gate.verdicts(lanes)[1] == ("unit", "test")
    assert gate.overall(lanes) == TEST
    assert gate.retest_lanes(lanes) == []


def test_a_build_infra_failure_retests_every_lane():
    lanes = [("build", "infra"), ("unit", ""), ("integration", "")]
    assert gate.overall(lanes) == INFRA_ONCE
    assert gate.retest_lanes(lanes) == ["build", "unit", "integration"]


def test_a_build_test_failure_retests_nothing():
    assert gate.retest_lanes([("build", "test"), ("unit", ""), ("integration", "")]) == []


def test_an_infra_lane_next_to_a_test_lane_retests_nothing():
    lanes = [("build", "pass"), ("unit", "infra"), ("integration", "test")]
    assert gate.overall(lanes) == TEST
    assert gate.retest_lanes(lanes) == []


def test_only_the_infra_lane_is_retested_after_a_passing_build():
    assert gate.retest_lanes([("build", "pass"), ("unit", "infra"), ("integration", "pass")]) == ["unit"]


@pytest.mark.parametrize(
    "lanes, reasons",
    [
        ([("build", "pass"), ("unit", "infra"), ("integration", "pass")], {"unit": "cache tripwire"}),
        ([("build", "pass"), ("unit", "infra"), ("integration", "infra")], {"unit": "cache tripwire"}),
        ([("build", "infra"), ("unit", ""), ("integration", "")], {"build": "cache tripwire"}),
    ],
)
def test_a_tripped_cache_retests_nothing(lanes, reasons):
    assert gate.retest_lanes(lanes, reasons) == []


def test_a_named_cause_other_than_the_tripwire_is_still_retested():
    assert gate.retest_lanes([("build", "pass"), ("unit", "infra")], {"unit": "other cause"}) == ["unit"]


def test_a_retest_turns_infra_into_twice():
    assert gate.overall([("build", "pass"), ("unit", "infra")], retest=True) == INFRA_TWICE
    assert gate.overall([("build", "infra"), ("unit", "")], retest=True) == INFRA_TWICE
    assert gate.overall([("build", "pass"), ("unit", "pass")], retest=True) == PASS


def test_parse_lane_takes_one_verdict():
    assert gate.parse_lane("unit=pass") == ("unit", "pass")
    assert gate.parse_lane("unit=") == ("unit", "")


@pytest.mark.parametrize("raw", ["unit", "=pass", "unit=infra,pass", "unit=infra,", "bad name=pass", "unit=pass\n| injected", "unit=pass\n", "unit\n=pass"])
def test_parse_lane_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        gate.parse_lane(raw)


@pytest.mark.parametrize("raw", ["unit", "unit=-1", "unit=1x", "unit=" + "9" * 10, "unit=3\n"])
def test_parse_executed_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        gate.parse_executed(raw)


def test_parse_executed_reads_a_count_or_nothing():
    assert gate.parse_executed("unit=12") == ("unit", 12)
    assert gate.parse_executed("unit=") == ("unit", None)


def test_summary_is_a_markdown_table_with_one_row_per_lane_and_the_overall():
    text = gate.summary([("build", "pass"), ("unit", "infra"), ("integration", "test")], TEST, executed={"integration": 4})
    lines = text.splitlines()
    assert lines[0].startswith("## ")
    assert "| build | pass | - | pass |" in lines
    assert "| unit | infra | - | red [infra failed 1x] |" in lines
    assert "| integration | test | 4 | red [test] |" in lines
    assert f"Result: **{TEST}**" in text


def test_summary_marks_lanes_after_a_failed_build_as_not_run():
    text = gate.summary([("build", "test"), ("unit", "")], TEST)
    assert "| unit | none | - | not run |" in text.splitlines()


def test_summary_cannot_be_forged_through_a_lane_verdict():
    text = gate.summary([("unit", "pass|x")], PASS)
    assert "pass|x" not in text


REASON = {"unit": "cache tripwire"}
OTHER = {"unit": "other cause"}


@pytest.mark.parametrize(
    "lanes, reasons, expected",
    [
        ([("unit", "infra")], REASON, TRIPWIRE),
        ([("unit", "infra")], OTHER, "red [infra: other cause]"),
        ([("unit", "test")], OTHER, TEST),
        ([("unit", "test")], REASON, TEST),
        ([("unit", "pass")], REASON, TRIPWIRE),
        ([("unit", "pass")], OTHER, PASS),
        ([("unit", "infra"), ("integration", "test")], REASON, TEST),
        ([("unit", "infra")], {"unit": ""}, INFRA_ONCE),
        ([("unit", "infra")], {"other": "cache tripwire"}, INFRA_ONCE),
        ([("build", "infra"), ("unit", "")], {"build": "cache tripwire"}, TRIPWIRE),
    ],
)
def test_a_reason_on_an_infra_lane_names_the_infra_verdict(lanes, reasons, expected):
    assert gate.overall(lanes, reasons) == expected


@pytest.mark.parametrize("raw", ["unit", "=x", "unit=a|b", "unit=a\nb", "unit=" + "x" * 41, "bad name=x"])
def test_parse_reason_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        gate.parse_reason(raw)


def test_parse_reason_accepts_a_plain_phrase_and_an_empty_one():
    assert gate.parse_reason("unit=cache tripwire") == ("unit", "cache tripwire")
    assert gate.parse_reason("unit=") == ("unit", "")


def run_cli(args, tmp_path=None, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")}
    if tmp_path is not None:
        env["GITHUB_STEP_SUMMARY"] = str(tmp_path / "summary.md")
        env["GITHUB_OUTPUT"] = str(tmp_path / "output.txt")
    env.update(extra)
    return subprocess.run(
        [sys.executable, "-m", "core.gate", *args],
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
        (["--lane", "build=pass", "--lane", "unit=pass"], 0, "gate: pass"),
        (["--lane", "unit=test"], 1, "gate: red [test]"),
        (["--lane", "unit=infra"], 1, "gate: red [infra failed 1x]"),
        (["--lane", "unit=infra", "--retest"], 1, "gate: red [infra failed 2x]"),
        (["--lane", "build=infra", "--lane", "unit="], 1, "gate: red [infra failed 1x]"),
        (["--lane", "build=pass", "--lane", "unit="], 1, "gate: red [test]"),
    ],
)
def test_cli_exit_code_follows_the_verdict(tmp_path, args, code, line):
    out = run_cli(args, tmp_path)
    assert (out.returncode, out.stdout.strip()) == (code, line)
    assert line.removeprefix("gate: ") in (tmp_path / "summary.md").read_text()


RETEST_ARGS = ["--lane", "build=pass", "--lane", "unit=pass", "--retest", "--retest-of", "5"]


@pytest.mark.parametrize(
    "args, code, line",
    [
        (RETEST_ARGS + ["--prior-lane", "integration=pass"], 0, "gate: pass"),
        (RETEST_ARGS + ["--prior-lane", "integration=test"], 1, "gate: red [test]"),
        (RETEST_ARGS + ["--prior-lane", "integration=infra"], 1, "gate: red [test]"),
        (RETEST_ARGS + ["--prior-lane", "integration="], 1, "gate: red [test]"),
        (["--lane", "build=pass", "--lane", "unit=pass", "--prior-lane", "integration=pass"], 1, "gate: red [test]"),
        (["--lane", "build=pass", "--lane", "unit=infra", "--retest", "--retest-of", "5", "--prior-lane", "integration=pass"], 1, "gate: red [infra failed 2x]"),
    ],
)
def test_lanes_not_rerun_count_only_when_the_previous_run_passed_them(tmp_path, args, code, line):
    out = run_cli(args, tmp_path)
    assert (out.returncode, out.stdout.strip()) == (code, line)
    assert "| integration | previous run: " in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("extra", [["--prior-lane", "build=pass"], ["--prior-lane", "unit=pass"], ["--prior-lane", "integration=pass", "--prior-lane", "integration=pass"]])
def test_a_prior_lane_may_not_be_build_a_rerun_lane_or_given_twice(tmp_path, extra):
    out = run_cli(RETEST_ARGS + extra, tmp_path)
    assert out.returncode == 2
    assert "gate: pass" not in out.stdout


def test_cli_writes_retest_lanes_to_the_step_output(tmp_path):
    run_cli(["--lane", "build=infra", "--lane", "unit=", "--lane", "integration="], tmp_path)
    assert (tmp_path / "output.txt").read_text() == "retest_lanes=build,unit,integration\n"
    run_cli(["--lane", "build=pass", "--lane", "unit=pass"], tmp_path)
    assert (tmp_path / "output.txt").read_text().splitlines()[-1] == "retest_lanes="


def test_cli_without_a_summary_file_still_prints_the_verdict():
    out = run_cli(["--lane", "unit=pass"])
    assert (out.returncode, out.stdout.strip()) == (0, "gate: pass")


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--lane", "garbage"],
        ["--lane"],
        ["--lane", "unit=infra,pass"],
        ["--lane", "unit=pass", "--lane", "unit=pass"],
        ["--lane", "unit=pass", "--reason", "other=x"],
        ["--lane", "unit=pass", "--executed", "other=3"],
        ["--lane", "unit=pass", "--input", "secret=x"],
    ],
)
def test_cli_usage_error_is_nonzero_and_never_green(args):
    out = run_cli(args)
    assert out.returncode != 0
    assert "gate: pass" not in out.stdout


def test_cli_renders_the_tripwire_in_stdout_exit_code_and_summary(tmp_path):
    out = run_cli(["--lane", "unit=infra", "--reason", "unit=cache tripwire"], tmp_path)
    assert (out.returncode, out.stdout.strip()) == (1, f"gate: {TRIPWIRE}")
    assert TRIPWIRE in (tmp_path / "summary.md").read_text()


INPUTS = {
    "pr_number": "7",
    "pr_branch": "feature-a",
    "pr_sha": SHA_A,
    "pr_author": "someone",
    "base_sha": SHA_B,
    "cache": "off",
    "fault": "fail_test",
}


def verdict_args(tmp_path, lanes, retest_of="", retest=False, **overrides):
    inputs = dict(INPUTS, **overrides)
    args = [arg for lane in lanes for arg in ("--lane", lane)]
    args += [arg for key, value in inputs.items() for arg in ("--input", f"{key}={value}")]
    args += ["--stack", "dotnet", "--retest-of", retest_of, "--verdict-file", str(tmp_path / "out" / "verdict.json")]
    return args + (["--retest"] if retest else [])


RUN = {"GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "1"}


def test_the_verdict_file_carries_schema_one(tmp_path):
    args = verdict_args(tmp_path, ["build=pass", "unit=test", "integration=pass"])
    out = run_cli(args + ["--executed", "unit=3", "--executed", "integration=", "--reason", "build="], tmp_path, **RUN)
    assert out.stdout.strip() == "gate: red [test]"
    document = json.loads((tmp_path / "out" / "verdict.json").read_text(encoding="utf-8"))
    assert document == {
        "schema": 1,
        "stack": "dotnet",
        "run_id": "123456",
        "run_attempt": 1,
        "retest": False,
        "retest_of": "",
        "result": "red [test]",
        "retest_lanes": [],
        "inputs": INPUTS,
        "lanes": [
            {"name": "build", "verdict": "pass", "reason": "", "executed": None},
            {"name": "unit", "verdict": "test", "reason": "", "executed": 3},
            {"name": "integration", "verdict": "pass", "reason": "", "executed": None},
        ],
    }


def test_the_verdict_file_after_a_build_infra_failure_lists_every_lane_for_retest(tmp_path):
    out = run_cli(verdict_args(tmp_path, ["build=infra", "unit=", "integration="]), tmp_path, **RUN)
    assert out.stdout.strip() == "gate: red [infra failed 1x]"
    document = json.loads((tmp_path / "out" / "verdict.json").read_text(encoding="utf-8"))
    assert document["retest_lanes"] == ["build", "unit", "integration"]
    assert [lane["verdict"] for lane in document["lanes"]] == ["infra", "none", "none"]


def test_the_verdict_file_and_output_retest_nothing_after_a_tripwire(tmp_path):
    out = run_cli(verdict_args(tmp_path, ["build=infra", "unit=", "integration="]) + ["--reason", "build=cache tripwire"], tmp_path, **RUN)
    assert out.stdout.strip() == f"gate: {TRIPWIRE}"
    assert json.loads((tmp_path / "out" / "verdict.json").read_text(encoding="utf-8"))["retest_lanes"] == []
    assert (tmp_path / "output.txt").read_text() == "retest_lanes=\n"


def test_a_retest_run_is_recorded_as_one(tmp_path):
    out = run_cli(verdict_args(tmp_path, ["build=pass", "unit=infra"], retest_of="99", retest=True), tmp_path, **RUN)
    assert out.stdout.strip() == "gate: red [infra failed 2x]"
    document = json.loads((tmp_path / "out" / "verdict.json").read_text(encoding="utf-8"))
    assert (document["retest"], document["retest_of"], document["result"]) == (True, "99", INFRA_TWICE)


@pytest.mark.parametrize(
    "lanes, retest_of, retest, overrides, run",
    [
        (["unit=pass"], "", False, {"pr_sha": "main"}, RUN),
        (["unit=pass"], "", False, {"base_sha": SHA_A.upper()}, RUN),
        (["unit=pass"], "", False, {"pr_number": "07"}, RUN),
        (["unit=pass"], "", False, {"pr_branch": "-x"}, RUN),
        (["unit=pass"], "", False, {"pr_branch": "a..b"}, RUN),
        (["unit=pass"], "", False, {"pr_author": "a b"}, RUN),
        (["unit=pass"], "", False, {"cache": "maybe"}, RUN),
        (["unit=pass"], "", False, {"fault": "Fail"}, RUN),
        (["unit=pass"], "x1", True, {}, RUN),
        (["unit=pass"], "", True, {}, RUN),
        (["unit=pass"], "5", False, {}, RUN),
        (["unit=pass"], "", False, {}, {"GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "2"}),
        (["unit=pass"], "", False, {}, {"GITHUB_RUN_ID": "", "GITHUB_RUN_ATTEMPT": "1"}),
        (["unit=pass"], "", False, {}, {"GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "0"}),
    ],
)
def test_a_verdict_file_that_breaks_the_schema_is_refused_and_never_green(tmp_path, lanes, retest_of, retest, overrides, run):
    out = run_cli(verdict_args(tmp_path, lanes, retest_of, retest, **overrides), tmp_path, **run)
    assert out.returncode == 1
    assert out.stdout.strip().splitlines()[-1] == "gate: red [test]"
    assert "verdict.json refused" in out.stdout
    assert not (tmp_path / "out" / "verdict.json").exists()


def test_a_verdict_file_needs_every_input(tmp_path):
    args = verdict_args(tmp_path, ["unit=pass"])
    index = args.index("--input")
    out = run_cli(args[:index] + args[index + 2:], tmp_path, **RUN)
    assert (out.returncode, out.stdout.strip().splitlines()[-1]) == (1, "gate: red [test]")


def test_a_verdict_file_needs_a_stack(tmp_path):
    args = verdict_args(tmp_path, ["unit=pass"])
    index = args.index("--stack")
    args[index + 1] = ""
    out = run_cli(args, tmp_path, **RUN)
    assert (out.returncode, out.stdout.strip().splitlines()[-1]) == (1, "gate: red [test]")
