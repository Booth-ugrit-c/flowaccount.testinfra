from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import workflow_policy

SCRIPT = Path(__file__).resolve().parents[1] / "prejob.sh"
BASH = shutil.which("bash")
SHELLS = [name for name in ("bash", "sh") if shutil.which(name)]
ATTEMPT_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "sim-gate-attempt.yml"
HYGIENE_STEP = next(
    step
    for step in workflow_policy.parse(ATTEMPT_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["run"]["steps"]
    if step.get("name") == "Hygiene"
)
DOTNET_SURVIVORS = HYGIENE_STEP["env"]["SIM_SURVIVORS"]
GIT = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

FAKE_DOCKER = """\
#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_LOG"
[ -z "${FAKE_DOCKER_FAIL:-}" ] || exit 1
kind=container
if [ "$1" = network ] || [ "$1" = volume ]; then kind=$1; shift; fi
sub=$1; shift
case "$sub" in
ps|ls)
    for last; do :; done
    runner=${last#label=sim.runner=}
    [ "$last" != "label=sim.runner" ] || runner=
    awk -v k="$kind" -v r="$runner" '$1 == k && (r == "" || $3 == r) { print $2 }' "$FAKE_DOCKER_STATE"
    ;;
rm)
    for id; do
        case "$id" in -*) continue ;; esac
        grep -v "^$kind $id " "$FAKE_DOCKER_STATE" > "$FAKE_DOCKER_STATE.tmp"
        mv "$FAKE_DOCKER_STATE.tmp" "$FAKE_DOCKER_STATE"
    done
    ;;
esac
"""

FAKE_PS = """\
#!/bin/sh
me=$(id -u)
echo "$(cat "$T/self") $(cat "$T/parent") $me dotnet"
echo "$(cat "$T/parent") 1 $me dotnet"
echo "$(cat "$T/victim") 1 $me dotnet"
echo "$(cat "$T/other_comm") 1 $me sleep"
echo "$(cat "$T/other_user") 1 $((me + 1)) dotnet"
echo "900001 1 $me Runner.Listener"
echo "900002 900001 $me Runner.Worker"
echo "$(cat "$T/sibling") 900002 $me dotnet"
"""

WRAPPER = """\
#!/bin/bash
echo $$ > "$T/parent"
sleep 120 & echo $! > "$T/victim"
sleep 120 & echo $! > "$T/other_comm"
sleep 120 & echo $! > "$T/other_user"
sleep 120 & echo $! > "$T/sibling"
bash -c 'echo $$ > "$T/self"; exec "$PREJOB_SHELL" "$SCRIPT"' > "$T/out" 2>&1
echo $? > "$T/rc"
for name in victim other_comm other_user sibling; do
    if kill -0 "$(cat "$T/$name")" 2>/dev/null; then echo alive > "$T/$name.state"; else echo dead > "$T/$name.state"; fi
    kill -KILL "$(cat "$T/$name")" 2>/dev/null
done
exit 0
"""


def posix(path):
    return Path(path).as_posix()


class Lab:
    def __init__(self, tmp_path, shell):
        self.shell = shell
        self.t = tmp_path / "t"
        self.t.mkdir()
        self.workspace = tmp_path / "home" / "runner" / "_work" / "repo" / "repo"
        self.workspace.mkdir(parents=True)
        self.state = self.t / "docker.state"
        self.log = self.t / "docker.log"
        self.state.write_text("")
        self.log.write_text("")
        for name, body in (("docker", FAKE_DOCKER), ("ps", FAKE_PS), ("wrapper.sh", WRAPPER)):
            path = self.t / name
            path.write_bytes(body.encode())
            path.chmod(0o755)

    def containers(self, *rows):
        self.state.write_text("".join(f"{kind} {ident} {runner}\n" for kind, ident, runner in rows))

    def remaining(self):
        return sorted(line for line in self.state.read_text().splitlines() if line)

    def project(self, rel, with_project_file=True):
        base = self.workspace / rel
        for out in ("bin", "obj"):
            (base / out).mkdir(parents=True, exist_ok=True)
            (base / out / "artifact.dll").write_text("x")
        if with_project_file:
            (base / f"{base.name}.csproj").write_text("<Project/>")
        return base

    def env(self, **over):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RUNNER_", "GITHUB_", "SIM_"))}
        env.update(
            T=posix(self.t),
            SCRIPT=posix(SCRIPT),
            PREJOB_SHELL=self.shell,
            RUNNER_NAME="runner-a",
            SIM_SURVIVORS=DOTNET_SURVIVORS,
            GITHUB_WORKSPACE=posix(self.workspace),
            SIM_DOCKER=posix(self.t / "docker"),
            SIM_PS=posix(self.t / "ps"),
            FAKE_DOCKER_STATE=posix(self.state),
            FAKE_DOCKER_LOG=posix(self.log),
        )
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, cwd=None, **over):
        subprocess.run([BASH, posix(self.t / "wrapper.sh")], env=self.env(**over), timeout=60, check=True, cwd=cwd)
        read = lambda name: (self.t / name).read_text().strip()
        return {
            "rc": int(read("rc")),
            "out": read("out") if (self.t / "out").exists() else "",
            "victim": read("victim.state"),
            "other_comm": read("other_comm.state"),
            "other_user": read("other_user.state"),
            "sibling": read("sibling.state"),
        }

    def docker_calls(self):
        return [line for line in self.log.read_text().splitlines() if line]


@pytest.fixture(params=SHELLS)
def lab(tmp_path, request):
    return Lab(tmp_path, request.param)


def test_removes_only_this_runners_containers_networks_and_volumes(lab):
    lab.containers(
        ("container", "c-a1", "runner-a"),
        ("container", "c-b1", "runner-b"),
        ("network", "n-a1", "runner-a"),
        ("network", "n-b1", "runner-b"),
        ("volume", "v-a1", "runner-a"),
        ("volume", "v-b1", "runner-b"),
    )
    lab.run()
    assert lab.remaining() == ["container c-b1 runner-b", "network n-b1 runner-b", "volume v-b1 runner-b"]


def test_a_runner_name_that_prefixes_a_sibling_does_not_touch_the_sibling(lab):
    lab.containers(("container", "c-1", "runner-a"), ("container", "c-2", "runner-a2"), ("container", "c-3", "runner-"))
    lab.run()
    assert lab.remaining() == ["container c-2 runner-a2", "container c-3 runner-"]


def test_docker_is_only_asked_for_this_runners_label(lab):
    lab.containers(("container", "c-a1", "runner-a"))
    lab.run()
    calls = lab.docker_calls()
    assert calls and all("label=sim.runner=runner-a" in c for c in calls if " ls" in c or c.startswith("ps"))
    assert not any("prune" in c or "system" in c for c in calls)


@pytest.mark.parametrize("name", ["", "a b", "runner*", "../x", "runner;rm", "a" * 100])
def test_an_invalid_runner_name_fails_and_never_calls_docker(lab, name):
    lab.containers(("container", "c-a1", "runner-a"))
    result = lab.run(RUNNER_NAME=name)
    assert result["rc"] != 0
    assert lab.docker_calls() == []
    assert lab.remaining() == ["container c-a1 runner-a"]


def test_a_missing_runner_name_fails(lab):
    result = lab.run(RUNNER_NAME=None)
    assert result["rc"] != 0 and lab.docker_calls() == []


def test_docker_error_fails_the_job_instead_of_assuming_clean(lab):
    result = lab.run(FAKE_DOCKER_FAIL="1")
    assert result["rc"] != 0


def test_no_docker_binary_skips_container_cleanup_and_continues(lab):
    project = lab.project("src/App")
    result = lab.run(SIM_DOCKER="no-such-docker-binary")
    assert result["rc"] != 1 or "docker" not in result["out"].lower()
    assert not (project / "bin").exists()


def test_wipes_bin_and_obj_of_every_project_in_the_workspace(lab):
    app = lab.project("src/App")
    deep = lab.project("src/Deep/Nested/Lib")
    lab.run()
    for base in (app, deep):
        assert not (base / "bin").exists() and not (base / "obj").exists()
        assert (base / f"{base.name}.csproj").exists()


def test_keeps_bin_and_obj_that_do_not_belong_to_a_project(lab):
    plain = lab.project("docs/site", with_project_file=False)
    hidden = lab.project(".git/hooks-dir", with_project_file=False)
    modules = lab.project("node_modules/pkg")
    lab.run()
    assert (plain / "bin" / "artifact.dll").exists()
    assert (hidden / "bin" / "artifact.dll").exists()
    assert (modules / "bin" / "artifact.dll").exists()


def test_never_touches_anything_outside_the_workspace(lab, tmp_path):
    outside = tmp_path / "home" / "runner" / "other" / "App"
    for out in ("bin", "obj"):
        (outside / out).mkdir(parents=True)
        (outside / out / "keep.dll").write_text("x")
    (outside / "App.csproj").write_text("<Project/>")
    lab.run()
    assert (outside / "bin" / "keep.dll").exists() and (outside / "obj" / "keep.dll").exists()


@pytest.mark.parametrize("workspace", ["", "/", "/tmp", "/home", "relative/path", "/home/runner"])
def test_an_unsafe_workspace_is_refused_and_nothing_is_deleted(lab, workspace):
    project = lab.project("src/App")
    result = lab.run(GITHUB_WORKSPACE=workspace)
    assert result["rc"] != 0
    assert (project / "bin" / "artifact.dll").exists()


def test_a_relative_workspace_is_refused_even_when_it_resolves(lab, tmp_path):
    project = lab.project("src/App")
    result = lab.run(cwd=tmp_path, GITHUB_WORKSPACE="home/runner/_work/repo/repo")
    assert result["rc"] != 0
    assert (project / "bin" / "artifact.dll").exists()


@pytest.mark.parametrize("suffix", ["/src/..", "/src/../", "/./src/..", "/src/../src"])
def test_a_workspace_with_dot_segments_is_refused(lab, suffix):
    project = lab.project("src/App")
    result = lab.run(GITHUB_WORKSPACE=posix(lab.workspace) + suffix)
    assert result["rc"] != 0
    assert (project / "bin" / "artifact.dll").exists()


def test_a_workspace_that_does_not_exist_fails(lab, tmp_path):
    result = lab.run(GITHUB_WORKSPACE=posix(tmp_path / "home" / "runner" / "_work" / "none" / "none"))
    assert result["rc"] != 0


def test_a_workspace_equal_to_home_is_refused(lab):
    project = lab.project("src/App")
    home = posix(lab.workspace)
    if sys.platform == "win32":
        home = f"/{home[0].lower()}{home[2:]}"
    result = lab.run(HOME=home, GITHUB_WORKSPACE=home)
    assert result["rc"] != 0
    assert (project / "bin" / "artifact.dll").exists()


def test_a_leftover_dotnet_process_is_killed_and_fails_the_job(lab):
    result = lab.run()
    assert result["rc"] != 0
    assert result["victim"] == "dead"
    assert "survived" in result["out"]


def test_survivor_check_spares_other_commands_other_users_the_script_and_its_parents(lab):
    result = lab.run()
    assert result["other_comm"] == "alive"
    assert result["other_user"] == "alive"
    assert "self" not in result["out"]


def test_survivor_report_names_pid_and_command_but_no_arguments(lab):
    result = lab.run()
    assert "dotnet" in result["out"]
    assert str(result["other_user"]) not in result["out"].replace("alive", "")


def test_the_survivor_names_come_from_the_adapter_not_from_core(lab):
    result = lab.run(SIM_SURVIVORS="node")
    assert result["rc"] == 0, result["out"]
    assert result["victim"] == "alive"
    assert "prejob: clean" in result["out"]


@pytest.mark.parametrize("survivors", [None, "", "   ", "dotnet;id", "dotnet\nnode", "dot*"])
def test_missing_or_unsafe_survivor_names_fail_before_any_cleanup(lab, survivors):
    lab.containers(("container", "c-a1", "runner-a"))
    project = lab.project("src/App")
    result = lab.run(SIM_SURVIVORS=survivors)
    assert result["rc"] != 0
    assert "SIM_SURVIVORS" in result["out"]
    assert lab.docker_calls() == []
    assert (project / "bin" / "artifact.dll").exists()
    assert result["victim"] == "alive"


def test_a_sibling_runners_live_job_process_is_not_a_survivor(lab):
    result = lab.run()
    assert result["victim"] == "dead"
    assert result["sibling"] == "alive"


def test_the_shipped_survivor_list_names_the_dotnet_processes():
    assert DOTNET_SURVIVORS.split() == ["dotnet", "testhost", "vstest.console", "VBCSCompiler"]


def writable_then(function, path, _):
    os.chmod(path, 0o700)
    function(path)


def run_hygiene_step(tmp_path, prejob, sha=None, extra=None):
    workspace = tmp_path / "workspace"
    trusted = workspace / ".sim-trusted"
    (trusted / "core").mkdir(parents=True)
    (trusted / "core" / "prejob.sh").write_bytes(prejob.encode())
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "trusted"]):
        subprocess.run([*GIT, *args], cwd=trusted, check=True, capture_output=True)
    head = subprocess.run([*GIT, "rev-parse", "HEAD"], cwd=trusted, check=True, capture_output=True, text=True).stdout.strip()
    if extra:
        extra(trusted)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "SIM_"))}
    env.update(HYGIENE_STEP["env"], GITHUB_SHA=sha or head)
    proc = subprocess.run([BASH, "-c", HYGIENE_STEP["run"]], cwd=workspace, env=env, capture_output=True, text=True, timeout=60)
    return proc, trusted


def test_the_hygiene_step_runs_the_trusted_copy_with_the_survivor_list_and_removes_it(tmp_path):
    proc, trusted = run_hygiene_step(tmp_path, 'echo "ran with $SIM_SURVIVORS"\nexit 3\n')
    assert proc.returncode == 3
    assert f"ran with {DOTNET_SURVIVORS}" in proc.stdout
    assert not trusted.exists()


def test_the_hygiene_step_passes_when_the_trusted_prejob_passes(tmp_path):
    proc, trusted = run_hygiene_step(tmp_path, "echo clean\n")
    assert (proc.returncode, proc.stdout.strip()) == (0, "clean")
    assert not trusted.exists()


def test_the_hygiene_step_refuses_a_trusted_copy_that_is_not_the_dispatch_commit(tmp_path):
    proc, trusted = run_hygiene_step(tmp_path, "echo ran\n", sha="0" * 40)
    assert proc.returncode == 1
    assert "ran" not in proc.stdout.replace("::error::the trusted checkout is not the dispatch commit", "")
    assert "the trusted checkout is not the dispatch commit" in proc.stdout
    assert not trusted.exists()


def test_the_hygiene_step_refuses_a_trusted_directory_that_is_not_a_checkout(tmp_path):
    proc, trusted = run_hygiene_step(tmp_path, "echo ran\n", extra=lambda path: shutil.rmtree(path / ".git", onexc=writable_then))
    assert proc.returncode == 1
    assert "the trusted checkout is not the dispatch commit" in proc.stdout
    assert not trusted.exists()
