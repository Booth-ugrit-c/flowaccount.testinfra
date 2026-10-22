from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cache_build.sh"
BASH = shutil.which("bash")
SHELLS = [shutil.which(name) for name in ("bash", "sh") if shutil.which(name)]

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

CORE = "sim/dotnet/src/Core/Core.csproj"
UTILS = "sim/dotnet/src/Utils/Utils.csproj"
SERVICE = "sim/dotnet/src/Service/Service.csproj"
ENDPOINT_LAB = "http://lab-endpoint.example.test:9000"
ENDPOINT_TAILNET = "http://tailnet-endpoint.example.test:9000"

FAKE_CACHE = """\
#!/bin/sh
sub=$1
shift
project=
markers=
while [ $# -gt 0 ]; do
    case "$1" in
        --project) project=$(basename "$2" .csproj); shift ;;
        --markers) markers=$2; shift ;;
    esac
    shift
done
echo "$sub $project endpoint=${SIM_CACHE_ENDPOINT:-}" >> "$T/cache.log"
line=$(grep "^$sub ${project:--} " "$T/cache.table" | head -n 1 || true)
out=$(echo "$line" | awk '{ print $3 }')
rc=$(echo "$line" | awk '{ print $4 }')
if [ "$out" = tripped ] && [ -n "$markers" ]; then
    mkdir -p "$markers"
    : > "$markers/cache-tripwire"
fi
[ -z "$out" ] || [ "$out" = - ] || echo "$out"
exit "${rc:-0}"
"""

FAKE_DOTNET = """\
#!/bin/sh
echo "cwd=$(basename "$(pwd)") $*" >> "$T/dotnet.log"
case "$*" in
    *"$FAKE_DOTNET_FAIL_ON"*) [ -z "${FAKE_DOTNET_FAIL_ON:-}" ] || exit 1 ;;
esac
exit 0
"""


class Lab:
    def __init__(self, tmp_path, shell):
        self.shell = shell
        self.t = tmp_path / "t"
        self.t.mkdir()
        self.workspace = tmp_path / "ws"
        (self.workspace / "sim" / "dotnet" / "src").mkdir(parents=True)
        (self.workspace / "sim" / "dotnet" / ".gitignore").write_text("bin/\nobj/\n")
        (self.workspace / "sim" / "dotnet" / "global.json").write_text("{}")
        for name, body in (("cache", FAKE_CACHE), ("dotnet", FAKE_DOTNET)):
            path = self.t / name
            path.write_bytes(body.encode())
            path.chmod(0o755)
        self.table([])
        (self.t / "dotnet.log").write_text("")
        (self.t / "cache.log").write_text("")
        git = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run([*git, "add", "-A"], cwd=self.workspace, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "fixture"], cwd=self.workspace, check=True)
        self.git = git
        subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=self.workspace, check=True)

    def table(self, rows):
        (self.t / "cache.table").write_bytes("".join(f"{r}\n" for r in rows).encode())

    def env(self, **over):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SIM_", "RUNNER_", "GITHUB_", "FAKE_"))}
        env.update(
            T=self.t.as_posix(),
            SIM_CACHE_CMD=(self.t / "cache").as_posix(),
            SIM_DOTNET=(self.t / "dotnet").as_posix(),
            GITHUB_WORKSPACE=self.workspace.as_posix(),
            RUNNER_ENVIRONMENT="self-hosted",
            SIM_DEFAULT_BRANCH="main",
            SIM_CACHE_ENDPOINT_LAB=ENDPOINT_LAB,
            SIM_CACHE_ENDPOINT_TAILNET=ENDPOINT_TAILNET,
        )
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *args, **over):
        return subprocess.run(
            [self.shell, SCRIPT.as_posix(), *args],
            cwd=self.workspace,
            env=self.env(**over),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def plan(self, *projects):
        path = self.t / "plan.txt"
        path.write_bytes("".join(f"{p}\n" for p in projects).encode())
        return path.as_posix()

    def built(self):
        path = self.t / "built.txt"
        return [line for line in path.read_text().splitlines() if line] if path.exists() else []

    def write_built(self, *projects):
        path = self.t / "built.txt"
        path.write_bytes("".join(f"{p}\n" for p in projects).encode())
        return path.as_posix()

    def cache_calls(self):
        return [line.split(" endpoint=")[0] for line in (self.t / "cache.log").read_text().splitlines() if line]

    def cache_endpoints(self):
        return {line.split(" endpoint=")[1] for line in (self.t / "cache.log").read_text().splitlines() if line}

    def dotnet_calls(self):
        return [line for line in (self.t / "dotnet.log").read_text().splitlines() if line]


@pytest.fixture(params=SHELLS)
def lab(tmp_path, request):
    return Lab(tmp_path, request.param)


def test_a_hit_is_skipped_and_a_miss_is_built_alone_without_its_references(lab):
    lab.table(["restore Core hit 0", "restore Utils miss 0"])
    result = lab.run("build", lab.plan(CORE, UTILS), (lab.t / "built.txt").as_posix())
    assert result.returncode == 0, result.stderr
    assert lab.cache_calls() == ["restore Core", "restore Utils"]
    calls = lab.dotnet_calls()
    assert len(calls) == 1
    assert calls[0] == (
        f"cwd=dotnet build {lab.workspace.as_posix()}/{UTILS} -c Release --disable-build-servers "
        "-p:BuildProjectReferences=false -p:IncludeSourceRevisionInInformationalVersion=false"
    )
    assert lab.built() == [UTILS]


def test_the_plan_is_built_in_the_given_order(lab):
    lab.table(["restore Core miss 0", "restore Utils miss 0", "restore Service miss 0"])
    lab.run("build", lab.plan(CORE, UTILS, SERVICE), (lab.t / "built.txt").as_posix())
    assert lab.built() == [CORE, UTILS, SERVICE]
    assert [c.split("/")[-1].split(" ")[0] for c in lab.dotnet_calls()] == ["Core.csproj", "Utils.csproj", "Service.csproj"]


@pytest.mark.parametrize("answer", ["- 2", "garbage 0", "- 1", "miss 1", "hit 2", "HIT 0", "- 0"])
def test_anything_but_a_clean_hit_is_a_cold_build_and_never_a_failure(lab, answer):
    lab.table([f"restore Core {answer}"])
    result = lab.run("build", lab.plan(CORE), (lab.t / "built.txt").as_posix())
    assert result.returncode == 0, result.stderr
    assert lab.built() == [CORE]


def test_a_failing_build_fails_the_step_and_stops_the_plan(lab):
    lab.table(["restore Core miss 0", "restore Utils miss 0", "restore Service miss 0"])
    result = lab.run("build", lab.plan(CORE, UTILS, SERVICE), (lab.t / "built.txt").as_posix(), FAKE_DOTNET_FAIL_ON="Utils")
    assert result.returncode != 0
    assert lab.built() == [CORE]
    assert not any("Service" in call for call in lab.dotnet_calls())


def test_with_the_cache_off_nothing_is_restored_and_everything_is_built(lab):
    lab.table(["restore Core hit 0", "restore Utils hit 0"])
    lab.run("build", lab.plan(CORE, UTILS), (lab.t / "built.txt").as_posix(), SIM_CACHE_MODE="off")
    assert lab.cache_calls() == []
    assert lab.built() == [CORE, UTILS]


def test_an_empty_plan_builds_nothing(lab):
    result = lab.run("build", lab.plan(), (lab.t / "built.txt").as_posix())
    assert result.returncode == 0
    assert lab.dotnet_calls() == [] and lab.cache_calls() == []


@pytest.mark.parametrize(
    "environment, expected",
    [("self-hosted", ENDPOINT_LAB), ("github-hosted", ENDPOINT_TAILNET)],
)
def test_the_endpoint_follows_the_runner_environment(lab, environment, expected):
    lab.table(["restore Core hit 0"])
    result = lab.run("build", lab.plan(CORE), (lab.t / "built.txt").as_posix(), RUNNER_ENVIRONMENT=environment)
    assert lab.cache_endpoints() == {expected}
    assert ENDPOINT_LAB not in result.stdout + result.stderr and ENDPOINT_TAILNET not in result.stdout + result.stderr


def test_an_unknown_runner_environment_gets_no_endpoint(lab):
    lab.table(["restore Core hit 0"])
    lab.run("build", lab.plan(CORE), (lab.t / "built.txt").as_posix(), RUNNER_ENVIRONMENT="something-else")
    assert lab.cache_endpoints() == {""}


def test_the_tripwire_check_passes_when_the_store_is_clear(lab):
    lab.table(["tripwire - clear 0"])
    result = lab.run("tripwire", (lab.t / "markers").as_posix())
    assert result.returncode == 0
    assert lab.cache_calls() == ["tripwire "]
    assert not (lab.t / "markers" / "cache-tripwire").exists()
    assert not (lab.t / "markers" / "cache-off").exists()
    assert "::warning" not in result.stdout


def test_a_tripped_store_fails_the_step_and_leaves_the_marker(lab):
    lab.table(["tripwire - tripped 0"])
    result = lab.run("tripwire", (lab.t / "markers").as_posix())
    assert result.returncode != 0
    assert (lab.t / "markers" / "cache-tripwire").exists()


def test_a_tripped_answer_stops_the_step_even_without_the_marker(lab):
    lab.table(["tripwire - tripped 0"])
    (lab.t / "cache").write_bytes(FAKE_CACHE.replace(': > "$markers/cache-tripwire"', ":").encode())
    result = lab.run("tripwire", (lab.t / "markers").as_posix())
    assert result.returncode != 0
    assert not (lab.t / "markers" / "cache-off").exists()


@pytest.mark.parametrize("answer", ["- 2", "- 1", "- 0", "garbage 0", "CLEAR 0"])
def test_a_store_that_cannot_be_asked_is_not_a_trip_but_turns_the_cache_off_loudly(lab, answer):
    lab.table([f"tripwire - {answer}"])
    summary = lab.t / "summary.md"
    result = lab.run("tripwire", (lab.t / "markers").as_posix(), GITHUB_STEP_SUMMARY=summary.as_posix())
    assert result.returncode == 0
    assert not (lab.t / "markers" / "cache-tripwire").exists()
    assert (lab.t / "markers" / "cache-off").exists()
    assert "::warning title=cache off::the store did not answer the tripwire check" in result.stdout
    assert summary.read_text().startswith("cache off: ")


@pytest.mark.parametrize("join", [None, "", "failure", "skipped", "cancelled"])
def test_a_hosted_runner_whose_tailnet_join_failed_never_asks_the_store(lab, join):
    lab.table(["tripwire - clear 0"])
    result = lab.run("tripwire", (lab.t / "markers").as_posix(), RUNNER_ENVIRONMENT="github-hosted", SIM_TAILNET_JOIN=join)
    assert result.returncode == 0
    assert lab.cache_calls() == []
    assert (lab.t / "markers" / "cache-off").exists()
    assert "::warning title=cache off::the tailnet join failed" in result.stdout


def test_a_hosted_runner_that_joined_asks_the_store(lab):
    lab.table(["tripwire - clear 0"])
    result = lab.run("tripwire", (lab.t / "markers").as_posix(), RUNNER_ENVIRONMENT="github-hosted", SIM_TAILNET_JOIN="success")
    assert result.returncode == 0
    assert lab.cache_calls() == ["tripwire "]
    assert not (lab.t / "markers" / "cache-off").exists()


def test_a_lab_runner_ignores_the_tailnet_join_outcome(lab):
    lab.table(["tripwire - clear 0"])
    result = lab.run("tripwire", (lab.t / "markers").as_posix(), SIM_TAILNET_JOIN="failure")
    assert result.returncode == 0
    assert lab.cache_calls() == ["tripwire "]


def cache_off_markers(lab):
    markers = lab.t / "markers"
    markers.mkdir(exist_ok=True)
    (markers / "cache-off").write_text("")
    return markers.as_posix()


def test_with_the_cache_off_marker_the_build_never_asks_the_store(lab):
    lab.table(["restore Core hit 0", "restore Utils hit 0"])
    result = lab.run("build", lab.plan(CORE, UTILS), (lab.t / "built.txt").as_posix(), MARKERS=cache_off_markers(lab))
    assert result.returncode == 0, result.stderr
    assert lab.cache_calls() == []
    assert lab.built() == [CORE, UTILS]


def test_with_the_cache_off_marker_nothing_is_saved(lab):
    result = lab.run("save", lab.write_built(CORE), MARKERS=cache_off_markers(lab))
    assert result.returncode == 0
    assert lab.cache_calls() == []


def test_a_failed_join_then_build_then_save_is_a_cold_build_that_never_touches_the_store(lab):
    lab.table(["tripwire - clear 0", "restore Core hit 0", "save Core saved 0"])
    markers = (lab.t / "markers").as_posix()
    hosted = dict(RUNNER_ENVIRONMENT="github-hosted", SIM_TAILNET_JOIN="failure", MARKERS=markers)
    assert lab.run("tripwire", markers, **hosted).returncode == 0
    assert lab.run("build", lab.plan(CORE), (lab.t / "built.txt").as_posix(), **hosted).returncode == 0
    assert lab.run("save", (lab.t / "built.txt").as_posix(), **hosted).returncode == 0
    assert lab.cache_calls() == []
    assert lab.built() == [CORE]


def test_a_clean_tree_saves_every_built_project_in_order(lab):
    lab.table(["save Core saved 0", "save Utils saved 0"])
    result = lab.run("save", lab.write_built(CORE, UTILS))
    assert result.returncode == 0, result.stderr
    assert lab.cache_calls() == ["save Core", "save Utils"]


def test_untracked_files_outside_the_build_output_stop_the_save(lab):
    (lab.workspace / "sim" / "dotnet" / "src" / "Stray.cs").write_text("x")
    result = lab.run("save", lab.write_built(CORE))
    assert result.returncode == 0
    assert lab.cache_calls() == []
    assert "untracked" in result.stdout + result.stderr


def test_a_modified_tracked_file_stops_the_save(lab):
    (lab.workspace / "sim" / "dotnet" / "global.json").write_text('{"changed": true}')
    result = lab.run("save", lab.write_built(CORE))
    assert result.returncode == 0
    assert lab.cache_calls() == []


def test_build_output_alone_does_not_stop_the_save_even_when_it_is_not_ignored(lab):
    (lab.workspace / "sim" / "dotnet" / ".gitignore").write_text("")
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qam", "unignore"], cwd=lab.workspace, check=True)
    subprocess.run([*lab.git, "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=lab.workspace, check=True)
    for name in ("bin", "obj"):
        (lab.workspace / "sim" / "dotnet" / "src" / "Core" / name).mkdir(parents=True)
        (lab.workspace / "sim" / "dotnet" / "src" / "Core" / name / "x.dll").write_text("x")
    result = lab.run("save", lab.write_built(CORE))
    assert result.returncode == 0
    assert lab.cache_calls() == ["save Core"]


def test_a_refused_key_on_save_is_skipped_and_the_next_project_still_saves(lab):
    lab.table(["save Core - 2", "save Utils saved 0"])
    result = lab.run("save", lab.write_built(CORE, UTILS))
    assert result.returncode == 0
    assert lab.cache_calls() == ["save Core", "save Utils"]


def test_with_the_cache_off_nothing_is_saved(lab):
    result = lab.run("save", lab.write_built(CORE), SIM_CACHE_MODE="off")
    assert result.returncode == 0
    assert lab.cache_calls() == []


def test_nothing_built_means_nothing_saved(lab):
    result = lab.run("save", lab.write_built())
    assert result.returncode == 0
    assert lab.cache_calls() == []


def test_a_missing_built_list_saves_nothing(lab):
    result = lab.run("save", (lab.t / "absent.txt").as_posix())
    assert result.returncode == 0
    assert lab.cache_calls() == []


@pytest.mark.parametrize("args", [[], ["nonsense"], ["build"], ["save"]])
def test_bad_usage_fails(lab, args):
    assert lab.run(*args).returncode != 0


def advance_head(lab):
    (lab.workspace / "sim" / "dotnet" / "src" / "Next.cs").write_text("x")
    subprocess.run([*lab.git, "add", "-A"], cwd=lab.workspace, check=True)
    subprocess.run([*lab.git, "commit", "-q", "-m", "ahead"], cwd=lab.workspace, check=True)


def test_a_checkout_that_is_not_the_default_branch_tip_is_never_saved(lab):
    advance_head(lab)
    result = lab.run("save", lab.write_built(CORE))
    assert result.returncode == 0
    assert lab.cache_calls() == []
    assert "skip: not the default branch tip" in result.stdout + result.stderr


def test_a_checkout_that_is_the_default_branch_tip_is_saved(lab):
    advance_head(lab)
    subprocess.run([*lab.git, "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=lab.workspace, check=True)
    lab.table(["save Core saved 0"])
    assert lab.run("save", lab.write_built(CORE)).returncode == 0
    assert lab.cache_calls() == ["save Core"]


def test_a_missing_remote_tracking_ref_means_no_save(lab):
    subprocess.run([*lab.git, "update-ref", "-d", "refs/remotes/origin/main"], cwd=lab.workspace, check=True)
    result = lab.run("save", lab.write_built(CORE))
    assert result.returncode == 0 and lab.cache_calls() == []
    assert "skip: not the default branch tip" in result.stdout + result.stderr


@pytest.mark.parametrize("branch", [None, "", "other", "ma in", "-main", "main;x", "../main"])
def test_the_default_branch_must_be_named_and_safe(lab, branch):
    if branch and branch != "other":
        subprocess.run([*lab.git, "update-ref", f"refs/remotes/origin/{branch}", "HEAD"], cwd=lab.workspace, capture_output=True)
    result = lab.run("save", lab.write_built(CORE), SIM_DEFAULT_BRANCH=branch)
    assert result.returncode == 0 and lab.cache_calls() == []
    assert "skip: not the default branch tip" in result.stdout + result.stderr


def test_a_branch_tip_elsewhere_does_not_authorise_the_save(lab):
    advance_head(lab)
    subprocess.run([*lab.git, "update-ref", "refs/remotes/origin/topic", "HEAD"], cwd=lab.workspace, check=True)
    result = lab.run("save", lab.write_built(CORE), SIM_DEFAULT_BRANCH="topic")
    assert lab.cache_calls() == ["save Core"]
    result = lab.run("save", lab.write_built(CORE))
    assert lab.cache_calls() == ["save Core"]


def test_a_modified_tracked_file_under_a_directory_named_bin_stops_the_save(lab):
    tracked = lab.workspace / "tools" / "bin" / "run.sh"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("x")
    subprocess.run([*lab.git, "add", "-A"], cwd=lab.workspace, check=True)
    subprocess.run([*lab.git, "commit", "-q", "-m", "tracked bin"], cwd=lab.workspace, check=True)
    subprocess.run([*lab.git, "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=lab.workspace, check=True)
    tracked.write_text("changed")
    lab.table(["save Core saved 0"])
    result = lab.run("save", lab.write_built(CORE))
    assert result.returncode == 0
    assert lab.cache_calls() == []
