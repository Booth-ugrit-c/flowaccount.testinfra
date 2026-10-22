from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "kill_own_runner.sh"
BASH = shutil.which("bash")
SHELLS = [name for name in ("bash", "sh") if shutil.which(name)]

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

TWO_RUNNERS = """\
#!/bin/sh
echo "$(cat "$T/a_listener") 1 Runner.Listener"
echo "$(cat "$T/a_worker") $(cat "$T/a_listener") Runner.Worker"
echo "$(cat "$T/self") $(cat "$T/a_worker") sh"
echo "$(cat "$T/b_listener") 1 Runner.Listener"
echo "$(cat "$T/b_worker") $(cat "$T/b_listener") Runner.Worker"
echo "$(cat "$T/b_shell") $(cat "$T/b_worker") bash"
"""

NO_RUNNER = """\
#!/bin/sh
echo "$(cat "$T/self") 1 sh"
echo "$(cat "$T/a_listener") 1 Runner.Listener"
echo "$(cat "$T/a_worker") $(cat "$T/a_listener") Runner.Worker"
"""

BROKEN = """\
#!/bin/sh
exit 1
"""

WRAPPER = """\
#!/bin/bash
for name in a_listener a_worker b_listener b_worker b_shell; do
    sleep 120 & echo $! > "$T/$name"
done
bash -c 'echo $$ > "$T/self"; exec "$KILL_SHELL" "$SCRIPT"' > "$T/out" 2>&1
echo $? > "$T/rc"
for name in a_listener a_worker b_listener b_worker b_shell; do
    if kill -0 "$(cat "$T/$name")" 2>/dev/null; then echo alive > "$T/$name.state"; else echo dead > "$T/$name.state"; fi
    kill -KILL "$(cat "$T/$name")" 2>/dev/null
done
exit 0
"""


def run(tmp_path, shell, table):
    t = tmp_path / "t"
    t.mkdir()
    for name, body in (("ps", table), ("wrapper.sh", WRAPPER)):
        path = t / name
        path.write_bytes(body.encode())
        path.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIM_")}
    env.update(T=t.as_posix(), SCRIPT=SCRIPT.as_posix(), KILL_SHELL=shell, SIM_PS=(t / "ps").as_posix())
    subprocess.run([BASH, (t / "wrapper.sh").as_posix()], env=env, timeout=60, check=True)
    read = lambda name: (t / name).read_text().strip()
    states = {name: read(f"{name}.state") for name in ("a_listener", "a_worker", "b_listener", "b_worker", "b_shell")}
    return int(read("rc")), read("out"), states


@pytest.mark.parametrize("shell", SHELLS)
def test_kills_only_the_runner_processes_on_its_own_parent_chain(tmp_path, shell):
    rc, _, states = run(tmp_path, shell, TWO_RUNNERS)
    assert rc == 0
    assert (states["a_listener"], states["a_worker"]) == ("dead", "dead")
    assert (states["b_listener"], states["b_worker"], states["b_shell"]) == ("alive", "alive", "alive")


@pytest.mark.parametrize("shell", SHELLS)
def test_without_a_runner_on_its_chain_it_kills_nothing_and_fails(tmp_path, shell):
    rc, out, states = run(tmp_path, shell, NO_RUNNER)
    assert rc != 0
    assert set(states.values()) == {"alive"}
    assert "no runner process" in out


@pytest.mark.parametrize("shell", SHELLS)
def test_a_failing_process_listing_kills_nothing_and_fails(tmp_path, shell):
    rc, _, states = run(tmp_path, shell, BROKEN)
    assert rc != 0
    assert set(states.values()) == {"alive"}
