from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.zero_tests import count_key

REPO = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).resolve().parents[1] / "test_lane.sh"
SHELLS = [shutil.which(name) for name in ("bash", "sh") if shutil.which(name)]

pytestmark = pytest.mark.skipif(not SHELLS, reason="a POSIX shell is required")

CORE = "sim/dotnet/tests/Core.Tests/Core.Tests.csproj"
UTILS = "sim/dotnet/tests/Utils.Tests/Utils.Tests.csproj"
FILTER = "Category!=integration"

FAKE_DOTNET = """\
#!/bin/sh
echo "cwd=$(basename "$(pwd)") $*" >> "$T/dotnet.log"
project=
dir=
name=
while [ $# -gt 0 ]; do
    case "$1" in
        test) project=$2; shift ;;
        --results-directory) dir=$2; shift ;;
        --logger) name=${2#trx;LogFileName=}; shift ;;
    esac
    shift
done
base=$(basename "$project" .csproj)
row=$(grep "^$base " "$T/behaviour" | head -n 1 || true)
pause=$(echo "$row" | awk '{ print $2 }')
rc=$(echo "$row" | awk '{ print $3 }')
total=$(echo "$row" | awk '{ print $4 }')
echo "start $base" >> "$T/events.log"
[ -z "$pause" ] || [ "$pause" = 0 ] || sleep "$pause"
case "$total" in
    none | '') ;;
    malformed) printf '<TestRun' > "$dir/$name" ;;
    *) printf '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><ResultSummary><Counters total="%s" /></ResultSummary></TestRun>' "$total" > "$dir/$name" ;;
esac
echo "output of $base"
echo "end $base" >> "$T/events.log"
exit "${rc:-0}"
"""


class Lab:
    def __init__(self, tmp_path: Path, shell: str):
        self.shell = shell
        self.t = tmp_path / "t"
        self.t.mkdir()
        self.workspace = tmp_path / "ws"
        (self.workspace / "sim" / "dotnet").mkdir(parents=True)
        self.counts = tmp_path / "counts"
        self.fake = self.t / "dotnet"
        self.fake.write_bytes(FAKE_DOTNET.encode())
        self.fake.chmod(0o755)
        for name in ("dotnet.log", "events.log", "behaviour"):
            (self.t / name).write_text("")

    def behaviour(self, *rows: str) -> None:
        (self.t / "behaviour").write_bytes("".join(f"{r}\n" for r in rows).encode())

    def listing(self, *projects: str, newline: str = "\n") -> str:
        path = self.t / "test.txt"
        path.write_bytes("".join(f"{p}{newline}" for p in projects).encode())
        return path.as_posix()

    def env(self, **over):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SIM_", "RUNNER_", "GITHUB_", "FAKE_"))}
        env.update(
            T=self.t.as_posix(),
            SIM_DOTNET=self.fake.as_posix(),
            SIM_PYTHON=Path(sys.executable).as_posix(),
            GITHUB_WORKSPACE=self.workspace.as_posix(),
        )
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, listing: str, parallel: str = "2", filter: str = FILTER, **over):
        return subprocess.run(
            [self.shell, SCRIPT.as_posix(), listing, self.counts.as_posix(), filter, parallel],
            cwd=self.workspace,
            env=self.env(**over),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return [line for line in (self.t / "dotnet.log").read_text().splitlines() if line]

    def events(self) -> list[str]:
        return [line for line in (self.t / "events.log").read_text().splitlines() if line]

    def count(self, project: str) -> str | None:
        path = self.counts / f"{key(project)}.count"
        return path.read_text().strip() if path.exists() else None


def key(project: str) -> str:
    return count_key(project)


@pytest.fixture(params=SHELLS)
def lab(tmp_path, request):
    return Lab(tmp_path, request.param)


def timed(lab, listing, parallel):
    started = time.monotonic()
    result = lab.run(listing, parallel=parallel)
    assert result.returncode == 0, result.stdout + result.stderr
    return time.monotonic() - started


def test_projects_run_in_parallel_up_to_the_limit(lab):
    listing = lab.listing(CORE, UTILS)
    lab.behaviour("Core.Tests 0 0 3", "Utils.Tests 0 0 2")
    overhead = timed(lab, listing, "2")
    lab.behaviour("Core.Tests 1 0 3", "Utils.Tests 1 0 2")
    wall = timed(lab, listing, "2")
    assert wall - overhead < 1.8, f"two 1s projects at PARALLEL=2 took {wall:.2f}s over a {overhead:.2f}s baseline"


def test_a_failing_project_fails_the_lane_and_the_others_still_finish(lab):
    lab.behaviour("Core.Tests 0 1 3", "Utils.Tests 1 0 2")
    result = lab.run(lab.listing(CORE, UTILS), parallel="1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert lab.events() == ["start Core.Tests", "end Core.Tests", "start Utils.Tests", "end Utils.Tests"]
    assert f"test_lane: fail {CORE}" in result.stdout
    assert f"test_lane: pass {UTILS}" in result.stdout
    assert lab.count(CORE) == "3" and lab.count(UTILS) == "2"


@pytest.mark.parametrize("code", ["2", "255"])
def test_any_dotnet_exit_code_is_a_lane_failure_that_keeps_going(lab, code):
    lab.behaviour(f"Core.Tests 0 {code} 3", "Utils.Tests 0 0 2")
    result = lab.run(lab.listing(CORE, UTILS), parallel="1")
    assert result.returncode == 1
    assert "end Utils.Tests" in lab.events()


def test_every_invocation_is_no_build_at_normal_verbosity_with_the_filter_and_a_trx(lab):
    lab.behaviour("Core.Tests 0 0 3", "Utils.Tests 0 0 2")
    result = lab.run(lab.listing(CORE, UTILS))
    assert result.returncode == 0, result.stdout + result.stderr
    calls = lab.calls()
    assert len(calls) == 2
    for call in calls:
        words = call.split(" ")
        assert words[0] == "cwd=dotnet" and words[1] == "test"
        assert "--no-build" in words
        assert "-v:normal" in words
        assert "--filter" in words and words[words.index("--filter") + 1] == FILTER
        assert "--results-directory" in words
    workspace = lab.workspace.as_posix()
    assert sorted(c.split(" ")[2] for c in calls) == sorted([f"{workspace}/{CORE}", f"{workspace}/{UTILS}"])


def test_each_project_gets_its_own_trx_and_count(lab):
    lab.behaviour("Core.Tests 0 0 3", "Utils.Tests 0 0 2")
    result = lab.run(lab.listing(CORE, UTILS))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (lab.counts / f"{key(CORE)}.trx").is_file()
    assert (lab.counts / f"{key(UTILS)}.trx").is_file()
    assert lab.count(CORE) == "3" and lab.count(UTILS) == "2"


def test_an_empty_filter_passes_no_filter(lab):
    lab.behaviour("Core.Tests 0 0 3")
    lab.run(lab.listing(CORE), filter="")
    assert "--filter" not in lab.calls()[0]


@pytest.mark.parametrize("total", ["none", "malformed"])
def test_no_readable_trx_leaves_no_count(lab, total):
    lab.behaviour(f"Core.Tests 0 0 {total}")
    result = lab.run(lab.listing(CORE))
    assert result.returncode == 0
    assert lab.count(CORE) is None


def test_a_stale_trx_and_count_from_an_earlier_run_are_removed(lab):
    lab.counts.mkdir()
    (lab.counts / f"{key(CORE)}.count").write_text("5\n")
    lab.behaviour("Core.Tests 0 0 3")
    lab.run(lab.listing(CORE))
    (lab.counts / f"{key(CORE)}.trx").write_text("stale")
    lab.behaviour("Core.Tests 0 0 none")
    lab.run(lab.listing(CORE))
    assert lab.count(CORE) is None
    assert not (lab.counts / f"{key(CORE)}.trx").exists()


def test_each_project_log_is_replayed_in_a_group(lab):
    lab.behaviour("Core.Tests 0 0 3", "Utils.Tests 0 0 2")
    result = lab.run(lab.listing(CORE, UTILS))
    out = result.stdout
    assert out.index(f"::group::{CORE}") < out.index("output of Core.Tests") < out.index(f"::group::{UTILS}")
    assert out.index(f"::group::{UTILS}") < out.index("output of Utils.Tests")


def test_an_empty_list_runs_nothing_and_passes(lab):
    result = lab.run(lab.listing("", "  "))
    assert result.returncode == 0
    assert "0 projects selected" in result.stdout
    assert lab.calls() == []


@pytest.mark.parametrize("parallel", ["", "x", "-1", "1.5"])
def test_a_bad_parallel_value_is_a_usage_error(lab, parallel):
    lab.behaviour("Core.Tests 0 0 3")
    result = lab.run(lab.listing(CORE), parallel=parallel)
    assert result.returncode == 2
    assert lab.calls() == []


def test_parallel_zero_uses_the_processor_count(lab):
    lab.behaviour("Core.Tests 0 0 3")
    result = lab.run(lab.listing(CORE), parallel="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(lab.calls()) == 1


def test_without_sim_dotnet_the_real_dotnet_on_the_path_is_used(lab):
    bindir = lab.t / "bin"
    bindir.mkdir()
    shutil.copy(lab.fake, bindir / "dotnet")
    (bindir / "dotnet").chmod(0o755)
    lab.behaviour("Core.Tests 0 0 3")
    result = lab.run(lab.listing(CORE), SIM_DOTNET=None, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(lab.calls()) == 1


def test_crlf_and_non_ascii_project_names_meet_the_zero_tests_guard(lab):
    odd = "sim/dotnet/tests/Tëst Tests/Tëst-Tests.csproj"
    lab.behaviour("Core.Tests 0 0 3", "Tëst-Tests 0 0 4")
    listing = lab.listing(CORE, odd, CORE, newline="\r\n")
    result = lab.run(listing)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(lab.calls()) == 2
    guard = subprocess.run(
        [sys.executable, "-m", "core.zero_tests", "--list", listing, "--counts", str(lab.counts)],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert guard.returncode == 0, guard.stdout + guard.stderr
    assert f"total=4 {odd}" in guard.stdout


def test_a_zero_count_from_the_lane_turns_the_guard_red(lab):
    lab.behaviour("Core.Tests 0 0 0")
    listing = lab.listing(CORE)
    assert lab.run(listing).returncode == 0
    guard = subprocess.run(
        [sys.executable, "-m", "core.zero_tests", "--list", listing, "--counts", str(lab.counts)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert guard.returncode == 1
