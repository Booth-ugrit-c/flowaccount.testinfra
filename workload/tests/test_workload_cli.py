import json

import pytest

from workload import cli, runner
from workload.tests.helpers import result_of, run_cli, spec


def write(tmp_path, data):
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_check_exits_zero_on_valid_file(tmp_path):
    proc = run_cli("check", write(tmp_path, [spec(), spec(id="integration-004", services=["sql"])]))
    assert proc.returncode == 0
    assert "all specs valid" in proc.stdout


def test_check_exits_two_when_one_spec_is_invalid(tmp_path):
    proc = run_cli("check", write(tmp_path, [spec(), spec(id="unit-2")]))
    assert proc.returncode == 2
    assert "spec 1 (unit-2)" in proc.stdout


def test_check_exits_two_on_unknown_key_missing_file_and_non_array(tmp_path):
    assert run_cli("check", write(tmp_path, [spec(extra=1)])).returncode == 2
    assert run_cli("check", str(tmp_path / "absent.json")).returncode == 2
    assert run_cli("check", write(tmp_path, spec())).returncode == 2


def test_check_in_process_matches_exit_code(tmp_path, capsys):
    assert cli.main(["check", write(tmp_path, [spec(threads=0)])]) == 2
    assert cli.main(["check", write(tmp_path, [spec()])]) == 0


def test_run_with_invalid_spec_exits_two_and_still_prints_a_result_line():
    proc = run_cli("run", "--job-json", json.dumps(spec(threads=0)))
    assert proc.returncode == 2
    result = result_of(proc)
    assert result["ok"] is False
    assert result["error"].startswith("invalid spec:")


def test_run_with_unreadable_job_file_exits_two(tmp_path):
    proc = run_cli("run", "--job-file", str(tmp_path / "absent.json"))
    assert proc.returncode == 2
    assert result_of(proc)["ok"] is False


def test_run_needs_exactly_one_source():
    assert run_cli("run").returncode != 0
    assert run_cli("run", "--job-json", "{}", "--job-file", "x").returncode != 0


def test_format_line_is_compact_json_after_the_prefix():
    result = runner.make_result("unit-001", True, 5.00049, 2.7, None, None, None)
    line = runner.format_line(result)
    assert line == ('workload-result {"id":"unit-001","ok":true,"cpu_seconds":5.0,'
                    '"wall_seconds":2.7,"peak_rss_mib":null,"services_ready_seconds":null,"error":null}')
    assert "\n" not in line


def test_error_text_is_one_short_line():
    result = runner.make_result("unit-001", False, None, None, None, None, "first\nsecond " + "x" * 500)
    assert "\n" not in result["error"]
    assert len(result["error"]) == runner.ERROR_LIMIT


@pytest.mark.parametrize("ok,expected", [(True, 0), (False, 1)])
def test_run_exit_code_follows_ok(monkeypatch, capsys, ok, expected):
    result = runner.make_result("unit-001", ok, 1.0, 1.0, None, None, None if ok else "boom")
    monkeypatch.setattr(runner, "run_job", lambda job: result)
    monkeypatch.setattr(runner, "install_signal_handlers", lambda: None)
    assert cli.main(["run", "--job-json", json.dumps(spec())]) == expected
    assert capsys.readouterr().out.startswith("workload-result ")
