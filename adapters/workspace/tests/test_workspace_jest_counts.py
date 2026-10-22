from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "jest_counts.py"


def report(**fields) -> dict:
    base = {
        "numTotalTests": 0,
        "numPassedTests": 0,
        "numFailedTests": 0,
        "numPendingTests": 0,
        "numTodoTests": 0,
        "success": True,
        "testResults": [],
    }
    base.update(fields)
    return base


def run(path: Path, *extra: str):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(path), *extra],
        capture_output=True,
        text=True,
        timeout=60,
    )


def write(tmp_path: Path, content) -> Path:
    path = tmp_path / "jest.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(json.dumps(content), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"numTotalTests": 3, "numPassedTests": 3}, "3"),
        ({"numTotalTests": 5, "numPassedTests": 3, "numFailedTests": 2}, "5"),
        ({"numTotalTests": 7, "numPassedTests": 2, "numFailedTests": 1, "numPendingTests": 3, "numTodoTests": 1}, "3"),
    ],
)
def test_the_count_is_passed_plus_failed(tmp_path, fields, expected):
    result = run(write(tmp_path, report(**fields)))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_a_report_whose_tests_were_all_skipped_or_todo_counts_zero(tmp_path):
    result = run(write(tmp_path, report(numTotalTests=4, numPendingTests=3, numTodoTests=1)))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


@pytest.mark.parametrize(
    "content",
    [
        b'{"numPassedTests":',
        b"",
        b"\xff\xfe",
        b'{"numPassedTests": NaN, "numFailedTests": 0}',
        [],
        "3",
        report(numPassedTests=None),
        report(numPassedTests=True),
        report(numFailedTests=-1),
        report(numPassedTests=1.0),
        report(numPassedTests="1"),
        {"numPassedTests": 1},
        {"numFailedTests": 1},
    ],
)
def test_an_unusable_report_prints_no_count(tmp_path, content):
    result = run(write(tmp_path, content))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "jest_counts: no count" in result.stderr


def test_a_missing_file_prints_no_count(tmp_path):
    result = run(tmp_path / "absent.json")
    assert result.returncode == 1
    assert result.stdout == ""


def test_wrong_arguments_are_a_usage_error(tmp_path):
    result = run(write(tmp_path, report(numPassedTests=1)), "extra")
    assert result.returncode == 2
    assert result.stdout == ""
