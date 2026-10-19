import json
import signal
import subprocess
import sys
import time

import pytest

from workload.tests.helpers import ROOT, result_of, run_cli, spec

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX resource limits and signals")

LIMIT_BYTES = 600 * 1024 * 1024


def limit_address_space():
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (LIMIT_BYTES, LIMIT_BYTES))


def test_touched_memory_shows_in_peak_rss():
    proc = run_cli("run", "--job-json", json.dumps(spec(cpu_seconds=0.3, memory_mib=200)))
    assert proc.returncode == 0, proc.stderr
    result = result_of(proc)
    assert result["ok"] is True
    assert result["peak_rss_mib"] >= 200


def test_memory_above_address_space_limit_fails_visibly():
    proc = run_cli("run", "--job-json", json.dumps(spec(cpu_seconds=0.3, memory_mib=1024)),
                   preexec_fn=limit_address_space)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    result = result_of(proc)
    assert result["ok"] is False
    assert "MemoryError" in result["error"]


def test_same_spec_passes_when_it_fits_the_limit():
    proc = run_cli("run", "--job-json", json.dumps(spec(cpu_seconds=0.3, memory_mib=64)),
                   preexec_fn=limit_address_space)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result_of(proc)["ok"] is True


def test_same_oversized_spec_passes_without_the_limit():
    proc = run_cli("run", "--job-json", json.dumps(spec(cpu_seconds=0.3, memory_mib=1024)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result_of(proc)["ok"] is True


def test_sigterm_ends_the_run_with_a_result_line_and_a_nonzero_exit():
    proc = subprocess.Popen(
        [sys.executable, "-m", "workload", "run", "--job-json",
         json.dumps(spec(cpu_seconds=120, threads=2, memory_mib=16))],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(3)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 143, out + err
    assert '"error":"terminated by signal"' in out
