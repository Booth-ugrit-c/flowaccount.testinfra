from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "trx_counts.py"
NS = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"


def trx(counters: str, ns: str = NS, root: str = "TestRun", prolog: str = "") -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>{prolog}'
        f'<{root} xmlns="{ns}">'
        '<Results><UnitTestResult outcome="Passed" /></Results>'
        f'<ResultSummary outcome="Completed">{counters}</ResultSummary>'
        f"</{root}>"
    )


def run(tmp_path: Path, text: str):
    path = tmp_path / "result.trx"
    path.write_bytes(text.encode())
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(path)], capture_output=True, text=True, timeout=60
    )


def test_the_executed_count_is_read_not_the_total(tmp_path):
    result = run(tmp_path, trx('<Counters total="5" executed="3" passed="2" failed="1" notExecuted="0" />'))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "3\n"


def test_all_tests_skipped_is_an_executed_count_of_zero(tmp_path):
    result = run(tmp_path, trx('<Counters total="3" executed="0" passed="0" failed="0" notExecuted="0" />'))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0\n"


def test_without_executed_the_count_is_total_minus_not_executed(tmp_path):
    result = run(tmp_path, trx('<Counters total="5" notExecuted="2" />'))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "3\n"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not xml",
        trx('<Counters executed="3" />')[:-20],
        trx('<Counters total="3" />'),
        trx('<Counters notExecuted="0" />'),
        trx('<Counters passed="3" />'),
        trx('<Counters executed="" total="3" />'),
        trx('<Counters executed="-1" />'),
        trx('<Counters executed="3.0" />'),
        trx('<Counters executed=" 3" />'),
        trx('<Counters executed="٣" />'),
        trx('<Counters total="3" notExecuted="x" />'),
        trx('<Counters total="2" notExecuted="3" />'),
        trx('<Counters executed="3" /><Counters executed="4" />'),
        trx(""),
        trx('<Counters executed="3" />', ns="urn:other"),
        trx('<Counters executed="3" />', root="Other"),
    ],
    ids=[
        "empty",
        "not-xml",
        "truncated",
        "total-only",
        "not-executed-only",
        "no-counts",
        "blank-executed",
        "negative",
        "decimal",
        "padded",
        "non-ascii-digit",
        "bad-not-executed",
        "not-executed-above-total",
        "two-counters",
        "no-counters",
        "wrong-namespace",
        "wrong-root",
    ],
)
def test_a_malformed_file_gives_no_count(tmp_path, text):
    result = run(tmp_path, text)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no count" in result.stderr


def test_counters_outside_the_result_summary_are_ignored(tmp_path):
    text = trx('<Counters executed="3" />').replace("<Results>", '<Results><Counters executed="9" />')
    result = run(tmp_path, text)
    assert result.stdout == "3\n"


def test_an_external_entity_is_refused(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("7")
    prolog = f'<!DOCTYPE TestRun [<!ENTITY leak SYSTEM "{secret.as_uri()}">]>'
    text = trx('<Counters executed="3" />', prolog=prolog).replace("<Results>", "<Results>&leak;")
    result = run(tmp_path, text)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "DTD" in result.stderr


def test_an_internal_entity_expansion_is_refused(tmp_path):
    prolog = '<!DOCTYPE TestRun [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    text = trx('<Counters executed="3" />', prolog=prolog).replace("<Results>", "<Results>&b;")
    result = run(tmp_path, text)
    assert result.returncode == 1
    assert result.stdout == ""


def test_a_missing_file_gives_no_count(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(tmp_path / "absent.trx")], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 1
    assert result.stdout == ""


def test_wrong_usage_is_not_a_count():
    result = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert result.stdout == ""
