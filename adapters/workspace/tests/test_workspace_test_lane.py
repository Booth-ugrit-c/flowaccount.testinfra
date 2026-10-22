from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "test_lane.sh"
SHELLS = [shutil.which(name) for name in ("bash", "sh") if shutil.which(name)]

pytestmark = pytest.mark.skipif(not SHELLS, reason="a POSIX shell is required")

ALPHA = "@sim/alpha"
SOLO = "@sim/solo"
SEP = "\x1f"

FAKE_NX = """\
#!/bin/sh
{ printf 'cwd=%s' "$(basename "$(pwd)")"; for a in "$@"; do printf '\\037%s' "$a"; done; echo; } >> "$T/nx.log"
project=${2%:test}
out=
for a in "$@"; do
    case "$a" in
        --outputFile=*) out=${a#--outputFile=} ;;
    esac
done
row=$(awk -v p="$project" '$1 == p' "$T/behaviour" | head -n 1)
pause=$(echo "$row" | awk '{ print $2 }')
rc=$(echo "$row" | awk '{ print $3 }')
passed=$(echo "$row" | awk '{ print $4 }')
failed=$(echo "$row" | awk '{ print $5 }')
skipped=$(echo "$row" | awk '{ print $6 }')
echo "start $project" >> "$T/events.log"
[ -z "$pause" ] || [ "$pause" = 0 ] || sleep "$pause"
case "$passed" in
    none | '') ;;
    malformed) printf '{"numPassedTests":' > "$out" ;;
    *) printf '{"numTotalTests":%s,"numPassedTests":%s,"numFailedTests":%s,"numPendingTests":%s,"numTodoTests":0}' \
        "$((passed + failed + skipped))" "$passed" "$failed" "$skipped" > "$out" ;;
esac
echo "output of $project"
echo "end $project" >> "$T/events.log"
exit "${rc:-0}"
"""

FAKE_NODE = """\
#!/bin/sh
echo "entry=$1" >> "$T/node.log"
shift
exec "$T/nx" "$@"
"""


def key(project: str) -> str:
    return re.sub(rb"[^A-Za-z0-9._-]", b"_", project.encode("utf-8")).decode("ascii")


class Lab:
    def __init__(self, tmp_path: Path, shell: str):
        self.shell = shell
        self.t = tmp_path / "t"
        self.t.mkdir()
        self.workspace = tmp_path / "ws"
        (self.workspace / "sim" / "workspace").mkdir(parents=True)
        self.counts = tmp_path / "counts"
        self.fake = self.t / "nx"
        self.fake.write_bytes(FAKE_NX.encode())
        self.fake.chmod(0o755)
        for name in ("nx.log", "node.log", "events.log", "behaviour"):
            (self.t / name).write_text("")

    def behaviour(self, *rows: str) -> None:
        (self.t / "behaviour").write_bytes("".join(f"{r}\n" for r in rows).encode())

    def listing(self, *projects: str, newline: str = "\n") -> str:
        path = self.t / "test.txt"
        path.write_bytes("".join(f"{p}{newline}" for p in projects).encode())
        return path.as_posix()

    def env(self, **over):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SIM_", "RUNNER_", "GITHUB_", "NX_"))}
        env.update(T=self.t.as_posix(), SIM_NX=self.fake.as_posix(), SIM_PYTHON=Path(sys.executable).as_posix())
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, listing: str, parallel: str = "2", **over):
        return subprocess.run(
            [self.shell, SCRIPT.as_posix(), listing, self.counts.as_posix(), parallel],
            cwd=self.workspace,
            env=self.env(**over),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

    def calls(self) -> list[list[str]]:
        text = (self.t / "nx.log").read_text(encoding="utf-8")
        return [line.split(SEP) for line in text.splitlines() if line]

    def events(self) -> list[str]:
        return [line for line in (self.t / "events.log").read_text().splitlines() if line]

    def count(self, project: str) -> str | None:
        path = self.counts / f"{key(project)}.count"
        return path.read_text(encoding="ascii") if path.exists() else None


@pytest.fixture(params=SHELLS)
def lab(tmp_path, request):
    return Lab(tmp_path, request.param)


def timed(lab, listing, parallel):
    started = time.monotonic()
    result = lab.run(listing, parallel=parallel)
    assert result.returncode == 0, result.stdout + result.stderr
    return time.monotonic() - started


def test_projects_run_in_parallel_up_to_the_limit(lab):
    listing = lab.listing(ALPHA, SOLO)
    lab.behaviour(f"{ALPHA} 0 0 3 0 0", f"{SOLO} 0 0 2 0 0")
    overhead = timed(lab, listing, "2")
    lab.behaviour(f"{ALPHA} 1 0 3 0 0", f"{SOLO} 1 0 2 0 0")
    wall = timed(lab, listing, "2")
    assert wall - overhead < 1.8, f"two 1s projects at PARALLEL=2 took {wall:.2f}s over a {overhead:.2f}s baseline"


def test_a_failing_project_fails_the_lane_and_the_others_still_finish(lab):
    lab.behaviour(f"{ALPHA} 0 1 2 1 0", f"{SOLO} 1 0 2 0 0")
    result = lab.run(lab.listing(ALPHA, SOLO), parallel="1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert lab.events() == [f"start {ALPHA}", f"end {ALPHA}", f"start {SOLO}", f"end {SOLO}"]
    assert f"test_lane: fail {ALPHA}" in result.stdout
    assert f"test_lane: pass {SOLO}" in result.stdout
    assert lab.count(ALPHA) == "3\n" and lab.count(SOLO) == "2\n"


@pytest.mark.parametrize("code", ["2", "255"])
def test_any_nx_exit_code_is_a_lane_failure_that_keeps_going(lab, code):
    lab.behaviour(f"{ALPHA} 0 {code} 3 0 0", f"{SOLO} 0 0 2 0 0")
    result = lab.run(lab.listing(ALPHA, SOLO), parallel="1")
    assert result.returncode == 1
    assert f"end {SOLO}" in lab.events()


def test_every_invocation_is_the_uncached_test_target_with_jest_json_after_the_separator(lab):
    lab.behaviour(f"{ALPHA} 0 0 3 0 0", f"{SOLO} 0 0 2 0 0")
    result = lab.run(lab.listing(ALPHA, SOLO))
    assert result.returncode == 0, result.stdout + result.stderr
    calls = sorted(lab.calls(), key=lambda call: call[2])
    assert len(calls) == 2
    for call, project in zip(calls, [ALPHA, SOLO]):
        assert call[:-1] == ["cwd=workspace", "run", f"{project}:test", "--skip-nx-cache", "--outputStyle=static",
                             "--", "--ci", "--json"]
        assert call[-1].startswith("--outputFile=") and call[-1].endswith(f"/{key(project)}.json")
        assert (lab.counts / f"{key(project)}.json").is_file()


def test_the_count_is_the_executed_tests_not_the_total(lab):
    lab.behaviour(f"{ALPHA} 0 1 2 1 4", f"{SOLO} 0 0 5 0 0")
    lab.run(lab.listing(ALPHA, SOLO))
    assert lab.count(ALPHA) == "3\n" and lab.count(SOLO) == "5\n"


def test_a_project_whose_tests_were_all_skipped_writes_a_zero_count_for_the_guard(lab):
    lab.behaviour(f"{ALPHA} 0 0 0 0 3", f"{SOLO} 0 0 2 0 0")
    result = lab.run(lab.listing(ALPHA, SOLO))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (lab.counts / "_sim_alpha.count").read_text(encoding="ascii") == "0\n"
    assert lab.count(SOLO) == "2\n"


@pytest.mark.parametrize("report", ["none", "malformed"])
def test_no_readable_jest_json_leaves_no_count(lab, report):
    lab.behaviour(f"{ALPHA} 0 0 {report}")
    result = lab.run(lab.listing(ALPHA))
    assert result.returncode == 0
    assert lab.count(ALPHA) is None
    assert not (lab.counts / f"{key(ALPHA)}.count.tmp").exists()


def test_a_stale_json_and_count_from_an_earlier_run_are_removed(lab):
    lab.behaviour(f"{ALPHA} 0 0 3 0 0")
    lab.run(lab.listing(ALPHA))
    assert lab.count(ALPHA) == "3\n"
    lab.behaviour(f"{ALPHA} 0 0 none")
    lab.run(lab.listing(ALPHA))
    assert lab.count(ALPHA) is None
    assert not (lab.counts / f"{key(ALPHA)}.json").exists()


def test_a_stale_json_alone_cannot_produce_a_count(lab):
    lab.counts.mkdir()
    (lab.counts / f"{key(ALPHA)}.json").write_text(json.dumps({"numPassedTests": 7, "numFailedTests": 0}))
    lab.behaviour(f"{ALPHA} 0 0 none")
    lab.run(lab.listing(ALPHA))
    assert lab.count(ALPHA) is None


def test_each_project_log_is_replayed_in_a_group(lab):
    lab.behaviour(f"{ALPHA} 0 0 3 0 0", f"{SOLO} 0 0 2 0 0")
    out = lab.run(lab.listing(ALPHA, SOLO)).stdout
    assert out.index(f"::group::{ALPHA}") < out.index(f"output of {ALPHA}") < out.index(f"::group::{SOLO}")
    assert out.index(f"::group::{SOLO}") < out.index(f"output of {SOLO}")


def test_an_empty_list_runs_nothing_and_passes(lab):
    result = lab.run(lab.listing("", "  "))
    assert result.returncode == 0
    assert "0 projects selected" in result.stdout
    assert lab.calls() == []


@pytest.mark.parametrize("parallel", ["", "x", "-1", "1.5"])
def test_a_bad_parallel_value_is_a_usage_error(lab, parallel):
    lab.behaviour(f"{ALPHA} 0 0 3 0 0")
    result = lab.run(lab.listing(ALPHA), parallel=parallel)
    assert result.returncode == 2
    assert lab.calls() == []


def test_parallel_zero_uses_the_processor_count(lab):
    lab.behaviour(f"{ALPHA} 0 0 3 0 0")
    result = lab.run(lab.listing(ALPHA), parallel="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(lab.calls()) == 1


def install_fake_nx_package(lab, entry: str = "bin/nx.js"):
    package = lab.workspace / "sim" / "workspace" / "node_modules" / "nx"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "nx", "bin": {"nx": entry}}))
    (package / "bin" / "nx.js").write_text("")
    bindir = lab.t / "bin"
    bindir.mkdir()
    (bindir / "node").write_bytes(FAKE_NODE.encode())
    (bindir / "node").chmod(0o755)
    return package, str(bindir) + os.pathsep + os.environ["PATH"]


def test_without_sim_nx_node_runs_the_nx_entry_declared_by_the_installed_package(lab):
    package, path = install_fake_nx_package(lab)
    lab.behaviour(f"{ALPHA} 0 0 3 0 0")
    result = lab.run(lab.listing(ALPHA), SIM_NX=None, PATH=path)
    assert result.returncode == 0, result.stdout + result.stderr
    entries = (lab.t / "node.log").read_text().splitlines()
    assert len(entries) == 1
    assert Path(entries[0].removeprefix("entry=")).resolve() == (package / "bin" / "nx.js").resolve()
    assert lab.calls()[0][1:3] == ["run", f"{ALPHA}:test"]
    assert lab.count(ALPHA) == "3\n"


@pytest.mark.parametrize("entry", [None, "../../escape.js"])
def test_an_unresolvable_nx_is_exit_2_and_runs_nothing(lab, entry):
    path = os.environ["PATH"]
    if entry is not None:
        _, path = install_fake_nx_package(lab, entry)
    lab.behaviour(f"{ALPHA} 0 0 3 0 0")
    result = lab.run(lab.listing(ALPHA), SIM_NX=None, PATH=path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "test_lane:" in result.stderr
    assert lab.calls() == []


def test_nx_runs_with_daemon_cloud_and_tui_off(lab):
    lab.fake.write_bytes(FAKE_NX.replace('exit "${rc:-0}"', 'echo "env=$NX_DAEMON $NX_NO_CLOUD $NX_TUI"; exit 0').encode())
    lab.behaviour(f"{ALPHA} 0 0 3 0 0")
    result = lab.run(lab.listing(ALPHA), NX_DAEMON="true", NX_NO_CLOUD="false", NX_TUI="true")
    assert "env=false true false" in result.stdout


def test_crlf_duplicates_and_non_ascii_names_get_one_run_and_the_guard_key(lab):
    odd = "@sim/tëst-lib"
    lab.behaviour(f"{ALPHA} 0 0 3 0 0", f"{odd} 0 0 4 0 0")
    listing = lab.listing(ALPHA, odd, ALPHA, newline="\r\n")
    result = lab.run(listing)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(call[2] for call in lab.calls()) == sorted([f"{ALPHA}:test", f"{odd}:test"])
    assert (lab.counts / "_sim_t__st-lib.count").read_text(encoding="ascii") == "4\n"
    assert lab.count(ALPHA) == "3\n"
