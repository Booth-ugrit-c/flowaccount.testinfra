import functools
import json
import sys

import pytest

from workload import cpu, runner, spec as spec_mod
from workload.tests import crashers
from workload.tests.helpers import run_cli, result_of, spec


def test_split_sums_exactly_and_is_even():
    shares = cpu.split_cpu(120.0, 4)
    assert shares == [30.0] * 4
    shares = cpu.split_cpu(10.0, 3)
    assert len(shares) == 3
    assert sum(shares) == pytest.approx(10.0, abs=1e-12)
    assert max(shares) - min(shares) < 1e-9


def test_split_single_worker_takes_everything():
    assert cpu.split_cpu(7.5, 1) == [7.5]


def test_floor_boundary():
    assert cpu.cpu_meets_floor(100.0, 98.0)
    assert not cpu.cpu_meets_floor(100.0, 97.99)
    assert cpu.cpu_meets_floor(100.0, 130.0)


def test_crosscheck_tolerance():
    assert cpu.crosscheck_ok(10.0, None)
    assert cpu.crosscheck_ok(10.0, 10.3)
    assert not cpu.crosscheck_ok(10.0, 9.0)
    assert not cpu.crosscheck_ok(10.0, 12.0)


def test_real_burn_lands_within_two_percent():
    measured, _ = cpu.run_burn(2.0, 2)
    assert 2.0 * 0.98 <= measured <= 2.0 * 1.02 + 0.1


def test_cli_run_prints_one_result_line_with_exact_keys():
    proc = run_cli("run", "--job-json", json.dumps(spec(cpu_seconds=1, threads=2, memory_mib=8)))
    assert proc.returncode == 0, proc.stderr
    result_lines = [line for line in proc.stdout.splitlines()]
    assert len(result_lines) == 1
    assert result_lines[0].startswith("workload-result {")
    assert ", " not in result_lines[0] and ": " not in result_lines[0]
    result = result_of(proc)
    assert list(result) == ["id", "ok", "cpu_seconds", "wall_seconds", "peak_rss_mib",
                            "services_ready_seconds", "error"]
    assert result["ok"] is True and result["error"] is None
    assert 0.98 <= result["cpu_seconds"] <= 1.12
    assert result["services_ready_seconds"] is None


def test_cli_run_reads_job_file(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps(spec(cpu_seconds=0.5)), encoding="utf-8")
    proc = run_cli("run", "--job-file", str(path))
    assert proc.returncode == 0, proc.stderr
    assert result_of(proc)["id"] == "unit-001"


def test_environment_is_never_printed(monkeypatch):
    monkeypatch.setenv("WORKLOAD_TEST_SECRET", "s3cr3t-value-123")
    proc = run_cli("run", "--job-json", json.dumps(spec(cpu_seconds=0.3)))
    assert "s3cr3t-value-123" not in proc.stdout + proc.stderr


def job(**overrides):
    return spec_mod.parse_spec(spec(**overrides))


def test_worker_stopping_early_fails_the_job():
    burn = functools.partial(cpu.run_burn, target=crashers.early_stop_worker)
    result = runner.run_job(job(cpu_seconds=2.0, threads=2), burn=burn)
    assert result["ok"] is False
    assert result["error"].startswith("cpu below target")
    assert result["cpu_seconds"] < 0.98 * 2.0


def test_killed_worker_fails_the_job():
    burn = functools.partial(cpu.run_burn, target=crashers.killed_worker)
    result = runner.run_job(job(threads=2), burn=burn)
    assert result["ok"] is False
    assert "worker" in result["error"] and "code 9" in result["error"]


def test_worker_without_report_fails_the_job():
    burn = functools.partial(cpu.run_burn, target=crashers.silent_worker)
    result = runner.run_job(job(), burn=burn)
    assert result["ok"] is False
    assert "worker 0" in result["error"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RUSAGE_CHILDREN is Linux-only here")
def test_kernel_cross_check_agrees_with_worker_reports():
    reported, children = cpu.run_burn(1.5, 2)
    assert children is not None
    assert cpu.crosscheck_ok(reported, children)
