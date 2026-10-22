from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cache_npm.sh"
SHELLS = [shutil.which(name) for name in ("bash", "sh") if shutil.which(name)]

pytestmark = pytest.mark.skipif(not SHELLS, reason="a POSIX shell is required")

FAKE = """\
#!/bin/sh
printf '%s endpoint=%s args=%s\\n' "$FAKE_NAME" "${SIM_CACHE_ENDPOINT:-unset}" "$*" >> "$T/calls.log"
case "$1" in
    tripwire)
        answer=$(cat "$T/tripwire")
        [ "$answer" != tripped ] || [ -n "${FAKE_NO_MARKER:-}" ] || : > "$3/cache-tripwire"
        printf '%s\\n' "$answer"
        ;;
    *) cat "$T/$1" ;;
esac
exit "$(cat "$T/rc")"
"""


class Lab:
    def __init__(self, tmp_path: Path, shell: str):
        self.shell = shell
        self.t = tmp_path / "t"
        self.t.mkdir()
        self.markers = tmp_path / "markers"
        self.markers.mkdir()
        self.dir = tmp_path / "npm-cache"
        for name in ("cache", "store"):
            fake = self.t / name
            fake.write_bytes(FAKE.replace("$FAKE_NAME", name).encode())
            fake.chmod(0o755)
        self.answers(tripwire="clear", restore="hit", save="saved", rc="0")
        (self.t / "calls.log").write_text("")

    def answers(self, **values):
        for name, value in values.items():
            (self.t / name).write_text(value + "\n")

    def run(self, *args, **over):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SIM_", "RUNNER_", "GITHUB_", "MARKERS"))}
        env.update(
            T=self.t.as_posix(),
            SIM_CACHE_CMD=(self.t / "cache").as_posix(),
            SIM_STORE_CMD=(self.t / "store").as_posix(),
            SIM_ROOT="sim/workspace",
            RUNNER_ENVIRONMENT="github-hosted",
            SIM_TAILNET_JOIN="success",
            SIM_CACHE_ENDPOINT_LAB="http://lab.invalid",
            SIM_CACHE_ENDPOINT_TAILNET="http://tailnet.invalid",
            MARKERS=self.markers.as_posix(),
        )
        env.update(over)
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run([self.shell, SCRIPT.as_posix(), *args], env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        return (self.t / "calls.log").read_text().splitlines()


@pytest.fixture(params=SHELLS)
def lab(tmp_path, request):
    return Lab(tmp_path, request.param)


def test_a_clear_tripwire_asks_the_store_once_and_keeps_the_cache_on(lab):
    result = lab.run("tripwire", lab.markers.as_posix())
    assert result.returncode == 0, result.stderr
    assert lab.calls() == [f"store endpoint=http://tailnet.invalid args=tripwire --markers {lab.markers.as_posix()}"]
    assert not (lab.markers / "cache-off").exists()


def test_a_tripped_store_stops_the_step(lab):
    lab.answers(tripwire="tripped")
    result = lab.run("tripwire", lab.markers.as_posix())
    assert result.returncode == 1 and "tripwire" in result.stderr
    assert (lab.markers / "cache-tripwire").exists()


def test_a_tripped_answer_stops_the_step_even_without_the_marker(lab):
    lab.answers(tripwire="tripped")
    result = lab.run("tripwire", lab.markers.as_posix(), FAKE_NO_MARKER="1")
    assert result.returncode == 1 and not (lab.markers / "cache-tripwire").exists()


def test_a_tripwire_marker_stops_the_step_whatever_the_answer(lab):
    (lab.markers / "cache-tripwire").write_text("")
    result = lab.run("tripwire", lab.markers.as_posix())
    assert result.returncode == 1


@pytest.mark.parametrize("answer, rc", [("", "0"), ("garbage", "0"), ("clear", "1")])
def test_a_store_that_cannot_be_asked_turns_the_cache_off_loudly(lab, answer, rc):
    lab.answers(tripwire=answer, rc=rc)
    result = lab.run("tripwire", lab.markers.as_posix())
    assert result.returncode == 0
    assert (lab.markers / "cache-off").exists() and "::warning title=cache off::" in result.stdout


@pytest.mark.parametrize("join", ["failure", "skipped", ""])
def test_a_hosted_runner_whose_tailnet_join_failed_never_asks_the_store(lab, join):
    result = lab.run("tripwire", lab.markers.as_posix(), SIM_TAILNET_JOIN=join)
    assert result.returncode == 0 and lab.calls() == []
    assert (lab.markers / "cache-off").exists()


def test_a_lab_runner_ignores_the_join_and_uses_the_lab_endpoint(lab):
    result = lab.run("tripwire", lab.markers.as_posix(), RUNNER_ENVIRONMENT="self-hosted", SIM_TAILNET_JOIN="failure")
    assert result.returncode == 0
    assert lab.calls()[0].startswith("store endpoint=http://lab.invalid ")


def test_an_unknown_runner_environment_gets_no_endpoint(lab):
    lab.run("restore", lab.dir.as_posix(), RUNNER_ENVIRONMENT="elsewhere")
    assert lab.calls() == [f"cache endpoint=unset args=restore --root sim/workspace --dir {lab.dir.as_posix()}"]


def test_restore_asks_the_cache_for_the_directory_and_reports_the_answer(lab):
    result = lab.run("restore", lab.dir.as_posix())
    assert result.returncode == 0 and "cache: hit" in result.stdout
    assert lab.calls() == [f"cache endpoint=http://tailnet.invalid args=restore --root sim/workspace --dir {lab.dir.as_posix()}"]
    assert lab.dir.is_dir()


def test_a_failing_restore_is_a_miss_never_a_failure(lab):
    lab.answers(restore="", rc="2")
    result = lab.run("restore", lab.dir.as_posix())
    assert result.returncode == 0 and "cache: miss" in result.stdout


@pytest.mark.parametrize("mode", ["restore", "save"])
@pytest.mark.parametrize("how", ["input", "marker"])
def test_with_the_cache_off_nothing_is_asked(lab, mode, how):
    over = {"SIM_CACHE_MODE": "off"} if how == "input" else {}
    if how == "marker":
        (lab.markers / "cache-off").write_text("")
    result = lab.run(mode, lab.dir.as_posix(), **over)
    assert result.returncode == 0 and lab.calls() == []


def test_save_hands_the_directory_to_the_cache(lab):
    result = lab.run("save", lab.dir.as_posix())
    assert result.returncode == 0 and "cache: saved" in result.stdout
    assert lab.calls() == [f"cache endpoint=http://tailnet.invalid args=save --root sim/workspace --dir {lab.dir.as_posix()}"]


def test_a_failing_save_is_skipped_never_a_failure(lab):
    lab.answers(save="", rc="2")
    result = lab.run("save", lab.dir.as_posix())
    assert result.returncode == 0 and "cache: save skipped" in result.stdout


@pytest.mark.parametrize("args", [(), ("build",), ("tripwire",), ("restore",), ("save",)])
def test_bad_usage_fails(lab, args):
    assert lab.run(*args).returncode != 0
