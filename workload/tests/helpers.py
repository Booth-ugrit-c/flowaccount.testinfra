import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULT_PREFIX = "workload-result "


def spec(**overrides):
    base = {"id": "unit-001", "lane": "unit", "cpu_seconds": 1, "threads": 1,
            "memory_mib": 0, "services": [], "label": "x"}
    base.update(overrides)
    return base


def run_cli(*args, preexec_fn=None, timeout=300):
    return subprocess.run(
        [sys.executable, "-m", "workload", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout, preexec_fn=preexec_fn,
    )


def result_of(proc):
    lines = [line for line in proc.stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    assert len(lines) == 1, proc.stdout + proc.stderr
    return json.loads(lines[0][len(RESULT_PREFIX):])


def docker_available():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
